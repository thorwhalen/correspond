"""Telegram: a bot over the Bot API, with the standard library.

References: ``telegram:`` (the bot's whole update stream, for listening),
``telegram:<chat id>`` (a private chat, a group or a channel; ``telegram:@name`` for a
public channel), ``telegram:<chat id>/<topic id>`` (a forum topic).

Bots have no history API, and Telegram keeps undelivered updates for at most 24 hours, so
``poll`` writes every update it receives to a local log under the data root
(``telegram/``) and ``read`` reads that log (a ``telegram:@name`` reference is first resolved
with ``getChat``, over the network). ``getUpdates`` confirms updates for every chat
at once, which is why listening is account-wide: narrow to one chat when routing.

Senders are ``platform``: Telegram authenticated the account, and correspond pulled the
update from the Bot API itself. The bot token never appears in an error or a plan.

>>> Telegram().parse_ref("-4001/7").parent.encoded
'telegram:-4001'
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Mapping, MutableMapping
from datetime import datetime, timezone
from typing import Any

from correspond.channels._http import urllib_http
from correspond.errors import ChannelError, InvalidRef, NotSupported
from correspond.model import (
    Account,
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

__all__ = ["Telegram"]

NAME = "telegram"
CHAT_RE = re.compile(r"^(?:-?[1-9][0-9]{0,19}|@[A-Za-z][A-Za-z0-9_]{3,31})$")
MAX_TEXT_CHARS = 4096
MAX_UPDATES = 100
UPDATE_TYPES = ("message", "edited_message", "channel_post", "edited_channel_post")
_MEDIA = ("photo", "document", "voice", "video", "audio", "sticker", "animation")
_PLATFORM = Authenticity(
    grade=Grade.PLATFORM, evidence={"attested_by": "the Telegram Bot API (getUpdates)"}
)


def _log_key(message: Message) -> str:
    chat = (
        message.conversation.parent.id
        if message.conversation.parent
        else message.conversation.id
    )
    return f"{chat}/{message.id}.json"


def _error(code: int, method: str, description: str, retry_after: Any) -> ChannelError:
    message = f"Telegram {method}: {description}"
    lowered = description.lower()
    if code == 429:
        return ChannelError(
            message, kind="rate_limited", retryable=True, retry_after=retry_after
        )
    # 401 is a refused token; the Bot API answers a malformed token's URL with a bare 404 "Not Found".
    if code == 401 or (code == 404 and lowered.startswith("not found")):
        return ChannelError(f"{message} (the bot token was not accepted)", kind="auth")
    if code == 403:
        return ChannelError(message, kind="permission")
    if "not found" in lowered:
        return ChannelError(message, kind="not_found")
    if code == 409:
        return ChannelError(
            f"{message} (another getUpdates consumer or a webhook is active for this bot)",
            kind="unavailable",
        )
    if code >= 500:
        return ChannelError(
            message, kind="unavailable", retryable=True, retry_after=retry_after
        )
    return ChannelError(message, kind="validation")


class Telegram:
    """A Telegram bot: listen, read the local log, send, edit, react."""

    name = NAME

    def __init__(
        self,
        *,
        http: Callable[..., Any] = urllib_http,
        log: MutableMapping[str, Any] | None = None,
        run: Callable[..., Any] = subprocess.run,
    ):
        self.http = http
        self.run = run
        self._log = log

    @property
    def log(self) -> MutableMapping[str, Any]:
        """Every message this bot has received or sent, as JSON, keyed ``<chat id>/<message id>.json``."""
        if self._log is None:
            from correspond.stores import json_store

            self._log = json_store(NAME)
        return self._log

    @property
    def capabilities(self) -> Capabilities:
        """A bot's view of Telegram."""
        return Capabilities(
            channel=NAME,
            read=Support.PARTIAL,
            listen=Support.FULL,
            send=Support.FULL,
            edit=Support.FULL,
            react=Support.FULL,
            initiate=Support.NONE,
            reply=Support.FULL,
            history_depth=HistoryDepth.BUFFER_24H,
            listen_modes=("poll",),
            native_fields=("chat_type", "chat_title", "message_thread_id", "media"),
            grades=(Grade.PLATFORM,),
            max_text_length=MAX_TEXT_CHARS,
            reactions_per_message=1,
            formats=("plain",),
            rate_limits=(
                "about one message per second per chat, 20 per minute per group",
            ),
            notes=(
                "read returns what listen has logged on this machine; updates never polled within 24 hours are gone",
                "listen is account-wide (telegram:); narrow to a chat when routing",
                "reading telegram:@name first resolves the name with getChat, over the network",
                "a bot cannot start a conversation: someone must write to it first",
                "a title is prepended to the text: Telegram messages have none",
            ),
        )

    def parse_ref(self, id: str) -> ConversationRef:
        """``""`` (the bot), a chat id or ``@name``, or ``<chat id>/<topic id>``."""
        if id == "":
            return ConversationRef(channel=NAME, id="", kind="bot")
        chat, _, topic = id.partition("/")
        if not CHAT_RE.match(chat) or (topic and not topic.isdigit()) or id.endswith("/"):
            raise InvalidRef(
                f"telegram references are telegram:, telegram:<chat id>, telegram:@name or telegram:<chat id>/<topic id>, not telegram:{id}"
            )
        chat_ref = ConversationRef(channel=NAME, id=chat, kind="chat")
        if not topic:
            return chat_ref
        return ConversationRef(channel=NAME, id=id, kind="topic", parent=chat_ref)

    # ------------------------------------------------------------------ plumbing

    def _call(self, method: str, params: Mapping[str, Any]) -> Any:
        token = require(NAME, "token", run=self.run)
        base = (value(NAME, "api_url") or "https://api.telegram.org").rstrip("/")
        try:
            response = self.http(
                "POST",
                f"{base}/bot{token}/{method}",
                headers={"Content-Type": "application/json"},
                body=json.dumps(dict(params)).encode("utf-8"),
            )
        except ChannelError as error:
            raise ChannelError(
                str(error).replace(token, "<token>"),
                kind=error.kind,
                retryable=error.retryable,
                retry_after=error.retry_after,
            ) from None
        data = response.json()
        if isinstance(data, dict) and data.get("ok"):
            return data.get("result")
        data = data if isinstance(data, dict) else {}
        description = str(data.get("description") or f"HTTP {response.status}").replace(
            token, "<token>"
        )
        code = int(data.get("error_code") or response.status)
        retry_after = (data.get("parameters") or {}).get("retry_after")
        raise _error(code, method, description, retry_after)

    def _message(self, data: Mapping[str, Any], *, is_self: bool = False) -> Message:
        chat = data.get("chat") or {}
        chat_id = str(chat.get("id", ""))
        thread = data.get("message_thread_id") if data.get("is_topic_message") else None
        conversation = self.parse_ref(f"{chat_id}/{thread}" if thread else chat_id)
        sender = data.get("from") or data.get("sender_chat") or {}
        name = " ".join(
            p for p in (sender.get("first_name"), sender.get("last_name")) if p
        )
        reply = data.get("reply_to_message") or {}
        replied = reply.get("message_id")
        return Message(
            id=str(data["message_id"]),
            conversation=conversation,
            author=ChannelIdentity(
                channel=NAME,
                native_id=str(sender.get("id", "")),
                handle=sender.get("username"),
                display_name=name or sender.get("title") or None,
                is_bot=bool(sender.get("is_bot")),
                is_self=is_self,
            ),
            authenticity=_PLATFORM,
            sent_at=datetime.fromtimestamp(int(data.get("date") or 0), tz=timezone.utc),
            edited_at=datetime.fromtimestamp(int(data["edit_date"]), tz=timezone.utc)
            if data.get("edit_date")
            else None,
            text=data.get("text") or data.get("caption") or "",
            # In a forum topic every message "replies" to the topic's first message; that is not a reply.
            reply_to=str(replied) if replied and replied != thread else None,
            native={
                "chat_type": chat.get("type"),
                "chat_title": chat.get("title"),
                "message_thread_id": thread,
                "media": [kind for kind in _MEDIA if kind in data],
            },
            raw=dict(data),
        )

    def _target(self, ref: ConversationRef) -> dict[str, Any]:
        if not ref.id:
            raise ChannelError("name a chat: telegram:<chat id>", kind="validation")
        chat, _, topic = ref.id.partition("/")
        params: dict[str, Any] = {
            "chat_id": int(chat) if chat.lstrip("-").isdigit() else chat
        }
        if topic:
            params["message_thread_id"] = int(topic)
        return params

    @staticmethod
    def _message_number(message_id: str) -> int:
        if not str(message_id).isdigit():
            raise ChannelError(
                f"Telegram message ids are numbers, not {message_id!r}", kind="validation"
            )
        return int(message_id)

    def _remember(self, data: Any) -> Message | None:
        if not isinstance(data, dict) or "message_id" not in data:
            return None
        message = self._message(data, is_self=True)
        self.log[_log_key(message)] = message.to_dict()
        return message

    @staticmethod
    def _account(data: Any) -> Account | None:
        bot = data.get("from") if isinstance(data, dict) else None
        if not bot:
            return None
        return Account(
            channel=NAME,
            id=f"@{bot['username']}" if bot.get("username") else str(bot.get("id")),
            acts_as="bot",
        )

    # --------------------------------------------------------------- operations

    def poll(
        self, ref: ConversationRef, *, cursor: str | None = None, limit: int | None = None
    ):
        """Updates since ``cursor`` (an update offset), every one logged before it is handed over."""
        if ref.id:
            raise NotSupported(
                "listen to one chat",
                NAME,
                alternatives=("listen to telegram: and route by conversation",),
            )
        params: dict[str, Any] = {"timeout": 0, "allowed_updates": list(UPDATE_TYPES)}
        if cursor:
            if not str(cursor).isdigit():
                raise ChannelError(
                    f"a Telegram cursor is an update offset, not {cursor!r}",
                    kind="validation",
                )
            params["offset"] = int(cursor)
        if limit:
            params["limit"] = max(1, min(MAX_UPDATES, int(limit)))
        updates = self._call("getUpdates", params) or []
        events = []
        for update in updates:
            update_id = int(update["update_id"])
            kind = next((k for k in UPDATE_TYPES if k in update), None)
            if kind is None:
                continue
            message = self._message(update[kind])
            self.log[_log_key(message)] = message.to_dict()
            events.append(
                Event(
                    kind="message.updated"
                    if kind.startswith("edited")
                    else "message.created",
                    channel=NAME,
                    delivery_id=f"telegram:update-{update_id}",
                    cursor=str(update_id + 1),
                    message=message,
                    payload={"update_id": update_id, "type": kind},
                )
            )
        final = str(int(updates[-1]["update_id"]) + 1) if updates else None
        return with_final_cursor(events, final)

    def read(self, ref: ConversationRef, *, since=None, limit=None) -> list[Message]:
        """What the log holds for a chat or topic, oldest first."""
        if not ref.id:
            raise ChannelError("read one chat: telegram:<chat id>", kind="validation")
        chat = ref.parent.id if ref.parent else ref.id
        topic = ref.id.partition("/")[2] or None
        if chat.startswith("@"):
            chat = str((self._call("getChat", {"chat_id": chat}) or {}).get("id", chat))
        prefix = f"{chat}/"
        messages = []
        for key in self.log:
            if not key.startswith(prefix):
                continue
            message = Message.from_dict(self.log[key])
            if (
                topic is None
                or str(message.native.get("message_thread_id") or "") == topic
            ):
                messages.append(message)
        messages.sort(key=lambda m: (m.sent_at, int(m.id) if m.id.isdigit() else 0))
        return window(messages, since=since, limit=limit)

    def send(
        self, ref: ConversationRef, draft: Draft, *, dry_run: bool = False
    ) -> SendResult:
        """``sendMessage``, the title (if any) prepended."""
        params = self._target(ref)
        text = f"{draft.title}\n\n{draft.text}" if draft.title else draft.text
        if len(text) > MAX_TEXT_CHARS:
            raise ChannelError(
                f"with the title the text is {len(text)} characters; Telegram accepts {MAX_TEXT_CHARS}",
                kind="validation",
            )
        params["text"] = text
        if draft.reply_to:
            params["reply_parameters"] = {
                "message_id": self._message_number(draft.reply_to)
            }
        plan = {"action": "sendMessage", **params, "title_prepended": bool(draft.title)}
        if dry_run:
            return SendResult(
                ok=True, channel=NAME, conversation=ref.encoded, dry_run=True, plan=plan
            )
        result = self._call("sendMessage", params)
        message = self._remember(result)
        return SendResult(
            ok=True,
            channel=NAME,
            conversation=ref.encoded,
            message_id=message.id if message else None,
            account=self._account(result),
            plan=plan,
        )

    def edit(
        self,
        ref: ConversationRef,
        message_id: str,
        draft: Draft,
        *,
        dry_run: bool = False,
    ) -> SendResult:
        """``editMessageText`` on one of the bot's messages."""
        params = self._target(ref)
        params.pop("message_thread_id", None)
        params.update(
            {"message_id": self._message_number(message_id), "text": draft.text}
        )
        plan = {"action": "editMessageText", **params}
        if dry_run:
            return SendResult(
                ok=True,
                channel=NAME,
                conversation=ref.encoded,
                operation="edit",
                dry_run=True,
                plan=plan,
            )
        result = self._call("editMessageText", params)
        self._remember(result)
        return SendResult(
            ok=True,
            channel=NAME,
            conversation=ref.encoded,
            operation="edit",
            message_id=str(message_id),
            account=self._account(result),
            plan=plan,
        )

    def react(
        self,
        ref: ConversationRef,
        message_id: str,
        reaction: str,
        *,
        dry_run: bool = False,
    ) -> SendResult:
        """``setMessageReaction`` with one emoji (a bot has one reaction per message)."""
        params = self._target(ref)
        params.pop("message_thread_id", None)
        params.update(
            {
                "message_id": self._message_number(message_id),
                "reaction": [{"type": "emoji", "emoji": reaction}],
            }
        )
        plan = {"action": "setMessageReaction", **params}
        if dry_run:
            return SendResult(
                ok=True,
                channel=NAME,
                conversation=ref.encoded,
                operation="react",
                dry_run=True,
                plan=plan,
            )
        self._call("setMessageReaction", params)
        return SendResult(
            ok=True,
            channel=NAME,
            conversation=ref.encoded,
            operation="react",
            message_id=str(message_id),
            plan=plan,
        )
