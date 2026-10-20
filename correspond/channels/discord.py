"""Discord: a bot reading and posting in channels over REST, through discorddol.

Reference: ``discord:<channel id>``, a channel or a thread (Discord numbers both with
snowflakes). A bot reads only the channels its server roles let it see, and since Discord
made message content a privileged intent, it reads the text of a message only when the
Message Content intent is enabled for it (Developer Portal, Bot, Privileged Gateway
Intents; a bot in more than 100 servers needs Discord's approval) or when the message
mentions it.

- ``read`` fetches the channel's history (the latest 50 messages unless ``limit`` says).
- ``listen`` polls the channel for messages newer than the last one seen (the cursor is
  that message's id; a first poll looks back a day). It does not open the gateway, so it
  sees new messages, not edits or deletions, and one channel per reference.
- ``send`` posts a message (2,000 characters at most); a title is prepended in bold. With
  ``reply_to`` it is a Discord reply to that message (its author is not pinged). Only
  the users it mentions as ``<@id>`` are pinged: ``@everyone``, ``@here`` and role
  mentions stay text, through Discord's allowed mentions.
- ``verify`` grades an HTTP interaction Discord posted to your endpoint: ``crypto`` when
  its Ed25519 signature (``X-Signature-Ed25519`` over ``X-Signature-Timestamp`` + body)
  checks against the application's public key, ``forged`` otherwise. Discord sends
  deliberately invalid signatures to test the endpoint; those must come back ``forged``
  (answer them 401).

Senders are ``platform``: Discord authenticated the account, and correspond fetched the
message from the API itself. A server member's role ids are its ``authority``. The bot token never appears in an error or a plan.

Needs the ``discord`` extra (discorddol, over discord.py in REST-only mode), imported on
first use, not when the channel is listed.

>>> Discord().parse_ref("123456789012345678").encoded
'discord:123456789012345678'
"""

from __future__ import annotations

import mimetypes
import re
import subprocess
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from correspond.channels._http import urllib_http
from correspond.errors import ChannelError, InvalidRef, MissingRequirement, NotSupported
from correspond.model import (
    Attachment,
    Audience,
    Authenticity,
    Capabilities,
    ChannelIdentity,
    ConversationRef,
    Draft,
    Event,
    Grade,
    HistoryDepth,
    Message,
    Scope,
    SendResult,
    Support,
    parse_time,
)
from correspond.ops import window, with_final_cursor
from correspond.registry import require

__all__ = ["Discord", "MAX_TEXT_CHARS"]

NAME = "discord"
#: A Discord snowflake: a channel, thread, message or user id.
SNOWFLAKE_MAX_DIGITS = 20
MAX_TEXT_CHARS = 2000
#: Discord's cap on the users one message may ping (allowed_mentions.users).
MAX_PINGED_USERS = 100
READ_DEFAULT = 50
LISTEN_LOOKBACK = timedelta(days=1)
#: How far behind the clock a quiet first poll sets its cursor.
CLOCK_SKEW = timedelta(seconds=5)
#: Discord's epoch, in milliseconds since 1970: a snowflake's top bits count from it.
DISCORD_EPOCH_MS = 1_420_070_400_000
#: What would notify a whole server or a role: kept as text, never a ping.
MASS_MENTION_RE = re.compile(r"@(?:everyone|here)\b|<@&[0-9]+>")
#: How old an interaction's timestamp may be before its signature counts as a replay.
SIGNATURE_MAX_AGE_S = 300
#: An Ed25519 signature is 64 bytes, its public key 32, both sent as hex.
ED25519_SIGNATURE_HEX = 128
ED25519_PUBLIC_KEY_HEX = 64
#: Digits a Unix timestamp in seconds can plausibly have; longer is refused before parsing.
TIMESTAMP_MAX_DIGITS = 12
#: HTTP statuses, as correspond's error kinds (discord.py's HTTPException carries ``status``).
_STATUS_KINDS = {
    400: "validation",
    401: "auth",
    403: "permission",
    404: "not_found",
    429: "rate_limited",
}
#: Exceptions discorddol raises by name, without a status.
_NAMED_KINDS = {
    "Forbidden": "permission",
    "NotFound": "not_found",
    "LoginFailure": "auth",
}
_PLATFORM = Authenticity(
    grade=Grade.PLATFORM, evidence={"attested_by": "the Discord API (REST)"}
)


def _snowflake(text: str, what: str) -> str:
    value = str(text or "").strip()
    if (
        not (value.isdigit() and 1 <= len(value) <= SNOWFLAKE_MAX_DIGITS)
        or value[0] == "0"
    ):
        raise InvalidRef(
            f"discord references are discord:<channel id> (a number), not discord:{text}"
            if what == "channel"
            else f"a Discord {what} id is a number, not {text!r}"
        )
    return value


def snowflake_at(moment: datetime) -> str:
    """The smallest snowflake Discord could mint at ``moment``: a message id as a time cursor.

    >>> snowflake_at(datetime(2015, 1, 1, tzinfo=timezone.utc))
    '0'
    """
    ms = int(moment.timestamp() * 1000) - DISCORD_EPOCH_MS
    return str(max(0, ms) << 22)


def _bold(title: str) -> str:
    """``title`` as one bold line, its own markdown escaped."""
    flat = " ".join(title.split())
    return "**" + re.sub(r"([\\*_~`|>])", r"\\\1", flat) + "**"


class Discord:
    """A Discord bot over REST: read a channel, poll it for new messages, post to it."""

    name = NAME

    def __init__(
        self,
        *,
        backend: Any = None,
        http: Callable[..., Any] = urllib_http,
        run: Callable[..., Any] = subprocess.run,
        clock: Callable[[], float] = time.time,
    ):
        self.run = run
        self.clock = clock
        self.http = http
        self._backend = backend

    @property
    def backend(self) -> Any:
        """discorddol's REST backend with the configured bot token, or the one passed in."""
        if self._backend is None:
            token = require(NAME, "token", run=self.run)
            try:
                from discorddol import DiscordRest
            except ImportError:
                raise MissingRequirement(
                    NAME,
                    "discorddol is not installed",
                    fix="pip install 'correspond[discord]'",
                ) from None
            self._backend = DiscordRest(token=token)
        return self._backend

    @property
    def capabilities(self) -> Capabilities:
        """A bot's view of Discord, over REST."""
        return Capabilities(
            channel=NAME,
            read=Support.FULL,
            listen=Support.PARTIAL,
            send=Support.FULL,
            audience=Support.PARTIAL,
            initiate=Support.FULL,
            reply=Support.FULL,
            verify=Support.FULL,
            history_depth=HistoryDepth.FULL,
            listen_modes=("poll",),
            grades=(Grade.PLATFORM, Grade.CRYPTO),
            max_text_length=MAX_TEXT_CHARS,
            formats=("markdown",),
            native_fields=(
                "channel_id",
                "jump_url",
                "pinned",
                "type",
                "embeds",
                "role_ids",
            ),
            rate_limits=(
                "about 5 messages per 5 seconds per channel; discord.py waits out a 429 itself",
                "10,000 invalid requests (401, 403, 429) in 10 minutes gets the bot's address banned for a while: do not poll a channel the bot may not read in a loop",
                "every call logs in over REST and opens a session (no gateway identify is spent)",
            ),
            notes=(
                "a bot reads message text only with the Message Content intent (Developer Portal; Discord's approval above 100 servers) or when the message mentions it; otherwise the text is empty",
                "listen polls one channel over REST for messages newer than the cursor: it sees new messages, not edits or deletions; the gateway is not used",
                "send posts to a channel the bot may write to; a title is prepended in bold; with reply_to it is a Discord reply (the replied-to author is not pinged: mention them as <@id> to ping them)",
                "only user mentions <@id> ping (listed in the plan); @everyone, @here and role mentions stay text through Discord's allowed mentions (listed in the plan as suppressed)",
                "an author's authority is the server role ids the message carried (none in a DM or from a webhook)",
                "an attachment is fetched from Discord's CDN by its signed link, which expires after about a day: read the message again for a fresh one",
                "audience is the channel's server members who can view it, never listed",
                "verify grades an HTTP interaction crypto when its Ed25519 signature checks against the application's public key (DISCORD_PUBLIC_KEY), forged otherwise, including Discord's deliberate invalid-signature probes and timestamps older than five minutes",
            ),
        )

    def parse_ref(self, id: str) -> ConversationRef:
        """A channel or thread id."""
        return ConversationRef(channel=NAME, id=_snowflake(id, "channel"), kind="channel")

    # ------------------------------------------------------------------ plumbing

    def _call(self, what: str, call: Callable[[], Any]) -> Any:
        """One backend call, its failures as correspond's error kinds."""
        try:
            return call()
        except ChannelError:
            raise
        except Exception as error:
            status = getattr(error, "status", None)
            if isinstance(status, int):
                kind = _STATUS_KINDS.get(
                    status, "unavailable" if status >= 500 else "validation"
                )
                retryable = kind == "rate_limited" or status >= 500
            elif type(error).__name__ in _NAMED_KINDS:
                kind, retryable = _NAMED_KINDS[type(error).__name__], False
            elif isinstance(error, (TypeError, ValueError)):
                kind, retryable = (
                    "validation",
                    False,
                )  # e.g. a forum channel has no history
            else:
                kind, retryable = (
                    "unavailable",
                    False,
                )  # not Discord's answer: retrying will not help
            raise ChannelError(
                f"Discord {what}: {error}", kind=kind, retryable=retryable
            ) from None

    def _attachment(self, data: Mapping[str, Any]) -> Attachment:
        url, name = data.get("url"), data.get("filename")

        def load(url=url) -> bytes:
            if not url:
                raise ChannelError(
                    "this attachment came without a link", kind="not_found"
                )
            reply = self.http("GET", url)
            if reply.status != 200:
                raise ChannelError(
                    f"Discord's CDN answered {reply.status} for an attachment (links expire after about a day: read the message again)",
                    kind="not_found" if reply.status in (403, 404) else "unavailable",
                )
            return reply.body

        return Attachment(
            ref=f"discord-attachment:{data.get('id')}",
            media_type=mimetypes.guess_type(name or "")[0] or "application/octet-stream",
            name=name,
            size=data.get("size"),
            loader=load,
        )

    def _message(self, conversation: ConversationRef, data: Mapping[str, Any]) -> Message:
        author = data.get("author") or {}
        role_ids = [str(r) for r in author.get("role_ids") or ()]
        return Message(
            id=str(data["id"]),
            conversation=conversation,
            author=ChannelIdentity(
                channel=NAME,
                native_id=str(author.get("id") or ""),
                handle=author.get("name") or None,
                display_name=author.get("display_name") or None,
                is_bot=bool(author.get("bot")),
                authority=("roles " + ", ".join(role_ids)) if role_ids else None,
            ),
            authenticity=_PLATFORM,
            sent_at=parse_time(data.get("created_at")),
            edited_at=parse_time(data.get("edited_at")),
            text=data.get("content") or "",
            body_format="markdown",
            url=data.get("jump_url"),
            reply_to=data.get("reply_to") or None,
            attachments=tuple(self._attachment(a) for a in data.get("attachments") or ()),
            native={
                "channel_id": str(data.get("channel_id") or conversation.id),
                "jump_url": data.get("jump_url"),
                "pinned": bool(data.get("pinned")),
                "type": data.get("type"),
                "embeds": list(data.get("embeds") or ()),
                "role_ids": role_ids,
            },
            raw=dict(data),
        )

    # ------------------------------------------------------------------ reading

    def read(
        self,
        ref: ConversationRef,
        *,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[Message]:
        """The channel's latest messages (``limit``, else 50), oldest first; with ``since``, only those after it."""
        count = max(1, int(limit)) if limit else READ_DEFAULT
        found = self._call(
            f"history of channel {ref.id}",
            # Newest first, so a limit keeps the latest; window puts them in order.
            lambda: self.backend.messages(
                ref.id, limit=count, after=since, oldest_first=False
            ),
        )
        messages = [self._message(ref, data) for data in found or ()]
        return window(messages, since=since, limit=count)

    def poll(
        self, ref: ConversationRef, *, cursor: str | None = None, limit: int | None = None
    ):
        """Messages newer than ``cursor`` (a message id), oldest first; a first poll looks back a day."""
        if cursor is not None and str(cursor).strip():
            after: Any = _snowflake(cursor, "message")
        else:
            after = datetime.now(timezone.utc) - LISTEN_LOOKBACK
        count = max(1, int(limit)) if limit else None
        found = self._call(
            f"history of channel {ref.id}",
            lambda: self.backend.messages(
                ref.id, limit=count, after=after, oldest_first=True
            ),
        )
        events = []
        for data in found or ():
            message = self._message(ref, data)
            events.append(
                Event(
                    kind="message.created",
                    channel=NAME,
                    delivery_id=f"discord:{ref.id}:{message.id}",
                    cursor=message.id,
                    message=message,
                )
            )
        if events:
            final = events[-1].cursor
        elif not (cursor and str(cursor).strip()):
            # A quiet first poll still starts the next one from now, not from a day back again.
            final = snowflake_at(datetime.now(timezone.utc) - CLOCK_SKEW)
        else:
            final = None
        return with_final_cursor(events, final)

    # ----------------------------------------------------------------- audience

    def audience(self, ref: ConversationRef, *, draft: Draft | None = None) -> Audience:
        """The members of the channel's server who can view it: a group nobody lists here."""
        return Audience(
            ref=ref.encoded,
            scope=Scope.GROUP,
            complete=False,
            classes=(
                "the server's members whose roles let them view the channel (or the thread's members)",
                "bots and integrations in the server that can read it",
            ),
            durability=("copies_pushed", "edit_history_visible"),
            widening=("joiners_read_history", "forwarding", "visibility_flip"),
            evidence=(
                "a Discord channel is read by the members of its server its permission overwrites allow; correspond does not list them",
            ),
        )

    # ----------------------------------------------------------------- verifying

    def _public_key(self) -> Any:
        hex_key = require(NAME, "public_key", run=self.run).strip()
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                Ed25519PublicKey,
            )
        except ImportError:
            raise MissingRequirement(
                NAME,
                "cryptography is not installed (it checks Ed25519 signatures)",
                fix="pip install 'correspond[discord]'",
            ) from None
        try:
            if len(hex_key) != ED25519_PUBLIC_KEY_HEX:
                raise ValueError
            return Ed25519PublicKey.from_public_bytes(bytes.fromhex(hex_key))
        except ValueError:
            raise MissingRequirement(
                NAME,
                f"the application public key is not {ED25519_PUBLIC_KEY_HEX} hex characters",
                fix="copy it again from the Developer Portal, General Information, Public Key",
            ) from None

    def verify(self, headers: Mapping[str, str], body: bytes) -> Authenticity:
        """``crypto`` when ``X-Signature-Ed25519`` checks over a fresh ``X-Signature-Timestamp`` + body, ``forged`` otherwise.

        Raises :class:`~correspond.errors.MissingRequirement` when the public key is unset
        or unusable (a configuration error, never a verdict on the request). Replay
        protection is the five-minute window only: a captured request can be replayed
        within it, so act idempotently on an interaction's id.
        """
        key = self._public_key()
        from cryptography.exceptions import InvalidSignature

        def forged(reason: str) -> Authenticity:
            return Authenticity(grade=Grade.FORGED, evidence={"signature": reason})

        if not isinstance(headers, Mapping):
            return forged("no headers")
        if not isinstance(body, (bytes, bytearray, memoryview)):
            return forged("body is not bytes")
        lowered = {str(k).lower(): str(v).strip() for k, v in headers.items()}
        stamp = lowered.get("x-signature-timestamp", "")
        signature = lowered.get("x-signature-ed25519", "")
        if not (
            stamp.isascii() and stamp.isdigit() and len(stamp) <= TIMESTAMP_MAX_DIGITS
        ):
            return forged("no timestamp")
        if not signature:
            return forged("missing")
        try:
            if len(signature) != ED25519_SIGNATURE_HEX or not signature.isascii():
                raise ValueError
            raw = bytes.fromhex(signature)
        except ValueError:
            return forged("malformed")
        try:
            key.verify(raw, stamp.encode() + bytes(body))
        except InvalidSignature:
            return forged("mismatch")
        # Checked after the signature, so a stale verdict is never given to a forgery.
        if abs(self.clock() - int(stamp)) > SIGNATURE_MAX_AGE_S:
            return forged("stale (a replay?)")
        return Authenticity(grade=Grade.CRYPTO, evidence={"signature": "valid"})

    # ------------------------------------------------------------------ writing

    def send(
        self, ref: ConversationRef, draft: Draft, *, dry_run: bool = False
    ) -> SendResult:
        """Post ``draft.text`` (the title, if any, prepended in bold) to the channel."""
        reply_to = _snowflake(draft.reply_to, "message") if draft.reply_to else None
        if draft.notify:
            raise NotSupported(
                "notify",
                NAME,
                alternatives=("mention the user in the text as <@user id>",),
            )
        text = f"{_bold(draft.title)}\n\n{draft.text}" if draft.title else draft.text
        if len(text) > MAX_TEXT_CHARS:
            raise ChannelError(
                f"the text is {len(text)} characters; Discord accepts {MAX_TEXT_CHARS}",
                kind="validation",
            )
        mentions = sorted(set(re.findall(r"<@!?([0-9]+)>", text)))
        plan = {
            "action": "reply to a message" if reply_to else "post a message",
            "channel_id": ref.id,
            "text": text,
            "title_prepended": bool(draft.title),
            "mentions": mentions,
            "suppressed_mentions": sorted(set(MASS_MENTION_RE.findall(text))),
        }
        if reply_to:
            plan["reply_to"] = reply_to
        if len(mentions) > MAX_PINGED_USERS:
            raise ChannelError(
                f"the text mentions {len(mentions)} users; Discord pings at most {MAX_PINGED_USERS} per message",
                kind="validation",
            )
        allowed = {
            "everyone": False,
            "roles": False,
            "users": mentions,
            "replied_user": False,
        }
        if dry_run:
            return SendResult(
                ok=True, channel=NAME, conversation=ref.encoded, dry_run=True, plan=plan
            )
        posted = self._call(
            f"post to channel {ref.id}",
            lambda: self.backend.send_message(
                ref.id, text, allowed_mentions=allowed, reply_to=reply_to
            ),
        )
        return SendResult(
            ok=True,
            channel=NAME,
            conversation=ref.encoded,
            message_id=str((posted or {}).get("id") or "") or None,
            url=(posted or {}).get("jump_url"),
            plan=plan,
        )
