"""WhatsApp, from the chat exports you make on the phone: read only, no API, nothing sent.

WhatsApp has no API for a personal account's chats. What it does have is *Export chat*,
which writes the conversation as ``_chat.txt`` (zipped, with or without the media). This
channel reads those exports into a local archive, one per chat, and serves the archive as
an ordinary conversation, so ``read`` with ``since`` works here as it does everywhere and
answers "what is new since I last processed this chat?".

Reference: ``whatsapp:<chat>``, where ``<chat>`` is a name you choose when importing
(lowercase letters, digits, ``-``, ``_`` and ``.``), for example ``whatsapp:ada-lovelace``.

Two steps:

1. :meth:`WhatsAppExport.import_export` parses an export (the ``.zip`` or the ``.txt``) and
   merges it into the chat's archive. Exports overlap (each one repeats the whole chat), so
   a message already archived is recognised by its id and not added twice. The result says
   how many messages were new and the time span they cover.
2. :meth:`WhatsAppExport.read` returns the archived messages, oldest first, with ``since``
   and ``limit``.

The archive lives under the data root (``<data root>/whatsapp/``), never in a repository:
``<chat>.jsonl`` holds one message per line, ``<chat>.imports.jsonl`` one line per import,
and ``<chat>.txt`` a transcript with one ``<ISO time> <sender>: <text>`` line per message,
for grep and for anchors (a time is a stable anchor across imports; a line number is not).

Both export layouts are parsed: iOS (``[2/11/26, 12:51:08] Ada: hi``) and Android
(``2/11/26, 12:51 - Ada: hi``), with 12- or 24-hour times, and ``/``, ``.`` or ``-`` in
dates. Whether a date is day-first is decided from the export itself (a field above 12
settles it; otherwise the reading that keeps the times in order wins), or set with
``dayfirst``. Times carry no zone in an export: they are read in the zone named by ``tz``
(an IANA name), else this machine's.

>>> rows = parse_export("[2/11/26, 12:51:08] Ada: hi\\nsecond line\\n[2/11/26, 12:52:00] Bram: \\u200eimage omitted", tz="UTC")
>>> [(r["sent_at"], r["sender"], r["text"], r["native"].get("media")) for r in rows]
[('2026-02-11T12:51:08+00:00', 'Ada', 'hi\\nsecond line', None), ('2026-02-11T12:52:00+00:00', 'Bram', '', 'image')]
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from collections import Counter
from collections.abc import Iterable, Mapping, MutableMapping
from datetime import datetime
from pathlib import Path
from typing import Any

from correspond.errors import CorrespondError, InvalidRef
from correspond.model import (
    Audience,
    Authenticity,
    Capabilities,
    ChannelIdentity,
    ConversationRef,
    Grade,
    HistoryDepth,
    Message,
    Scope,
    Support,
    format_time,
    parse_time,
)
from correspond.ops import window

__all__ = [
    "ARCHIVE_KIND",
    "NAME",
    "WhatsAppExport",
    "export_text",
    "message_id",
    "parse_export",
]

NAME = "whatsapp"
#: The data-root folder that holds the archives.
ARCHIVE_KIND = "whatsapp"

_CHAT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
#: Direction and isolation marks WhatsApp scatters through exports.
_MARKS_RE = re.compile("[‎‏‪-‮⁦-⁩]")
_DATE = r"(\d{1,4})[./-](\d{1,2})[./-](\d{1,4})"
_TIME = r"(\d{1,2})[:.](\d{2})(?:[:.](\d{2}))?(?:[\s ]*([AaPp])\.?\s?[Mm]\.?)?"
_IOS_RE = re.compile(rf"^\[{_DATE},?\s+{_TIME}\]\s(?P<rest>.*)$")
_ANDROID_RE = re.compile(rf"^{_DATE},?\s+{_TIME}\s[-–]\s(?P<rest>.*)$")
_SENDER_RE = re.compile(r"^(?P<sender>[^:]{1,80}?):\s?(?P<text>.*)$", re.S)

_EDITED = "<This message was edited>"
_MEDIA = {
    "image omitted": "image",
    "video omitted": "video",
    "audio omitted": "audio",
    "sticker omitted": "sticker",
    "GIF omitted": "gif",
    "document omitted": "document",
    "Contact card omitted": "contact",
    "<Media omitted>": "media",
}
_ATTACHED_RE = re.compile(r"^<attached:\s*(?P<name>[^>]+)>$|^(?P<name2>\S+) \(file attached\)$")
_DELETED = ("This message was deleted.", "You deleted this message.")
_CALL_RE = re.compile(r"^(Missed )?(voice|video) call(, .*)?$", re.I)


# ------------------------------------------------------------------------- parsing


def export_text(source: str | Path | bytes) -> str:
    """The text of an export: a ``.txt`` file, a ``.zip`` holding ``_chat.txt`` (or one ``.txt``), or the raw bytes of either."""
    data = source if isinstance(source, bytes) else Path(source).expanduser().read_bytes()
    if data[:4] == b"PK\x03\x04":
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            texts = [n for n in archive.namelist() if n.lower().endswith(".txt")]
            chosen = next((n for n in texts if Path(n).name == "_chat.txt"), None)
            if chosen is None and len(texts) == 1:
                chosen = texts[0]
            if chosen is None:
                raise CorrespondError(
                    f"the zip holds {len(texts)} .txt files and no _chat.txt: name the chat file and pass it alone"
                )
            data = archive.read(chosen)
    return data.decode("utf-8-sig", errors="replace")


def _headers(lines: Iterable[str]) -> list[tuple[int, re.Match]]:
    found = []
    for number, line in enumerate(lines, 1):
        clean = _MARKS_RE.sub("", line)
        match = _IOS_RE.match(clean) or _ANDROID_RE.match(clean)
        if match:
            found.append((number, match))
    return found


def _dates(match: re.Match, dayfirst: bool) -> tuple[int, int, int]:
    a, b, c = (int(match.group(i)) for i in (1, 2, 3))
    if len(match.group(1)) == 4:  # ISO-like: year first
        return a, b, c
    year = c + 2000 if c < 100 else c
    return (year, b, a) if dayfirst else (year, a, b)


def _clock(match: re.Match) -> tuple[int, int, int]:
    hour, minute = int(match.group(4)), int(match.group(5))
    second = int(match.group(6) or 0)
    meridiem = (match.group(7) or "").lower()
    if meridiem == "p" and hour < 12:
        hour += 12
    elif meridiem == "a" and hour == 12:
        hour = 0
    return hour, minute, second


def _guess_dayfirst(headers: list[tuple[int, re.Match]]) -> bool:
    """Day-first when a first field exceeds 12, month-first when a second does; otherwise the reading that keeps times in order."""
    slashed = [m for _, m in headers if len(m.group(1)) != 4]
    if any(int(m.group(1)) > 12 for m in slashed):
        return True
    if any(int(m.group(2)) > 12 for m in slashed):
        return False

    def descents(dayfirst: bool) -> int:
        stamps = [(*_dates(m, dayfirst), *_clock(m)) for m in slashed]
        return sum(1 for x, y in zip(stamps, stamps[1:]) if y < x)

    return descents(True) < descents(False)


def _zone(tz: str | None):
    if tz is None:
        return None
    from zoneinfo import ZoneInfo

    return ZoneInfo(tz)


def _when(match: re.Match, dayfirst: bool, zone) -> datetime:
    naive = datetime(*_dates(match, dayfirst), *_clock(match))
    return naive.replace(tzinfo=zone) if zone is not None else naive.astimezone()


def _native(text: str, sender: str) -> tuple[str, dict]:
    native: dict[str, Any] = {}
    if text.endswith(_EDITED):
        text, native["edited"] = text[: -len(_EDITED)].rstrip(), True
    stripped = text.strip()
    if stripped in _MEDIA:
        return "", {**native, "media": _MEDIA[stripped]}
    attached = _ATTACHED_RE.match(stripped)
    if attached:
        name = attached.group("name") or attached.group("name2")
        return "", {**native, "media": "file", "attached": name.strip()}
    if stripped in _DELETED:
        return "", {**native, "deleted": True}
    if _CALL_RE.match(stripped):
        return stripped, {**native, "call": True}
    if not sender:
        native["system"] = True
    return text, native


def parse_export(
    text: str, *, dayfirst: bool | None = None, tz: str | None = None
) -> list[dict]:
    """The messages of an export's text, in file order, as JSON-ready rows.

    Each row: ``sent_at`` (ISO 8601 with offset), ``sender`` (the name the export gives,
    ``""`` for a system line), ``text`` (continuation lines joined, WhatsApp's direction
    marks removed, media and edit markers taken out) and ``native``: ``media`` (``image``,
    ``video``, ``audio``, ``sticker``, ``gif``, ``document``, ``contact``, ``file``,
    ``media``), ``attached`` (the file name, when the export carried the file), ``edited``,
    ``deleted``, ``call``, ``system``, and ``line`` (the line number in this export).
    Text before the first dated line is ignored.
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    headers = _headers(lines)
    if not headers:
        return []
    if dayfirst is None:
        dayfirst = _guess_dayfirst(headers)
    zone = _zone(tz)
    rows = []
    for index, (number, match) in enumerate(headers):
        end = headers[index + 1][0] - 1 if index + 1 < len(headers) else len(lines)
        body = "\n".join(
            [match.group("rest"), *(_MARKS_RE.sub("", l) for l in lines[number:end])]
        ).rstrip("\n")
        said = _SENDER_RE.match(body)
        sender, said_text = (
            (said.group("sender").strip(), said.group("text")) if said else ("", body)
        )
        if said and not sender:
            sender, said_text = "", body
        message_text, native = _native(_MARKS_RE.sub("", said_text), sender)
        rows.append(
            {
                "sent_at": _when(match, dayfirst, zone).isoformat(),
                "sender": sender,
                "text": message_text,
                "native": {**native, "line": number},
            }
        )
    return rows


def message_id(row: Mapping[str, Any], occurrence: int = 0) -> str:
    """A stable id from the time, sender, text and media kind: the same message in two overlapping exports gets the same id.

    ``occurrence`` tells apart identical messages sent in the same second (the second is
    ``<hash>-1``).

    >>> message_id({"sent_at": "2026-02-11T12:51:08+00:00", "sender": "Ada", "text": "hi"})
    '514e092c59e260a1'
    """
    native = row.get("native") or {}
    key = "\x1f".join(
        (
            str(parse_time(row["sent_at"]).timestamp()),
            row.get("sender", ""),
            row.get("text", ""),
            str(native.get("media") or ""),
            str(native.get("attached") or ""),
        )
    )
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return digest if occurrence == 0 else f"{digest}-{occurrence}"


def _with_ids(rows: Iterable[dict]) -> list[dict]:
    seen: Counter = Counter()
    out = []
    for row in rows:
        base = message_id(row)
        out.append({"id": message_id(row, seen[base]), **row})
        seen[base] += 1
    return out


# ------------------------------------------------------------------------- adapter


def _jsonl(rows: Iterable[Mapping]) -> str:
    return "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows)


def _rows(text: str | None) -> list[dict]:
    return [json.loads(l) for l in (text or "").splitlines() if l.strip()]


def _transcript_line(row: Mapping[str, Any]) -> str:
    native = row.get("native") or {}
    text = row.get("text", "")
    if native.get("media"):
        text = f"[{native.get('attached') or native['media']}] {text}".rstrip()
    if native.get("deleted"):
        text = "[deleted]"
    if native.get("edited"):
        text += " [edited]"
    body = text.replace("\n", "\n    ")
    return f"{row['sent_at']} {row.get('sender') or '(system)'}: {body}"


class WhatsAppExport:
    """WhatsApp chats as archived from their exports: ``import_export`` merges one in, ``read`` serves it.

    ``store`` is where archives are kept, a ``MutableMapping`` of file names to text (default:
    text files under ``<data root>/whatsapp/``; a ``dict`` in tests). ``self_names`` are the
    names under which the exports show the operator's own messages (default: the
    ``self_name`` setting, comma-separated), so those messages are marked ``is_self``.
    """

    name = NAME

    def __init__(
        self,
        *,
        store: MutableMapping[str, str] | None = None,
        self_names: Iterable[str] | None = None,
    ):
        self._store = store
        self._default_store = False
        self._self_names = tuple(self_names) if self_names is not None else None

    @property
    def store(self) -> MutableMapping[str, str]:
        """The archive store, built on first use."""
        if self._store is None:
            from correspond.stores import text_store

            self._store, self._default_store = text_store(ARCHIVE_KIND), True
        return self._store

    @property
    def self_names(self) -> tuple[str, ...]:
        """The operator's own sender names: given, else the ``self_name`` setting."""
        if self._self_names is None:
            from correspond.registry import value

            configured = value(NAME, "self_name") or ""
            self._self_names = tuple(n.strip() for n in configured.split(",") if n.strip())
        return self._self_names

    @property
    def capabilities(self) -> Capabilities:
        """Read what was imported, and nothing else."""
        return Capabilities(
            channel=NAME,
            read=Support.FULL,
            audience=Support.PARTIAL,
            history_depth=HistoryDepth.SINCE_LINK,
            grades=(Grade.CLAIMED,),
            native_fields=(
                "media",
                "attached",
                "edited",
                "deleted",
                "call",
                "system",
                "line",
            ),
            notes=(
                "read serves only what was imported from chat exports (correspond import-export whatsapp:<chat> <export>); import the newest export first to see recent messages",
                "authors are the names the export gives, graded claimed: an export is a text file, and nothing in it is verified",
                "an export carries no time zone: times are read in the zone given at import, else this machine's",
                "an edited message's earlier text is not in an export, and an edit made between two exports appears as a new message",
                "audience is the senders seen in the imported exports: a member who never wrote is not listed; two senders make a private chat (named), more a group",
            ),
        )

    def parse_ref(self, id: str) -> ConversationRef:
        """A chat name: lowercase letters, digits, ``-``, ``_``, ``.``."""
        if not _CHAT_RE.match(id or ""):
            raise InvalidRef(
                f"whatsapp:{id}",
                "a WhatsApp chat is named when it is imported: lowercase letters, digits, '-', '_' or '.', e.g. whatsapp:ada-lovelace",
            )
        return ConversationRef(channel=NAME, id=id, kind="chat")

    def audience(self, ref: ConversationRef, *, draft: Any = None) -> Audience:
        """Who reads the chat, as far as the archive shows: everyone who wrote in it."""
        senders = sorted({r["sender"] for r in self.archived(ref) if r.get("sender")})
        readers = [
            ChannelIdentity(
                channel=NAME,
                native_id=s,
                display_name=s,
                is_self=s in self.self_names,
            )
            for s in senders
        ]
        others = [r for r in readers if not r.is_self]
        return Audience(
            ref=ref.encoded,
            scope=Scope.GROUP if len(senders) > 2 else Scope.NAMED,
            readers=readers,
            external=bool(others) if senders else None,
            durability=("copies_pushed",),
            widening=("forwarding",) + (("joiners_read_history",) if len(senders) > 2 else ()),
            evidence=(
                f"{len(senders)} sender(s) in the imported exports of {ref.encoded}; a member who never wrote is not listed",
            ),
        )

    # ------------------------------------------------------------------ archive

    def archived(self, ref: ConversationRef) -> list[dict]:
        """The archived rows of one chat, oldest first."""
        return _rows(self.store.get(f"{ref.id}.jsonl"))

    def imports(self, ref: ConversationRef) -> list[dict]:
        """One record per import of this chat, oldest first."""
        return _rows(self.store.get(f"{ref.id}.imports.jsonl"))

    def transcript_path(self, ref: ConversationRef) -> str | None:
        """The transcript's path on disk, when the archive is the default one under the data root."""
        if self._store is not None and not self._default_store:
            return None
        from correspond.stores import kind_dir

        return str(kind_dir(ARCHIVE_KIND) / f"{ref.id}.txt")

    def import_export(
        self,
        ref: ConversationRef,
        source: str | Path | bytes,
        *,
        dayfirst: bool | None = None,
        tz: str | None = None,
        now: datetime | None = None,
    ) -> dict:
        """Merge an export into the chat's archive; say what was new.

        Returns ``parsed`` (messages in the export), ``added``, ``total`` (archived now),
        ``previous_last`` (the newest archived message before this import, ``None`` on the
        first), ``first_added`` and ``last_added`` (the span of what was new), and
        ``export_span`` (first and last time in the export). An export whose messages are
        all archived adds nothing and changes nothing but the import log.
        """
        raw = source if isinstance(source, bytes) else Path(source).expanduser().read_bytes()
        rows = _with_ids(parse_export(export_text(raw), dayfirst=dayfirst, tz=tz))
        archived = self.archived(ref)
        known = {r["id"] for r in archived}
        new = [r for r in rows if r["id"] not in known]
        merged = sorted(archived + new, key=lambda r: (parse_time(r["sent_at"]), r["id"]))
        previous_last = archived[-1]["sent_at"] if archived else None
        new_sorted = sorted(new, key=lambda r: parse_time(r["sent_at"]))
        report = {
            "chat": ref.id,
            "imported_at": format_time(now or datetime.now().astimezone()),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "parsed": len(rows),
            "added": len(new),
            "total": len(merged),
            "previous_last": previous_last,
            "first_added": new_sorted[0]["sent_at"] if new_sorted else None,
            "last_added": new_sorted[-1]["sent_at"] if new_sorted else None,
            "export_span": [rows[0]["sent_at"], rows[-1]["sent_at"]] if rows else None,
            "senders": dict(Counter(r["sender"] for r in rows if r["sender"])),
        }
        if new:
            self.store[f"{ref.id}.jsonl"] = _jsonl(merged)
            self.store[f"{ref.id}.txt"] = "\n".join(map(_transcript_line, merged)) + "\n"
        self.store[f"{ref.id}.imports.jsonl"] = _jsonl([*self.imports(ref), report])
        return report

    # ------------------------------------------------------------------- reading

    def _message(self, ref: ConversationRef, row: Mapping[str, Any]) -> Message:
        sender = row.get("sender") or ""
        return Message(
            id=row["id"],
            conversation=ref,
            author=ChannelIdentity(
                channel=NAME,
                native_id=sender,
                display_name=sender or None,
                is_self=bool(sender) and sender in self.self_names,
            ),
            authenticity=Authenticity(
                grade=Grade.CLAIMED, evidence={"source": "chat export"}
            ),
            sent_at=parse_time(row["sent_at"]),
            text=row.get("text", ""),
            native=dict(row.get("native") or {}),
        )

    def read(
        self,
        ref: ConversationRef,
        *,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[Message]:
        """The archived messages, oldest first; ``since`` and ``limit`` as everywhere."""
        return window(
            (self._message(ref, row) for row in self.archived(ref)),
            since=since,
            limit=limit,
        )
