"""What the sender of an email actually wrote: their text, apart from quoted replies, forwarded blocks, signatures and disclaimers.

A reply body carries the whole conversation beneath it, so a profile, a summary or a search
built from raw bodies mostly measures the people being quoted. :func:`authored` splits one
body into its parts:

>>> body = '''Fine by me, ship it Friday.
...
... /Ada
...
... On Mon, 3 Mar 2025 at 10:00, Grace Hopper <grace@example.org> wrote:
... > Can we ship on Friday?
... '''
>>> found = authored(body)
>>> found.text
'Fine by me, ship it Friday.\\n\\n/Ada'
>>> found.quoted.splitlines()[-1]
'> Can we ship on Friday?'

Three shapes are handled:

- **top-posting**: the text above the first reply header (``On … wrote:`` in several
  languages, an Outlook ``From: … Sent: …`` block, ``-----Original Message-----``) or above
  a run of ``>`` lines;
- **forwarding**: a forwarded block is never authored text (``forwarded`` is set);
- **inline replies**: answers written *inside* the quoted text with no ``>`` marker, as
  Outlook users do ("Responses below."). These are recovered only when the earlier messages
  of the thread are passed as ``earlier``: a line of the quoted region that appears in none
  of them was written by this sender. :func:`authored_thread` does that for a whole thread.

Signatures (after ``-- ``, a mobile footer, or a contact block after the sign-off) and legal
disclaimers are split off; the sign-off itself ("Best,", "/Ada") stays in the text, since it
is part of how the person writes.

The reply-header and footer vocabulary is a seam: pass ``markers=`` a :class:`Markers` with
other patterns. The defaults cover English, French, German, Swedish, Spanish, Italian and
Dutch clients. Prior art: ``email-reply-parser`` (top-posting, English) and
``mail-parser-reply`` (multilingual headers); neither recovers inline replies.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace

__all__ = ["Authored", "Markers", "DEFAULT_MARKERS", "authored", "authored_thread"]


def _rx(*patterns: str) -> tuple[re.Pattern, ...]:
    return tuple(re.compile(p, re.IGNORECASE) for p in patterns)


@dataclass(frozen=True)
class Markers:
    """The patterns that recognise reply headers, forwards and footers. Every field is a tuple of compiled regexes matched against one stripped line."""

    #: A line that, on its own, starts the quoted region ("On … wrote:", "-----Original Message-----").
    reply_headers: tuple[re.Pattern, ...] = _rx(
        r"^on\b.{0,300}\bwrote\s*:$",
        r"^le\b.{0,300}\ba [ée]crit\s*:$",
        r"^am\b.{0,300}\bschrieb\b.{0,300}:$",
        r"^(den|on)\b.{0,300}\bskrev\b.{0,300}:$",
        r"^el\b.{0,300}\bescribi[óo]\s*:$",
        r"^il\b.{0,300}\bha scritto\s*:$",
        r"^op\b.{0,300}\bschreef\b.{0,300}:$",
        r"^-{2,}\s*(original message|message d'origine|ursprüngliche nachricht|ursprungligt meddelande|mensaje original|messaggio originale|oorspronkelijk bericht)\s*-{2,}$",
    )
    #: The second line of a wrapped reply header ("wrote:"), never authored text.
    reply_header_tails: tuple[re.Pattern, ...] = _rx(
        r"^.{0,120}\b(wrote|a [ée]crit|schrieb|skrev|escribi[óo]|ha scritto|schreef)\s*:$"
    )
    #: A line that starts a forwarded block.
    forward_headers: tuple[re.Pattern, ...] = _rx(
        r"^-{2,}\s*(forwarded message|message transf[ée]r[ée]|weitergeleitete nachricht|vidarebefordrat meddelande|mensaje reenviado|messaggio inoltrato|doorgestuurd bericht)\s*-{2,}$",
        r"^begin forwarded message\s*:$",
        r"^d[ée]but du message r[ée]exp[ée]di[ée]\s*:$",
    )
    #: The first line of an Outlook-style header block ("From: Ada <ada@…>").
    header_from: tuple[re.Pattern, ...] = _rx(
        r"^\**(from|de|från|von|van|da|fra)\s*:\**\s*\S.*$"
    )
    #: A line that must follow ``header_from`` within a few lines for it to count as a header block.
    header_field: tuple[re.Pattern, ...] = _rx(
        r"^\**(sent|date|envoy[ée]|skickat|datum|gesendet|verzonden|inviato|enviado|fecha|to|à|till|an|aan|a|para|subject|objet|ämne|betreff|onderwerp|oggetto|asunto)\s*:\**(\s|$)"
    )
    #: A separator line Outlook puts above its header block.
    separators: tuple[re.Pattern, ...] = _rx(r"^_{10,}$", r"^-{20,}$")
    #: A line that starts a signature on its own ("-- ", a mobile footer).
    signature_starts: tuple[re.Pattern, ...] = _rx(
        r"^--\s*$",
        r"^(sent|envoy[ée]|skickat|gesendet|enviado|inviato|verzonden) (from|de|depuis|från|von|desde|da|vanaf) (my|mon|ma|min|mein|meinem|mi|il mio|mijn)\b.*$",
        r"^von meinem \S+ gesendet$",
        r"^get outlook for\b.*$",
        r"^(obtenir|télécharger) outlook pour\b.*$",
        r"^h[äa]mta outlook f[öo]r\b.*$",
    )
    #: A sign-off line: what is after it, in a trailing block of short lines, is a signature.
    sign_offs: tuple[re.Pattern, ...] = _rx(
        r"^/\s?\w[\w .-]{0,30}$",
        r"^(best|best regards|kind regards|warm regards|regards|cheers|thanks|thank you|many thanks|all the best|br|brgds|rgds"
        r"|cordialement|bien cordialement|bien à (vous|toi)|amitiés|bonne journée|merci"
        r"|mvh|med vänlig hälsning|vänliga hälsningar|hälsningar|bästa hälsningar|ha det bra"
        r"|mit freundlichen grüßen|viele grüße|beste grüße|grüße|saludos|un saludo|cordiali saluti|saluti|groeten|met vriendelijke groet)\b[\w ,.!-]{0,30}$",
    )
    #: A line that looks like contact details (phone, URL, address) — the body of a signature block.
    contact: tuple[re.Pattern, ...] = _rx(
        r"(\+|00)\d[\d ().-]{6,}\d",
        r"\b(tel|tél|phone|mobile|mob|portable|cell|fax)\b\.?\s*:?",
        r"https?://|\bwww\.",
        r"[\w.+-]+@[\w-]+\.[\w.]+",
    )
    #: A line that opens a legal disclaimer; it and everything after it are dropped.
    disclaimers: tuple[re.Pattern, ...] = _rx(
        r"\b(this|the) (e-?mail|message)( and any (attachments?|files?))?.{0,80}\b(confidential|privileged|intended (solely |only )?for)\b",
        r"\bconfidentiality notice\b",
        r"\bce (message|courriel|mail)( et (toutes )?(les|ses) pi[èe]ces jointes)?.{0,80}\b(confidentiel|destin[ée] exclusivement)",
        r"\bdetta (e-?post|meddelande).{0,80}\bkonfidentiell",
        r"\bplease consider the environment before printing\b",
        r"\bpensez à l'environnement avant d'imprimer\b",
    )
    #: Short top text that announces answers written inside the quote ("Responses below.").
    inline_announcements: tuple[re.Pattern, ...] = _rx(
        r"\b(see )?(my )?(answers?|responses?|replies|comments?|notes?) (are )?(below|inline|in (red|blue|bold|green))\b",
        r"\b(see|cf\.?) below\b",
        r"\b(réponses?|commentaires?) (ci-dessous|en (rouge|bleu|gras)|dans le texte)\b",
        r"\bvoir ci-dessous\b",
        r"\b(svar|kommentarer) (nedan|i texten|med (rött|blått))\b",
        r"\b(antworten|kommentare) (unten|im text|in rot)\b",
    )
    #: How many lines after ``header_from`` may hold the ``header_field`` that confirms it.
    header_window: int = 4


DEFAULT_MARKERS = Markers()


@dataclass(frozen=True)
class Authored:
    """One email body split into what its sender wrote and everything else."""

    text: str
    quoted: str = ""
    signature: str = ""
    disclaimer: str = ""
    inline: tuple[str, ...] = ()
    forwarded: bool = False
    inline_announced: bool = False
    notes: tuple[str, ...] = field(default=())

    @property
    def full_text(self) -> str:
        """The top text and any recovered inline answers, in order: everything this sender wrote."""
        return "\n\n".join(part for part in (self.text, *self.inline) if part)

    def to_dict(self) -> dict:
        """JSON-ready."""
        return {
            "text": self.text,
            "full_text": self.full_text,
            "inline": list(self.inline),
            "quoted": self.quoted,
            "signature": self.signature,
            "disclaimer": self.disclaimer,
            "forwarded": self.forwarded,
            "inline_announced": self.inline_announced,
            "notes": list(self.notes),
        }


_MAILTO = re.compile(r"<mailto:[^>]*>")
_SPACE = re.compile(r"\s+")
_WORD = re.compile(r"\w+", re.UNICODE)


def _matches(patterns: Iterable[re.Pattern], line: str) -> bool:
    return any(p.search(line) for p in patterns)


def _plain(line: str) -> str:
    """A line with quote markers, bold stars and ``<mailto:…>`` debris removed, for matching."""
    line = _MAILTO.sub("", line).strip()
    line = re.sub(r"^(>\s?)+", "", line).strip()
    return line.strip("*").strip() if line.startswith("*") else line


def _quote_start(lines: Sequence[str], markers: Markers) -> tuple[int, bool] | None:
    """Index of the first line of the quoted region, and whether it is a forward; ``None`` if nothing is quoted."""
    for i, raw in enumerate(lines):
        line = _plain(raw)
        stripped = raw.strip()
        if stripped.startswith(">") and _plain(raw) and _quote_run(lines, i):
            return i, False
        if _matches(markers.forward_headers, line):
            return i, True
        # "On <date>, <name> <address>\nwrote:" wraps; join up to three lines.
        joined = line
        for k in range(i, min(i + 3, len(lines))):
            if k > i:
                joined = f"{joined} {_plain(lines[k])}"
            if _matches(markers.reply_headers, joined):
                return i, False
        if _matches(markers.header_from, line) and any(
            _matches(markers.header_field, _plain(nxt))
            for nxt in lines[i + 1 : i + 1 + markers.header_window]
        ):
            start = (
                i - 1 if i and _matches(markers.separators, lines[i - 1].strip()) else i
            )
            return start, _is_forward_subject(lines[i : i + 1 + markers.header_window])
    return None


def _quote_run(lines: Sequence[str], i: int) -> bool:
    """A ``>`` line starts the quote only if the quoted lines run to the end, give or take a footer."""
    rest = [ln.strip() for ln in lines[i:] if ln.strip()]
    quoted = sum(ln.startswith(">") for ln in rest)
    return quoted >= max(1, int(0.6 * len(rest)))


def _is_forward_subject(block: Sequence[str]) -> bool:
    return any(
        re.match(
            r"^\**(subject|objet|ämne|betreff)\s*:\**\s*(fw|fwd|tr|vb|wg)\s*:",
            _plain(ln),
            re.I,
        )
        for ln in block
    )


def _split_disclaimer(lines: list[str], markers: Markers) -> tuple[list[str], list[str]]:
    for i, line in enumerate(lines):
        if _matches(markers.disclaimers, line):
            start = i
            # Take the whole paragraph the disclaimer begins in.
            while start and lines[start - 1].strip():
                start -= 1
            return lines[:start], lines[start:]
    return lines, []


def _split_signature(lines: list[str], markers: Markers) -> tuple[list[str], list[str]]:
    for i, line in enumerate(lines):
        if _matches(markers.signature_starts, line.strip()):
            return lines[:i], lines[i:]
    # A trailing block of short lines that holds contact details: cut after the sign-off,
    # or else at the first line of the block's contact paragraph.
    end = len(lines)
    while end and not lines[end - 1].strip():
        end -= 1
    start = end
    while start and _short(lines[start - 1]):
        start -= 1
    block = lines[start:end]
    if not any(_matches(markers.contact, ln) for ln in block):
        return lines, []
    for k in range(len(block) - 1, -1, -1):
        if _matches(markers.sign_offs, block[k].strip()):
            cut = start + k + 1
            return lines[:cut], lines[cut:]
    first_contact = next(k for k, ln in enumerate(block) if _matches(markers.contact, ln))
    cut = start + first_contact
    while cut > start and lines[cut - 1].strip():
        cut -= 1
    if cut == start and start == 0:
        return lines, []  # the whole message is short lines; keep it
    return lines[:cut], lines[cut:]


def _short(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return True
    return len(_WORD.findall(stripped)) <= 8 and not re.search(
        r"[.?!:]\s*$", stripped.rstrip(")")
    )


_BULLET = re.compile(r"^([-•*·◦▪–]|\d{1,2}[.)])\s+")
_EMPHASIS = re.compile(r"[*_]")
_QUOTES = str.maketrans(
    {"’": "'", "‘": "'", "“": '"', "”": '"', "«": '"', "»": '"', "\u00a0": " "}
)


def _normalise(text: str) -> str:
    """Lower-cased, whitespace-collapsed text without quote markers, bullets or emphasis, so rewrapped and re-rendered copies compare equal."""
    lines = (_BULLET.sub("", _plain(ln)) for ln in text.translate(_QUOTES).splitlines())
    return _SPACE.sub(" ", _EMPHASIS.sub("", " ".join(lines))).strip().lower()


def _is_header_line(line: str, markers: Markers) -> bool:
    return (
        _matches(markers.header_from, line)
        or _matches(markers.header_field, line)
        or _matches(markers.reply_headers, line)
        or _matches(markers.forward_headers, line)
        or _matches(markers.separators, line)
        or _matches(markers.reply_header_tails, line)
    )


def _inline_additions(
    quoted: list[str], earlier: Sequence[str], markers: Markers, *, min_coverage: float
) -> tuple[tuple[str, ...], str | None]:
    """Paragraphs of the quoted region found in none of the earlier messages, and a note when they cannot be trusted.

    They are trusted only when one earlier message is itself found in the quote (at least
    ``min_coverage`` of its own lines): then the quote's source is known, and whatever the
    quote adds to it was written by this sender.
    """
    corpus = " \n ".join(_normalise(e) for e in earlier)
    quote_norm = _normalise("\n".join(quoted))
    best = 0.0
    for body in earlier:
        own = [
            _normalise(ln)
            for ln in authored(body, markers=markers).text.splitlines()
            if len(_WORD.findall(ln)) >= 3
        ]
        if own:
            best = max(best, sum(ln in quote_norm for ln in own) / len(own))
    if best < min_coverage:
        return (), (
            "inline answers not recovered: none of the earlier messages given was found in "
            f"the quote (best {best:.0%})"
        )
    paragraphs: list[list[str]] = []
    last = None
    heads = _header_heads(quoted, markers)
    for i, raw in enumerate(quoted):
        line = _plain(raw)
        if (
            i in heads
            or not line
            or _is_header_line(line, markers)
            or _normalise(line) in corpus
        ):
            continue
        if last is not None and i == last + 1:
            paragraphs[-1].append(line)
        else:
            paragraphs.append([line])
        last = i
    return tuple("\n".join(p) for p in paragraphs if _substantive(p)), None


def _header_heads(quoted: Sequence[str], markers: Markers) -> set[int]:
    """Lines that open a reply header wrapped onto the next line ("On …, Ada <…>" + "wrote:")."""
    return {
        i
        for i in range(len(quoted) - 1)
        if _matches(markers.reply_header_tails, _plain(quoted[i + 1]))
        and not _matches(markers.reply_header_tails, _plain(quoted[i]))
    }


def _substantive(paragraph: list[str]) -> bool:
    """Not a stray list number or a lone symbol left over from re-rendering."""
    return len(re.findall(r"[^\W\d_]{2,}", " ".join(paragraph))) >= 1


def _marked_inline(quoted: list[str], markers: Markers) -> tuple[str, ...]:
    """Unmarked paragraphs between ``>`` lines: answers written inline in a ``>``-quoting client."""
    content = [ln for ln in quoted[1:] if ln.strip()]
    marked = [ln for ln in content if ln.lstrip().startswith(">")]
    if not marked or len(marked) == len(content) or len(marked) < len(content) / 2:
        return ()
    last_marked = max(i for i, ln in enumerate(quoted) if ln.lstrip().startswith(">"))
    tail, _ = _split_signature(list(quoted[last_marked + 1 :]), markers)
    region = list(quoted[: last_marked + 1]) + tail
    paragraphs: list[list[str]] = [[]]
    heads = _header_heads(quoted, markers)
    for i, ln in enumerate(region[1:], start=1):
        plain = ln.strip()
        if (
            i in heads
            or not plain
            or plain.startswith(">")
            or _is_header_line(_plain(ln), markers)
        ):
            if paragraphs[-1]:
                paragraphs.append([])
            continue
        paragraphs[-1].append(plain)
    return tuple("\n".join(p) for p in paragraphs if p)


def _clean(lines: list[str]) -> str:
    return re.sub(r"\n{3,}", "\n\n", "\n".join(ln.rstrip() for ln in lines)).strip()


def authored(
    text: str,
    *,
    earlier: Iterable[str] = (),
    markers: Markers = DEFAULT_MARKERS,
    min_coverage: float = 0.6,
) -> Authored:
    """Split one email body (plain text) into what its sender wrote and everything else.

    ``earlier`` is the text of messages that came before it in the thread (raw bodies are
    fine); with it, answers written inside the quoted text are recovered into ``inline``.
    They are trusted only when at least ``min_coverage`` of the quoted lines are found in
    ``earlier``, since a thread missing its earlier messages would make every quoted line look new.

    >>> authored("Thanks!\\n\\n-- \\nAda Lovelace\\nAnalytical Engines Ltd").signature
    '--\\nAda Lovelace\\nAnalytical Engines Ltd'
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    found = _quote_start(lines, markers)
    top, quoted = (lines, []) if found is None else (lines[: found[0]], lines[found[0] :])
    forwarded = bool(found and found[1])
    top, disclaimer = _split_disclaimer(top, markers)
    top, signature = _split_signature(top, markers)
    body = _clean(top)
    result = Authored(
        text=body,
        quoted=_clean(quoted),
        signature=_clean(signature),
        disclaimer=_clean(disclaimer),
        forwarded=forwarded,
        inline_announced=bool(body)
        and len(_WORD.findall(body)) <= 40
        and _matches(markers.inline_announcements, body),
    )
    earlier = list(earlier)
    marked = _marked_inline(quoted, markers) if quoted and not forwarded else ()
    if marked:
        result = replace(result, inline=marked)
    elif quoted and earlier and not forwarded:
        inline, note = _inline_additions(
            quoted, earlier, markers, min_coverage=min_coverage
        )
        result = replace(result, inline=inline, notes=(note,) if note else ())
    elif result.inline_announced and not earlier:
        result = replace(
            result,
            notes=(
                "answers are announced inside the quote; pass earlier= to recover them",
            ),
        )
    return result


def authored_thread(
    bodies: Iterable[str],
    *,
    markers: Markers = DEFAULT_MARKERS,
    min_coverage: float = 0.6,
) -> list[Authored]:
    """:func:`authored` for each body of a thread, oldest first, each given every earlier body as ``earlier``.

    >>> first, second = authored_thread([
    ...     "Can we ship Friday?\\nAnd who writes the notes?",
    ...     "Answers below.\\n\\nFrom: Ada <ada@example.org>\\nSent: Monday\\n"
    ...     "Can we ship Friday?\\nYes, Friday works.\\nAnd who writes the notes?\\nGrace does.",
    ... ])
    >>> second.inline
    ('Yes, Friday works.', 'Grace does.')
    """
    seen: list[str] = []
    results = []
    for body in bodies:
        results.append(
            authored(body, earlier=seen, markers=markers, min_coverage=min_coverage)
        )
        seen.append(body)
    return results
