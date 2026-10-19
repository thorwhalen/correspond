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

**Webhook delivery**, for a server: :meth:`Telegram.set_webhook` asks Telegram to POST
every update, one at a time, to an HTTPS address, carrying the configured
``webhook_secret`` in the ``X-Telegram-Bot-Api-Secret-Token`` header.
:func:`mk_webhook_app` is the receiver, an ASGI app that stores each update carrying that
secret under the data root (``telegram-webhook/``), in the order it arrives; the secret is
the gate, and nothing without it is stored. :func:`serve_webhook` runs it (with uvicorn,
the ``server`` extra) on ``127.0.0.1`` behind your own HTTPS reverse proxy, and refuses
any other address unless ``webhook_allow_remote`` is set. With ``delivery = "webhook"`` in
the config, ``listen`` reads what the receiver stored instead of calling ``getUpdates``:
Telegram serves one or the other, never both, so while a webhook is set ``getUpdates`` is
refused (409), and :meth:`Telegram.delete_webhook` returns the bot to polling.
:meth:`Telegram.verify` grades one delivery by the same header.

>>> Telegram().parse_ref("-4001/7").parent.encoded
'telegram:-4001'
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import re
import subprocess
import time
from collections.abc import Awaitable, Callable, Mapping, MutableMapping
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from correspond.channels._asgi import TOO_LARGE, lifespan, read_body, respond
from correspond.channels._http import (
    file_name,
    media_type as _media_type,
    multipart,
    urllib_http,
)
from correspond.errors import ChannelError, InvalidRef, MissingRequirement, NotSupported
from correspond.model import (
    Account,
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
)
from correspond.notify import handle, missing_mentions, with_mentions
from correspond.ops import window, with_final_cursor
from correspond.registry import require, value

__all__ = [
    "SECRET_HEADER",
    "Telegram",
    "mk_webhook_app",
    "serve_webhook",
    "webhook_app_from_env",
]

NAME = "telegram"
CHAT_RE = re.compile(r"^(?:-?[1-9][0-9]{0,19}|@[A-Za-z][A-Za-z0-9_]{3,31})$")
MAX_TEXT_CHARS = 4096
#: The Bot API's limits on a file a bot sends: any file, and a photo (shown inline).
MAX_DOCUMENT_BYTES = 50_000_000
MAX_PHOTO_BYTES = 10_000_000
#: The image types ``sendPhoto`` shows inline; anything else goes as a document.
PHOTO_TYPES = ("image/jpeg", "image/png", "image/webp")
#: A public Telegram username, the only handle a bot's mention reaches.
USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
MAX_UPDATES = 100
UPDATE_TYPES = ("message", "edited_message", "channel_post", "edited_channel_post")
_MEDIA = ("photo", "document", "voice", "video", "audio", "sticker", "animation")
_PLATFORM = Authenticity(
    grade=Grade.PLATFORM, evidence={"attested_by": "the Telegram Bot API (getUpdates)"}
)
_WEBHOOK = Authenticity(
    grade=Grade.PLATFORM,
    evidence={
        "attested_by": "the Telegram Bot API (a webhook delivery carrying the secret token)"
    },
)
#: The header Telegram puts the webhook's secret token in.
SECRET_HEADER = "x-telegram-bot-api-secret-token"
#: The alphabet and length Telegram accepts for a webhook secret token.
SECRET_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")
#: Where ``listen`` takes updates from: ``getUpdates``, or what the webhook receiver stored.
DELIVERIES = ("poll", "webhook")
#: The data-root folder the webhook receiver stores updates in.
WEBHOOK_KIND = "telegram-webhook"
MAX_WEBHOOK_BODY_BYTES = 1_000_000
#: Telegram's update ids are signed 64-bit integers.
MAX_UPDATE_ID = 2**63 - 1
#: How far back a first webhook listen looks, as getUpdates' buffer does.
WEBHOOK_LOOKBACK_NS = 24 * 3600 * 10**9
#: How long the receiver's updates are kept once a listen has passed them.
WEBHOOK_RETENTION_NS = 7 * 24 * 3600 * 10**9
WEBHOOK_HOST = "127.0.0.1"
WEBHOOK_PORT = 8081
#: The prefix of a webhook listen cursor (a getUpdates cursor is a bare offset).
_WEBHOOK_CURSOR = "w"
_RECEIVED_KEY_RE = re.compile(r"^(?P<stamp>[0-9]{20})-(?P<update>[0-9]{1,19})\.json$")
_TRUE = ("1", "true", "yes", "on")


def _secret_matches(received: str, secret: str) -> bool:
    return (
        bool(received)
        and received.isascii()
        and hmac.compare_digest(received.encode("ascii"), secret.encode("ascii"))
    )


def _check_secret(secret: Any) -> str:
    if not isinstance(secret, str) or not SECRET_TOKEN_RE.match(secret):
        raise ValueError(
            "the webhook secret is 1 to 256 characters of A-Z, a-z, 0-9, _ and - (Telegram's rule for secret_token)"
        )
    return secret


def mk_webhook_app(
    *,
    secret: str,
    updates: MutableMapping[str, Any] | None = None,
    max_body_bytes: int = MAX_WEBHOOK_BODY_BYTES,
    clock: Callable[[], int] = time.time_ns,
) -> Callable[..., Awaitable[None]]:
    """The webhook receiver as an ASGI app: Telegram POSTs updates, the app stores them in ``updates``.

    A request without ``secret`` in ``X-Telegram-Bot-Api-Secret-Token`` is refused (401)
    and nothing is stored: the secret is the only gate, since behind a reverse proxy every
    connection comes from this machine. Each update is stored under
    ``<arrival time in ns>-<update id>.json``, so ``listen`` takes them in the order they
    came, whatever their ids (Telegram restarts them at random after a week without
    updates). One receiver process per bot keeps that order: :meth:`Telegram.set_webhook`
    asks Telegram for one connection at a time. ``updates`` defaults to files under the
    data root (``telegram-webhook/``), which ``listen`` reads when ``delivery`` is
    ``webhook``.
    """
    _check_secret(secret)
    if updates is None:
        from correspond.stores import json_store

        updates = json_store(WEBHOOK_KIND)
    last = [0]

    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            return await lifespan(receive, send)
        if scope["type"] != "http":
            return None
        if scope.get("method", "GET") != "POST":
            return await respond(send, 405, {"error": "use POST"}, {"allow": "POST"})
        headers = {
            name.decode("latin-1").lower(): text.decode("latin-1")
            for name, text in scope.get("headers") or ()
        }
        if not _secret_matches(headers.get(SECRET_HEADER, ""), secret):
            return await respond(send, 401, {"error": "wrong or missing secret token"})
        declared = headers.get("content-length", "")
        if declared.isdigit() and int(declared) > max_body_bytes:
            return await respond(send, 413, {"error": "the update is too large"})
        body = await read_body(receive, max_body_bytes)
        if body is None:
            return None
        if body is TOO_LARGE:
            return await respond(send, 413, {"error": "the update is too large"})
        try:
            update = json.loads(body)
        except ValueError:  # not JSON, not UTF-8, or an integer too long to read
            return await respond(send, 400, {"error": "the body is not JSON"})
        update_id = update.get("update_id") if isinstance(update, dict) else None
        if (
            isinstance(update_id, bool)
            or not isinstance(update_id, int)
            or not 0 <= update_id <= MAX_UPDATE_ID
        ):
            return await respond(send, 400, {"error": "an update carries an update_id"})
        last[0] = max(
            clock(), last[0] + 1
        )  # strictly increasing, whatever the clock does
        updates[f"{last[0]:020d}-{update_id}.json"] = update
        return await respond(send, 200, None)

    return app


def webhook_app_from_env() -> Callable[..., Awaitable[None]]:
    """:func:`mk_webhook_app` configured from the settings, for ``uvicorn --factory``."""
    return mk_webhook_app(secret=require(NAME, "webhook_secret"))


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def serve_webhook(
    *,
    host: str = WEBHOOK_HOST,
    port: int = WEBHOOK_PORT,
    allow_remote: bool | None = None,
    app: Callable[..., Awaitable[None]] | None = None,
    run: Callable[..., Any] | None = None,
) -> None:
    """Run the receiver (with uvicorn) on ``host``, this machine unless ``allow_remote``.

    ``allow_remote`` defaults to the ``webhook_allow_remote`` setting. Put your HTTPS
    reverse proxy in front: Telegram reaches only public HTTPS addresses. Proxy headers are
    ignored, since the receiver needs no client address, and one worker keeps the order.
    """
    if allow_remote is None:
        allow_remote = (
            str(value(NAME, "webhook_allow_remote") or "").strip().lower() in _TRUE
        )
    if not allow_remote and not _is_loopback(host):
        raise ChannelError(
            f"the webhook receiver binds to this machine only ({WEBHOOK_HOST}), not {host!r}, "
            "unless webhook_allow_remote is set",
            kind="validation",
        )
    if run is None:
        try:
            import uvicorn
        except ImportError:
            raise MissingRequirement(
                NAME,
                "uvicorn, to serve the webhook receiver",
                fix="pip install 'correspond[server]'",
            ) from None
        run = uvicorn.run
    run(
        app or webhook_app_from_env(),
        host=host,
        port=port,
        workers=1,
        proxy_headers=False,
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
            f"{message} (another getUpdates consumer or a webhook is active for this bot: "
            "delete_webhook returns it to polling, or set delivery = webhook to listen to what the receiver stores)",
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
        updates: MutableMapping[str, Any] | None = None,
        run: Callable[..., Any] = subprocess.run,
        clock: Callable[[], int] = time.time_ns,
    ):
        self.http = http
        self.run = run
        self.clock = clock
        self._log = log
        self._updates = updates

    @property
    def log(self) -> MutableMapping[str, Any]:
        """Every message this bot has received or sent, as JSON, keyed ``<chat id>/<message id>.json``."""
        if self._log is None:
            from correspond.stores import json_store

            self._log = json_store(NAME)
        return self._log

    @property
    def updates(self) -> MutableMapping[str, Any]:
        """What the webhook receiver stored, keyed by zero-padded update id (``listen`` reads it when ``delivery`` is ``webhook``)."""
        if self._updates is None:
            from correspond.stores import json_store

            self._updates = json_store(WEBHOOK_KIND)
        return self._updates

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
            upload=Support.FULL,
            audience=Support.PARTIAL,
            verify=Support.FULL,
            initiate=Support.NONE,
            reply=Support.FULL,
            notify=Support.PARTIAL,
            history_depth=HistoryDepth.BUFFER_24H,
            listen_modes=DELIVERIES,
            native_fields=("chat_type", "chat_title", "message_thread_id", "media"),
            grades=(Grade.PLATFORM, Grade.FORGED),
            max_text_length=MAX_TEXT_CHARS,
            max_upload_bytes=MAX_DOCUMENT_BYTES,
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
                "notify mentions @username in the text: it reaches a member of the chat with a public username, not anyone outside it",
                "a title is prepended to the text: Telegram messages have none",
                "upload sends a JPEG, PNG or WebP image up to 10 MB as a photo (sendPhoto, shown inline) and anything else up to 50 MB as a document (sendDocument)",
                "audience comes from getChat: a public username, the chat type, history visibility; members are never listed",
                "delivery = webhook makes listen read what the webhook receiver (mk_webhook_app) stored instead of calling getUpdates; Telegram serves one or the other, so set_webhook stops getUpdates (409) until delete_webhook",
                "the webhook receiver stores only requests carrying the secret token, in arrival order (set_webhook asks for one connection at a time); serve_webhook binds it to 127.0.0.1 unless webhook_allow_remote is set; verify grades one delivery by that header: platform when it matches (it proves Telegram sent it, not a signature over the body), forged otherwise",
                "listen in webhook mode looks back a day on its first poll, deletes what it passed once a week old, and, when nothing is new, asks getWebhookInfo whether a webhook is set at all; a delivery Telegram repeats arrives twice, with one delivery_id",
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

    def _call(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        files: Mapping[str, tuple[str, bytes, str]] | None = None,
    ) -> Any:
        """One Bot API call: JSON, or ``multipart/form-data`` when it carries ``files``."""
        token = require(NAME, "token", run=self.run)
        base = (value(NAME, "api_url") or "https://api.telegram.org").rstrip("/")
        if files:
            fields = {
                k: v if isinstance(v, str) else json.dumps(v) for k, v in params.items()
            }
            content_type, body = multipart(fields, files)
        else:
            content_type, body = "application/json", json.dumps(dict(params)).encode()
        try:
            response = self.http(
                "POST",
                f"{base}/bot{token}/{method}",
                headers={"Content-Type": content_type},
                body=body,
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

    def _message(
        self,
        data: Mapping[str, Any],
        *,
        is_self: bool = False,
        authenticity: Authenticity = _PLATFORM,
    ) -> Message:
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
            authenticity=authenticity,
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
        """Updates since ``cursor``, every one logged before it is handed over.

        They come from ``getUpdates`` (the cursor is an update offset), or, with ``delivery
        = "webhook"`` in the config, from what the webhook receiver stored
        (:meth:`_poll_received`).
        """
        if ref.id:
            raise NotSupported(
                "listen to one chat",
                NAME,
                alternatives=("listen to telegram: and route by conversation",),
            )
        count = max(1, min(MAX_UPDATES, int(limit))) if limit else None
        if self._delivery() == "webhook":
            return self._poll_received(cursor, count)
        if cursor and not str(cursor).isdigit():
            raise ChannelError(
                f"a Telegram cursor is an update offset, not {cursor!r}",
                kind="validation",
            )
        params: dict[str, Any] = {"timeout": 0, "allowed_updates": list(UPDATE_TYPES)}
        if cursor:
            params["offset"] = int(cursor)
        if count:
            params["limit"] = count
        updates = (self._call("getUpdates", params) or [])[: count or None]
        pairs = [(u, str(int(u["update_id"]) + 1)) for u in updates]
        return with_final_cursor(
            self._events(pairs, _PLATFORM), pairs[-1][1] if pairs else None
        )

    def _poll_received(self, cursor: str | None, count: int | None):
        """What the webhook receiver stored after ``cursor`` (``w<arrival ns>``), in arrival order.

        A first listen, or one whose cursor is a ``getUpdates`` offset from before the
        switch, looks back a day. Updates a listen has passed are deleted once they are a
        week old. When nothing is new, ``getWebhookInfo`` is asked whether a webhook is set
        at all, so a bot left polling is not mistaken for a quiet one.
        """
        now = self.clock()
        text = str(cursor or "")
        if text.startswith(_WEBHOOK_CURSOR) and text[1:].isdigit():
            after = int(text[1:])
        elif not text or text.isdigit():
            after = now - WEBHOOK_LOOKBACK_NS
        else:
            raise ChannelError(
                f"a Telegram webhook cursor is {_WEBHOOK_CURSOR}<arrival ns>, not {cursor!r}",
                kind="validation",
            )
        received = sorted(
            (int(match["stamp"]), key)
            for key in list(self.updates)
            if (match := _RECEIVED_KEY_RE.match(key))
        )
        for stamp, key in received:
            if stamp <= after and stamp < now - WEBHOOK_RETENTION_NS:
                self.updates.pop(key, None)
        pending = [(stamp, key) for stamp, key in received if stamp > after]
        if count:
            pending = pending[:count]
        if not pending:
            self._require_webhook()
            return with_final_cursor([], None)
        pairs = [
            (self.updates[key], f"{_WEBHOOK_CURSOR}{stamp}") for stamp, key in pending
        ]
        return with_final_cursor(self._events(pairs, _WEBHOOK), pairs[-1][1])

    def _require_webhook(self) -> None:
        if not self._webhook_state().get("url"):
            raise ChannelError(
                "delivery is webhook, but no webhook is set for this bot: "
                "run Telegram().set_webhook('https://...'), or set delivery = poll",
                kind="validation",
            )

    def _events(
        self, pairs: list[tuple[Mapping[str, Any], str]], authenticity: Authenticity
    ) -> list[Event]:
        """One event per update of a type listen takes, each with the cursor that follows it; every message logged."""
        events = []
        for update, after in pairs:
            update_id = int(update["update_id"])
            kind = next((k for k in UPDATE_TYPES if k in update), None)
            if kind is None:
                continue
            message = self._message(update[kind], authenticity=authenticity)
            self.log[_log_key(message)] = message.to_dict()
            events.append(
                Event(
                    kind="message.updated"
                    if kind.startswith("edited")
                    else "message.created",
                    channel=NAME,
                    delivery_id=f"telegram:update-{update_id}",
                    cursor=after,
                    message=message,
                    payload={"update_id": update_id, "type": kind},
                )
            )
        return events

    def _delivery(self) -> str:
        delivery = str(value(NAME, "delivery") or "poll").strip().lower()
        if delivery not in DELIVERIES:
            raise ChannelError(
                f"telegram delivery is one of {DELIVERIES}, not {delivery!r}",
                kind="validation",
            )
        return delivery

    # ------------------------------------------------------------------ webhooks

    def verify(self, headers: Mapping[str, str], body: bytes) -> Authenticity:
        """``platform`` when the delivery carries the configured ``webhook_secret``, ``forged`` otherwise.

        The secret token proves Telegram sent the request (only Telegram and this machine
        know it); it signs nothing, so the grade is Telegram's own, not ``crypto``.
        """
        secret = require(NAME, "webhook_secret", run=self.run)
        lowered = {str(k).lower(): str(v) for k, v in headers.items()}
        received = lowered.get(SECRET_HEADER, "")
        if _secret_matches(received, secret):
            return Authenticity(
                grade=Grade.PLATFORM,
                evidence={**_WEBHOOK.evidence, "secret_token": "valid"},
            )
        return Authenticity(
            grade=Grade.FORGED,
            evidence={"secret_token": "mismatch" if received else "missing"},
        )

    def _bot_change(self, operation: str, plan: dict, *, dry_run: bool) -> SendResult:
        return SendResult(
            ok=True,
            channel=NAME,
            conversation=f"{NAME}:",
            operation=operation,
            dry_run=dry_run,
            plan=plan,
        )

    def set_webhook(self, url: str, *, dry_run: bool = False) -> SendResult:
        """Have Telegram POST this bot's updates to ``url`` (HTTPS), with the configured ``webhook_secret``.

        ``getUpdates`` stops working until :meth:`delete_webhook`; set ``delivery =
        "webhook"`` so ``listen`` reads what the receiver stores. The plan reads the
        webhook back with ``getWebhookInfo``. A dry run calls nothing and reads no secret.
        """
        if not str(url).lower().startswith("https://"):
            raise ChannelError(
                f"Telegram delivers webhooks over HTTPS only, not to {url!r}",
                kind="validation",
            )
        plan: dict[str, Any] = {
            "action": "deliver this bot's updates to a webhook",
            "request": "setWebhook",
            "url": url,
            "allowed_updates": list(UPDATE_TYPES),
            "max_connections": 1,
            "secret_token": "the configured webhook_secret",
            "consequence": "getUpdates is refused until delete_webhook; listen needs delivery = webhook",
        }
        if dry_run:
            return self._bot_change("set_webhook", plan, dry_run=True)
        secret = require(NAME, "webhook_secret", run=self.run)
        if not SECRET_TOKEN_RE.match(secret):
            raise ChannelError(
                "webhook_secret must be 1 to 256 characters of A-Z, a-z, 0-9, _ and -",
                kind="validation",
            )
        self._call(
            "setWebhook",
            {
                "url": url,
                "secret_token": secret,
                "allowed_updates": list(UPDATE_TYPES),
                "max_connections": 1,
            },
        )
        plan["now"] = self._webhook_state()
        return self._bot_change("set_webhook", plan, dry_run=False)

    def delete_webhook(
        self, *, drop_pending_updates: bool = False, dry_run: bool = False
    ) -> SendResult:
        """Stop webhook delivery, so ``getUpdates`` works again; pending updates are kept unless ``drop_pending_updates``."""
        plan: dict[str, Any] = {
            "action": "stop delivering this bot's updates to a webhook",
            "request": "deleteWebhook",
            "drop_pending_updates": bool(drop_pending_updates),
            "consequence": "listen needs delivery = poll again",
        }
        if dry_run:
            return self._bot_change("delete_webhook", plan, dry_run=True)
        self._call("deleteWebhook", {"drop_pending_updates": bool(drop_pending_updates)})
        plan["now"] = self._webhook_state()
        return self._bot_change("delete_webhook", plan, dry_run=False)

    def webhook_info(self) -> dict[str, Any]:
        """Where Telegram delivers this bot's updates now (``getWebhookInfo``): an empty ``url`` means polling."""
        return self._webhook_state()

    def _webhook_state(self) -> dict[str, Any]:
        info = self._call("getWebhookInfo", {}) or {}
        keep = ("url", "pending_update_count", "last_error_date", "last_error_message")
        return {key: info[key] for key in keep if key in info}

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

    def audience(self, ref: ConversationRef, *, draft: Draft | None = None) -> Audience:
        """Who reads a chat, from ``getChat``; a topic has its chat's readers.

        - A private chat: ``named``, the other account (by its id only, so a rename keeps
          the hash) as its reader.
        - A chat with a public username: ``public``.
        - A group or supergroup without one: ``group``; a channel without one: ``group``
          of its subscribers. Members are never listed.
        - A linked chat (a channel's discussion group, or the channel a group discusses)
          is read too: posts are copied into it. A public linked chat makes the audience
          public; one that cannot be read makes it unknown.

        Joiners read the history unless ``getChat`` says the history is hidden from them.
        Forwarding widens every chat unless its content is protected. A failed ``getChat``
        raises, so the audience resolves to public, defaulted.
        """
        if not ref.id:
            return Audience.unknown(
                ref.encoded, "telegram: is the bot's update stream, not a chat: name one"
            )
        chat_id = self._target(ref.parent or ref)["chat_id"]
        chat = self._call("getChat", {"chat_id": chat_id})
        chat = chat if isinstance(chat, dict) else {}
        kind, username = chat.get("type"), chat.get("username")
        evidence = [
            f"getChat {chat_id}: type {kind or 'not given'}, "
            + (f"public username @{username}" if username else "no public username")
        ]
        widening = ["forwarding"]
        if chat.get("has_protected_content"):
            widening = []
            evidence.append("content is protected: members cannot forward or save it")
        if kind != "private" and chat.get("has_visible_history") is not False:
            widening.append("joiners_read_history")
            if kind in ("group", "supergroup"):
                evidence.append(
                    "new members see the history"
                    if chat.get("has_visible_history")
                    else "history visibility not given: assumed visible to new members"
                )
        common = dict(
            ref=ref.encoded,
            retractable=False,
            durability=("copies_pushed",),
            widening=tuple(widening),
        )
        if kind == "private":
            name = " ".join(
                p for p in (chat.get("first_name"), chat.get("last_name")) if p
            )
            reader = ChannelIdentity(channel=NAME, native_id=str(chat.get("id", chat_id)))
            return Audience(
                scope=Scope.NAMED,
                readers=(reader,),
                complete=True,
                external=None,
                evidence=(
                    *evidence,
                    f"a private chat: the bot and one account ({name or 'no name'})",
                ),
                **common,
            )
        if kind not in ("group", "supergroup", "channel"):
            return Audience.unknown(
                ref.encoded, *evidence, f"chat type {kind!r} is not known"
            )
        whom = "subscriber of the channel" if kind == "channel" else "member of the group"
        classes = [f"every {whom}, and anyone who joins"]
        public = bool(username)
        linked_id = chat.get("linked_chat_id")
        if linked_id:
            try:
                linked = self._call("getChat", {"chat_id": linked_id})
            except ChannelError as error:
                return Audience.unknown(
                    ref.encoded,
                    *evidence,
                    f"linked chat {linked_id} could not be read ({error.kind}): {error}",
                )
            linked = linked if isinstance(linked, dict) else {}
            classes.append(
                f"every member of the linked chat {linked_id}, where posts are copied"
            )
            evidence.append(
                f"getChat {linked_id} (linked): type {linked.get('type') or 'not given'}, "
                + ("public username" if linked.get("username") else "no public username")
            )
            public = public or bool(linked.get("username"))
        if public:
            classes.insert(0, "anyone who finds the chat by its public username")
        return Audience(
            scope=Scope.PUBLIC if public else Scope.GROUP,
            classes=tuple(classes),
            external=True if public else None,
            evidence=(*evidence, "the Bot API does not list members"),
            **common,
        )

    def prepare(self, ref: ConversationRef, draft: Draft) -> Draft:
        """The draft as it will post: ``notify`` usernames checked, the missing mentions added. Pure."""
        if not draft.notify:
            return draft
        bad = [n for n in draft.notify if not USERNAME_RE.match(handle(n))]
        if bad:
            raise ChannelError(
                f"not a Telegram username: {', '.join(bad)}", kind="validation"
            )
        return replace(draft, text=with_mentions(draft.text, draft.notify))

    def send(
        self, ref: ConversationRef, draft: Draft, *, dry_run: bool = False
    ) -> SendResult:
        """``sendMessage``, the title (if any) prepended; ``draft.notify`` usernames the text does not @mention yet are mentioned on a closing ``cc`` line."""
        added = missing_mentions(draft.text, draft.notify) if draft.notify else ()
        draft = self.prepare(ref, draft)
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
        if draft.notify:
            plan.update(
                notify=[handle(n) for n in draft.notify], mentions_added=list(added)
            )
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

    def upload(
        self,
        ref: ConversationRef,
        name: str,
        data: bytes,
        *,
        media_type: str = "application/octet-stream",
        dry_run: bool = False,
    ) -> SendResult:
        """Send a file: ``sendPhoto`` for an inline image (:data:`PHOTO_TYPES`, 10 MB), else ``sendDocument`` (50 MB).

        A photo Telegram refuses (too large in pixels, too narrow, not really an image) is
        sent again as a document, which takes any file; the plan says so (``photo_refused``).
        """
        name = file_name(name)
        media_type = _media_type(media_type)
        params = self._target(ref)
        photo = media_type in PHOTO_TYPES and len(data) <= MAX_PHOTO_BYTES
        method, field = ("sendPhoto", "photo") if photo else ("sendDocument", "document")
        plan = {
            "action": method,
            **params,
            "name": name,
            "media_type": media_type,
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        if dry_run:
            return SendResult(
                ok=True,
                channel=NAME,
                conversation=ref.encoded,
                operation="upload",
                dry_run=True,
                plan=plan,
            )
        try:
            result = self._call(method, params, files={field: (name, data, media_type)})
        except ChannelError as error:
            if not photo or error.kind != "validation":
                raise
            plan.update(action="sendDocument", photo_refused=str(error))
            result = self._call(
                "sendDocument", params, files={"document": (name, data, media_type)}
            )
        message = self._remember(result)
        return SendResult(
            ok=True,
            channel=NAME,
            conversation=ref.encoded,
            operation="upload",
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
