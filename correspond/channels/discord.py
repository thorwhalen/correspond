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
- ``send`` posts a message (2,000 characters at most); a title is prepended in bold.

Senders are ``platform``: Discord authenticated the account, and correspond fetched the
message from the API itself. The bot token never appears in an error or a plan.

Needs the ``discord`` extra (discorddol, over discord.py in REST-only mode), imported on
first use, not when the channel is listed.

>>> Discord().parse_ref("123456789012345678").encoded
'discord:123456789012345678'
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

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
READ_DEFAULT = 50
LISTEN_LOOKBACK = timedelta(days=1)
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


class Discord:
    """A Discord bot over REST: read a channel, poll it for new messages, post to it."""

    name = NAME

    def __init__(
        self,
        *,
        backend: Any = None,
        run: Callable[..., Any] = subprocess.run,
    ):
        self.run = run
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
            reply=Support.NONE,
            history_depth=HistoryDepth.FULL,
            listen_modes=("poll",),
            grades=(Grade.PLATFORM,),
            max_text_length=MAX_TEXT_CHARS,
            formats=("markdown",),
            native_fields=("channel_id", "jump_url", "pinned", "type", "embeds"),
            rate_limits=(
                "about 5 messages per 5 seconds per channel; a bot that identifies more than 1,000 times a day has its token reset",
            ),
            notes=(
                "a bot reads message text only with the Message Content intent (Developer Portal; Discord's approval above 100 servers) or when the message mentions it; otherwise the text is empty",
                "listen polls one channel over REST for messages newer than the cursor: it sees new messages, not edits or deletions; the gateway is not used",
                "send posts to a channel the bot may write to; a title is prepended in bold; replying to a message is not supported yet",
                "audience is the channel's server members who can view it, never listed",
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
            name = type(error).__name__
            kind = {
                "Forbidden": "permission",
                "NotFound": "not_found",
                "LoginFailure": "auth",
                "RateLimited": "rate_limited",
            }.get(name, "unavailable")
            raise ChannelError(
                f"Discord {what}: {error}",
                kind=kind,
                retryable=kind in ("rate_limited", "unavailable"),
            ) from None

    def _message(self, conversation: ConversationRef, data: Mapping[str, Any]) -> Message:
        author = data.get("author") or {}
        return Message(
            id=str(data["id"]),
            conversation=conversation,
            author=ChannelIdentity(
                channel=NAME,
                native_id=str(author.get("id") or ""),
                handle=author.get("name") or None,
                display_name=author.get("display_name") or None,
                is_bot=bool(author.get("bot")),
            ),
            authenticity=_PLATFORM,
            sent_at=parse_time(data.get("created_at")),
            edited_at=parse_time(data.get("edited_at")),
            text=data.get("content") or "",
            body_format="markdown",
            url=data.get("jump_url"),
            reply_to=data.get("reply_to") or None,
            attachments=tuple(
                Attachment(
                    ref=f"discord-attachment:{a.get('id')}",
                    name=a.get("filename"),
                    size=a.get("size"),
                )
                for a in data.get("attachments") or ()
            ),
            native={
                "channel_id": str(data.get("channel_id") or conversation.id),
                "jump_url": data.get("jump_url"),
                "pinned": bool(data.get("pinned")),
                "type": data.get("type"),
                "embeds": list(data.get("embeds") or ()),
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
            lambda: self.backend.messages(
                ref.id, limit=count, after=since, oldest_first=since is not None
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
        return with_final_cursor(events, events[-1].cursor if events else None)

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
            widening=("joiners_read_history", "forwarding"),
            evidence=(
                "a Discord channel is read by the members of its server its permission overwrites allow; correspond does not list them",
            ),
        )

    # ------------------------------------------------------------------ writing

    def send(
        self, ref: ConversationRef, draft: Draft, *, dry_run: bool = False
    ) -> SendResult:
        """Post ``draft.text`` (the title, if any, prepended in bold) to the channel."""
        if draft.reply_to:
            raise NotSupported(
                "reply",
                NAME,
                alternatives=("post to the channel and quote the message you answer",),
            )
        if draft.notify:
            raise NotSupported(
                "notify",
                NAME,
                alternatives=("mention the user in the text as <@user id>",),
            )
        text = f"**{draft.title}**\n\n{draft.text}" if draft.title else draft.text
        if len(text) > MAX_TEXT_CHARS:
            raise ChannelError(
                f"the text is {len(text)} characters; Discord accepts {MAX_TEXT_CHARS}",
                kind="validation",
            )
        plan = {
            "action": "post a message",
            "channel_id": ref.id,
            "text": text,
            "title_prepended": bool(draft.title),
        }
        if dry_run:
            return SendResult(
                ok=True, channel=NAME, conversation=ref.encoded, dry_run=True, plan=plan
            )
        posted = self._call(
            f"post to channel {ref.id}",
            lambda: self.backend.send_message(ref.id, text),
        )
        return SendResult(
            ok=True,
            channel=NAME,
            conversation=ref.encoded,
            message_id=str((posted or {}).get("id") or "") or None,
            url=(posted or {}).get("jump_url"),
            plan=plan,
        )
