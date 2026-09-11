"""Email: IMAP to read and listen, SMTP to send, with the standard library.

References:

- ``email:``: the whole folder (``INBOX`` unless configured), which is what ``listen`` watches.
- ``email:<address>``: the correspondence with one address: its messages in the folder,
  and sends to it.

Message ids are ``Message-ID`` headers, angle brackets included. A draft's ``reply_to`` is
one, and the send carries ``In-Reply-To`` and ``References``. Reading never marks a message
seen.

Authenticity is ``claimed`` unless the **topmost** ``Authentication-Results`` header was
added by a server you trust (``trusted_authserv_ids``) and records ``dmarc=pass`` for the
From domain: then it is ``domain``. Only the topmost header counts, even when the trusted
server added several: a lower one carrying the same authserv-id may have been written by the
sender (not every server strips those), so a result recorded lower down fails safe as
``claimed``.

>>> Email().parse_ref("Ada@Example.org").encoded
'email:ada@example.org'
"""

from __future__ import annotations

import hashlib
import imaplib
import re
import smtplib
import ssl
import subprocess
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import formatdate, getaddresses, make_msgid, parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any

from correspond.errors import ChannelError, InvalidRef
from correspond.model import (
    Account,
    Attachment,
    Authenticity,
    Capabilities,
    ChannelIdentity,
    ConversationRef,
    Draft,
    Event,
    Grade,
    HistoryDepth,
    Message,
    SendResult,
    Support,
)
from correspond.ops import window, with_final_cursor
from correspond.registry import require, value

__all__ = ["Email", "authenticity"]

NAME = "email"
ADDRESS_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+$"
)
MESSAGE_ID_RE = re.compile(r"^<[^<>\s]+@[^<>\s]+>$")
FETCH_BATCH = 50
DEFAULT_READ_LIMIT = 50
LISTEN_LOOKBACK = timedelta(days=1)
TIMEOUT_S = 60
_UID_RE = re.compile(rb"UID (\d+)")
_MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _imap_date(moment: datetime) -> str:
    return f"{moment.day:02d}-{_MONTHS[moment.month - 1]}-{moment.year}"


def _csv(text: str | None) -> set[str]:
    return {part.strip().lower() for part in (text or "").split(",") if part.strip()}


def _last(response: Any) -> str | None:
    values = [v for v in (response[1] if response else []) if v]
    if not values:
        return None
    found = values[-1]
    return found.decode() if isinstance(found, bytes) else str(found)


class _TextOnly(HTMLParser):
    _BREAKS = {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._hidden += 1
        elif tag in self._BREAKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._hidden:
            self._hidden -= 1

    def handle_data(self, data):
        if not self._hidden:
            self.parts.append(data)


def _html_text(html: str) -> str:
    parser = _TextOnly()
    parser.feed(html)
    parser.close()
    return re.sub(r"\n{3,}", "\n\n", "".join(parser.parts)).strip()


def _addresses(message: EmailMessage, header: str) -> list[tuple[str, str]]:
    return [
        (name, addr.lower())
        for name, addr in getaddresses([str(v) for v in message.get_all(header, [])])
        if addr
    ]


def authenticity(
    message: EmailMessage, trusted_authserv_ids: Iterable[str]
) -> Authenticity:
    """``domain`` only when the topmost Authentication-Results comes from a trusted server and DMARC passed for the From domain."""
    trusted = {t.lower() for t in trusted_authserv_ids}
    senders = _addresses(message, "From")
    results = message.get_all("Authentication-Results") or []
    if not results:
        return Authenticity(
            grade=Grade.CLAIMED, evidence={"authentication_results": "none"}
        )
    top = " ".join(str(results[0]).split())
    authserv_id = (top.split(";", 1)[0].split() or [""])[0].lower()
    evidence: dict[str, Any] = {
        "authserv_id": authserv_id,
        "trusted": authserv_id in trusted,
    }
    if authserv_id not in trusted:
        return Authenticity(
            grade=Grade.CLAIMED,
            evidence={
                **evidence,
                "reason": "the topmost Authentication-Results header is not from a trusted server",
            },
        )
    dmarc = re.search(r"\bdmarc=([a-z]+)", top, re.I)
    header_from = re.search(r"\bheader\.from=\"?([^\s;\"]+)", top, re.I)
    domain = senders[0][1].rpartition("@")[2] if len(senders) == 1 else ""
    evidence["dmarc"] = dmarc.group(1).lower() if dmarc else "none"
    evidence["header_from"] = header_from.group(1).lower() if header_from else ""
    if domain and evidence["dmarc"] == "pass" and evidence["header_from"] == domain:
        return Authenticity(grade=Grade.DOMAIN, evidence=evidence)
    return Authenticity(
        grade=Grade.CLAIMED,
        evidence={**evidence, "reason": "DMARC did not pass for the one From domain"},
    )


def _body(message: EmailMessage) -> tuple[str, str | None, str]:
    part = message.get_body(preferencelist=("plain", "html"))
    if part is None:
        return "", None, "plain"
    try:
        content = part.get_content()
    except (LookupError, UnicodeError):
        content = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
    if part.get_content_type() == "text/html":
        return _html_text(content), content, "html"
    return content.strip(), content, "plain"


class Email:
    """A mailbox over IMAP and SMTP."""

    name = NAME

    def __init__(
        self,
        *,
        imap: Callable[[], Any] | None = None,
        smtp: Callable[[], Any] | None = None,
        run: Callable[..., Any] = subprocess.run,
    ):
        self.run = run
        self._imap_factory = imap or self._connect_imap
        self._smtp_factory = smtp or self._connect_smtp

    @property
    def capabilities(self) -> Capabilities:
        """Read and listen to one folder; send with threading headers."""
        return Capabilities(
            channel=NAME,
            read=Support.FULL,
            listen=Support.FULL,
            send=Support.FULL,
            initiate=Support.FULL,
            reply=Support.FULL,
            priority=Support.PARTIAL,
            history_depth=HistoryDepth.FULL,
            listen_modes=("poll",),
            grades=(Grade.DOMAIN, Grade.CLAIMED),
            formats=("plain", "html"),
            native_fields=("subject", "to", "cc", "folder", "uid", "references"),
            notes=(
                "read and listen look at one folder (INBOX by default); your own sent mail is in another",
                "listen polls by UID; IMAP IDLE is not used",
                "priority sets Importance and X-Priority, which mail clients may ignore",
            ),
        )

    def parse_ref(self, id: str) -> ConversationRef:
        """``""`` for the folder, or an address."""
        folder = ConversationRef(channel=NAME, id="", kind="folder")
        if id == "":
            return folder
        if not ADDRESS_RE.match(id):
            raise InvalidRef(
                f"email references are email: (the folder) or email:<address>, not email:{id}"
            )
        return ConversationRef(channel=NAME, id=id.lower(), kind="address", parent=folder)

    # ---------------------------------------------------------------- connections

    def _connect_imap(self) -> imaplib.IMAP4:
        host = require(NAME, "imap_host", run=self.run)
        port = int(value(NAME, "imap_port") or 993)
        user = require(NAME, "user", run=self.run)
        password = require(NAME, "password", run=self.run)
        try:
            conn = imaplib.IMAP4_SSL(
                host, port, ssl_context=ssl.create_default_context(), timeout=TIMEOUT_S
            )
        except (OSError, imaplib.IMAP4.error) as error:
            raise ChannelError(
                f"could not reach the IMAP server {host}: {error}",
                kind="network",
                retryable=True,
            ) from None
        try:
            conn.login(user, password)
        except imaplib.IMAP4.error as error:
            try:
                conn.logout()
            except (OSError, imaplib.IMAP4.error):
                pass
            raise ChannelError(
                f"the IMAP server refused the login for {user}: {error}", kind="auth"
            ) from None
        return conn

    @contextmanager
    def _imap(self) -> Iterator[Any]:
        conn = self._imap_factory()
        try:
            yield conn
        except imaplib.IMAP4.abort as error:
            raise ChannelError(
                f"the IMAP connection dropped: {error}", kind="network", retryable=True
            ) from None
        except imaplib.IMAP4.error as error:
            raise ChannelError(
                f"the IMAP server said: {error}", kind="unavailable"
            ) from None
        except (OSError, TimeoutError) as error:
            raise ChannelError(
                f"IMAP failed: {error}", kind="network", retryable=True
            ) from None
        finally:
            try:
                conn.logout()
            except (OSError, imaplib.IMAP4.error):
                pass

    def _connect_smtp(self) -> smtplib.SMTP:
        host = require(NAME, "smtp_host", run=self.run)
        port = int(value(NAME, "smtp_port") or 465)
        user = require(NAME, "user", run=self.run)
        password = require(NAME, "password", run=self.run)
        context = ssl.create_default_context()
        try:
            if port == 465:
                server = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT_S, context=context)
            else:
                server = smtplib.SMTP(host, port, timeout=TIMEOUT_S)
                server.starttls(context=context)
            server.login(user, password)
        except smtplib.SMTPAuthenticationError as error:
            raise ChannelError(
                f"the SMTP server refused the login for {user}: {error.smtp_code}",
                kind="auth",
            ) from None
        except (OSError, smtplib.SMTPException) as error:
            raise ChannelError(
                f"could not use the SMTP server {host}: {error}",
                kind="network",
                retryable=True,
            ) from None
        return server

    @contextmanager
    def _smtp(self) -> Iterator[Any]:
        server = self._smtp_factory()
        try:
            yield server
        except smtplib.SMTPRecipientsRefused as error:
            raise ChannelError(
                f"the server refused the recipient: {error.recipients}", kind="validation"
            ) from None
        except smtplib.SMTPSenderRefused as error:
            raise ChannelError(
                f"the server refused the sender: {error.smtp_error!r}", kind="permission"
            ) from None
        except smtplib.SMTPDataError as error:
            transient = 400 <= error.smtp_code < 500
            raise ChannelError(
                f"the server refused the message ({error.smtp_code})",
                kind="unavailable" if transient else "validation",
                retryable=transient,
            ) from None
        except smtplib.SMTPServerDisconnected as error:
            raise ChannelError(
                f"the SMTP server disconnected: {error}", kind="network", retryable=True
            ) from None
        except smtplib.SMTPException as error:
            raise ChannelError(f"SMTP failed: {error}", kind="unavailable") from None
        except OSError as error:
            raise ChannelError(
                f"SMTP failed: {error}", kind="network", retryable=True
            ) from None
        finally:
            try:
                server.quit()
            except (OSError, smtplib.SMTPException):
                pass

    # ------------------------------------------------------------------ IMAP work

    @staticmethod
    def _select(conn: Any, folder: str) -> tuple[str, int | None]:
        typ, data = conn.select(_quote(folder), readonly=True)
        if typ != "OK":
            raise ChannelError(
                f"the folder {folder!r} could not be opened: {data!r}", kind="not_found"
            )
        validity = _last(conn.response("UIDVALIDITY")) or "0"
        uidnext = _last(conn.response("UIDNEXT"))
        return validity, int(uidnext) if uidnext and uidnext.isdigit() else None

    @staticmethod
    def _search(conn: Any, criteria: list[str]) -> list[int]:
        typ, data = conn.uid("SEARCH", *criteria)
        if typ != "OK":
            raise ChannelError(f"the IMAP search failed: {data!r}", kind="unavailable")
        return sorted(int(x) for x in ((data or [b""])[0] or b"").split())

    @staticmethod
    def _fetch(conn: Any, uids: list[int]) -> list[tuple[int, bytes]]:
        found: list[tuple[int, bytes]] = []
        for start in range(0, len(uids), FETCH_BATCH):
            chunk = uids[start : start + FETCH_BATCH]
            typ, data = conn.uid(
                "FETCH", ",".join(str(u) for u in chunk), "(UID BODY.PEEK[])"
            )
            if typ != "OK":
                raise ChannelError(f"the IMAP fetch failed: {data!r}", kind="unavailable")
            data = data or []
            for index, item in enumerate(data):
                if not isinstance(item, tuple) or len(item) < 2:
                    continue
                match = _UID_RE.search(item[0])
                if (
                    match is None
                    and index + 1 < len(data)
                    and isinstance(data[index + 1], bytes)
                ):
                    match = _UID_RE.search(data[index + 1])
                if match:
                    found.append((int(match.group(1)), item[1]))
        found.sort(key=lambda pair: pair[0])
        return found

    def _message(
        self, uid: int, raw: bytes, *, folder: str, user: str, trusted: set[str]
    ) -> Message:
        parsed: EmailMessage = BytesParser(policy=policy.default).parsebytes(raw)
        senders = _addresses(parsed, "From")
        name, address = senders[0] if senders else ("", "")
        to = [a for _, a in _addresses(parsed, "To")]
        cc = [a for _, a in _addresses(parsed, "Cc")]
        is_self = bool(user) and address == user.lower()
        counterpart = to[0] if is_self and to else address
        conversation = (
            self.parse_ref(counterpart)
            if counterpart and ADDRESS_RE.match(counterpart)
            else self.parse_ref("")
        )
        message_id = (
            str(parsed.get("Message-ID") or "").strip()
            or f"<uid-{uid}@correspond.invalid>"
        )
        try:
            sent_at = (
                parsedate_to_datetime(str(parsed["Date"])) if parsed["Date"] else None
            )
        except (TypeError, ValueError):
            sent_at = None
        if sent_at is None:
            sent_at = datetime.fromtimestamp(0, tz=timezone.utc)
        elif sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=timezone.utc)
        text, body, body_format = _body(parsed)
        references = str(parsed.get("References") or "").split()
        attachments = []
        for index, part in enumerate(parsed.iter_attachments()):
            data = part.get_payload(decode=True) or b""
            attachments.append(
                Attachment(
                    ref=f"imap:{uid}/{index}",
                    media_type=part.get_content_type(),
                    name=part.get_filename(),
                    size=len(data),
                    sha256=hashlib.sha256(data).hexdigest(),
                    loader=lambda data=data: data,
                )
            )
        return Message(
            id=message_id,
            conversation=conversation,
            author=ChannelIdentity(
                channel=NAME,
                native_id=address,
                handle=address or None,
                display_name=name or None,
                is_self=is_self,
            ),
            authenticity=authenticity(parsed, trusted),
            sent_at=sent_at,
            text=text,
            body=body,
            body_format=body_format,
            attachments=tuple(attachments),
            reply_to=str(parsed.get("In-Reply-To") or "").strip() or None,
            thread_root=references[0] if references else None,
            native={
                "subject": str(parsed.get("Subject") or ""),
                "to": to,
                "cc": cc,
                "folder": folder,
                "uid": uid,
                "references": references,
            },
        )

    def _context(self) -> tuple[str, str, set[str]]:
        user = value(NAME, "user") or ""
        return (
            value(NAME, "folder") or "INBOX",
            user,
            _csv(value(NAME, "trusted_authserv_ids")),
        )

    def read(
        self,
        ref: ConversationRef,
        *,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[Message]:
        """Messages in the folder (from ``ref``'s address, if it has one), oldest first; never marked seen."""
        folder, user, trusted = self._context()
        criteria = ["FROM", _quote(ref.id)] if ref.id else ["ALL"]
        if since is not None:
            criteria += ["SINCE", _imap_date(since)]
        with self._imap() as conn:
            self._select(conn, folder)
            uids = self._search(conn, criteria)[-(limit or DEFAULT_READ_LIMIT) :]
            fetched = self._fetch(conn, uids)
        messages = [
            self._message(uid, raw, folder=folder, user=user, trusted=trusted)
            for uid, raw in fetched
        ]
        return window(messages, since=since, limit=limit)

    def poll(
        self, ref: ConversationRef, *, cursor: str | None = None, limit: int | None = None
    ):
        """New messages since ``cursor`` (``<uidvalidity>:<uid>``); a first poll, or a changed UIDVALIDITY, looks back a day."""
        folder, user, trusted = self._context()
        seen_validity, _, seen_uid = (cursor or "").partition(":")
        with self._imap() as conn:
            validity, uidnext = self._select(conn, folder)
            resume = bool(cursor) and seen_validity == validity and seen_uid.isdigit()
            if resume:
                criteria = ["UID", f"{int(seen_uid) + 1}:*"]
            else:
                criteria = [
                    "SINCE",
                    _imap_date(datetime.now(timezone.utc) - LISTEN_LOOKBACK),
                ]
            if ref.id:
                criteria += ["FROM", _quote(ref.id)]
            # "n:*" always includes the highest UID, even when it is below n.
            uids = [
                u for u in self._search(conn, criteria) if not resume or u > int(seen_uid)
            ]
            truncated = bool(limit) and len(uids) > limit
            if limit:
                uids = uids[:limit]
            fetched = self._fetch(conn, uids)
        events = [
            Event(
                kind="message.created",
                channel=NAME,
                delivery_id=f"email:{folder}:{validity}:{uid}",
                cursor=f"{validity}:{uid}",
                message=self._message(
                    uid, raw, folder=folder, user=user, trusted=trusted
                ),
                payload={"folder": folder, "uid": uid},
            )
            for uid, raw in fetched
        ]
        final = None
        if not truncated:
            if uids:
                final = f"{validity}:{uids[-1]}"
            elif not resume and uidnext:
                final = f"{validity}:{uidnext - 1}"
        return with_final_cursor(events, final)

    def send(
        self, ref: ConversationRef, draft: Draft, *, dry_run: bool = False
    ) -> SendResult:
        """Send to ``ref``'s address, with ``In-Reply-To`` and ``References`` when replying."""
        if not ref.id:
            raise ChannelError("send to an address: email:<address>", kind="validation")
        if draft.reply_to and not MESSAGE_ID_RE.match(draft.reply_to):
            raise ChannelError(
                f"reply_to is a Message-ID such as <abc@example.org>, not {draft.reply_to!r}",
                kind="validation",
            )
        user = value(NAME, "user") if dry_run else require(NAME, "user", run=self.run)
        sender = value(NAME, "from_address") or user or "(not set)"
        message = EmailMessage()
        try:
            message["From"] = sender
            message["To"] = ref.id
            if draft.title:
                message["Subject"] = " ".join(draft.title.split())
            message["Date"] = formatdate(usegmt=True)
            message["Message-ID"] = make_msgid(domain=sender.rpartition("@")[2] or None)
            if draft.reply_to:
                message["In-Reply-To"] = draft.reply_to
                message["References"] = draft.reply_to
            if draft.priority in ("high", "urgent"):
                message["Importance"], message["X-Priority"] = "high", "1"
            elif draft.priority == "low":
                message["Importance"], message["X-Priority"] = "low", "5"
            message.set_content(draft.text)
        except (ValueError, TypeError) as error:
            raise ChannelError(
                f"the message could not be built: {error}", kind="validation"
            ) from None
        plan = {
            "action": "send an email",
            "from": sender,
            "to": ref.id,
            "subject": draft.title,
            "in_reply_to": draft.reply_to,
            "priority": draft.priority,
            "server": value(NAME, "smtp_host") or "(not set)",
            "text": draft.text,
        }
        if dry_run:
            return SendResult(
                ok=True, channel=NAME, conversation=ref.encoded, dry_run=True, plan=plan
            )
        with self._smtp() as server:
            server.send_message(message)
        return SendResult(
            ok=True,
            channel=NAME,
            conversation=ref.encoded,
            message_id=str(message["Message-ID"]),
            account=Account(channel=NAME, id=user, acts_as="user"),
            plan=plan,
        )
