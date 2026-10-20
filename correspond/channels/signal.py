"""Signal through ``signal-cli-rest-api`` (JSON-RPC mode): listen, read what was logged, send.

References: ``signal:`` (the linked account's receive stream, for ``listen``),
``signal:+<number>`` (a person, by international phone number or Signal uuid) and
``signal:group.<id>`` (a group, by the REST API's group id).

Signal has no bot API. ``signal-cli`` runs as a *linked device* (or a registered number) of
an account you control, and the REST wrapper exposes it: ``POST /v2/send`` to send,
``GET /v1/receive/<number>`` (a WebSocket in JSON-RPC mode) to receive. The WebSocket client
is the optional ``websocket-client`` (extra ``signal``); tests and other transports pass
``ws=``.

Three facts shape the adapter:

- There is no history before linking: ``read`` returns only what ``listen`` and ``send``
  logged on this machine (``history_depth = since_link``).
- ``signal-cli`` releases older than about three months stop working against Signal's
  servers, so a silent channel may just be an old release (``requirements signal`` says so).
- Whether to trust a changed safety number is an authenticity policy of the server
  (``--trust-new-identities``). ``listen`` compares the account's known identities on every
  poll and reports a changed safety number as an ``identity.changed`` event.

>>> Signal().parse_ref("group.ZXhhbXBsZQ==").kind
'group'
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable, Iterable, Mapping, MutableMapping
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from correspond.channels._http import classify, urllib_http
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
from correspond.ops import window, with_final_cursor
from correspond.registry import require, value
from correspond.settings import mask

__all__ = ["Signal", "requirements_problems", "websocket_frames"]

NAME = "signal"
DEFAULT_URL = "http://127.0.0.1:8080"
DEFAULT_LISTEN_WAIT_S = 3.0
PERSON_RE = re.compile(
    r"^(\+\d{7,15}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$"
)
GROUP_RE = re.compile(r"^group\.[A-Za-z0-9+/=_-]{4,}$")
_PLATFORM = Authenticity(grade=Grade.PLATFORM, evidence={"method": "signal-protocol"})


def websocket_frames(url: str, *, timeout: float) -> Iterable[str]:
    """Text frames of the WebSocket at ``url`` until it has been quiet for ``timeout`` seconds (the default ``ws=``)."""
    try:
        import websocket
    except ImportError:
        raise MissingRequirement(
            NAME,
            "listening needs the websocket-client package",
            fix="pip install 'correspond[signal]'",
            kind="unavailable",
        ) from None
    try:
        connection = websocket.create_connection(url, timeout=timeout)
    except (OSError, websocket.WebSocketException) as error:
        raise ChannelError(
            f"cannot reach the signal-cli-rest-api WebSocket at {urlsplit(url).netloc}: {type(error).__name__}",
            kind="network",
            retryable=True,
        ) from None
    frames = []
    try:
        while True:
            try:
                frame = connection.recv()
            except websocket.WebSocketTimeoutException:
                break
            except websocket.WebSocketConnectionClosedException:
                break
            if frame:
                frames.append(frame if isinstance(frame, str) else frame.decode())
    finally:
        connection.close()
    return frames


def requirements_problems() -> list[str]:
    """What stops Signal working from this configuration; the signal-cli age caveat is a note, not a problem."""
    if value(NAME, "number"):
        return []
    return ["the linked account's number is not set (CORRESPOND_SIGNAL_NUMBER)"]


def _ws_url(base: str, number: str) -> str:
    parts = urlsplit(base)
    scheme = "wss" if parts.scheme == "https" else "ws"
    return urlunsplit(
        (
            scheme,
            parts.netloc,
            f"{parts.path.rstrip('/')}/v1/receive/{quote(number, safe='')}",
            "",
            "",
        )
    )


def _group_ref(internal_id: str) -> str:
    """The REST API's group id: ``group.`` and the base64 of the id signal-cli reports."""
    return "group." + base64.b64encode(internal_id.encode()).decode()


def _at(millis: Any) -> datetime:
    return datetime.fromtimestamp(int(millis or 0) / 1000, tz=timezone.utc)


def _log_key(conversation: str, message_id: str) -> str:
    return f"{conversation}/{message_id}.json"


class Signal:
    """A Signal account linked to a ``signal-cli-rest-api`` server."""

    name = NAME

    def __init__(
        self,
        *,
        http: Callable[..., Any] = urllib_http,
        ws: Callable[..., Iterable[str]] = websocket_frames,
        log: MutableMapping[str, Any] | None = None,
    ):
        self.http = http
        self.ws = ws
        self._log = log

    @property
    def log(self) -> MutableMapping[str, Any]:
        """Every message received or sent, as JSON, keyed ``<conversation id>/<message id>.json``; identities seen sit under ``identities/``."""
        if self._log is None:
            from correspond.stores import json_store

            self._log = json_store(NAME)
        return self._log

    @property
    def capabilities(self) -> Capabilities:
        """A linked device's view of Signal."""
        return Capabilities(
            channel=NAME,
            read=Support.PARTIAL,
            listen=Support.FULL,
            send=Support.FULL,
            audience=Support.PARTIAL,
            initiate=Support.FULL,
            reply=Support.PARTIAL,
            history_depth=HistoryDepth.SINCE_LINK,
            grades=(Grade.PLATFORM,),
            native_fields=("group", "quote"),
            formats=("plain",),
            notes=(
                "read returns only what listen and send logged on this machine: Signal gives no history before the device was linked",
                "listen is account-wide (signal:); narrow to a conversation when routing; it connects to the WebSocket, collects what arrives within listen_wait_s and disconnects",
                "signal-cli needs a registered or linked number, and a release older than about three months stops working: update it first when listening goes quiet",
                "a changed safety number is an identity.changed event (found by comparing GET /v1/identities on each listen); whether such an identity is trusted is the server's trust-new-identities setting, an authenticity policy",
                "a reply needs the quoted message's author: it is found in the log, so only messages this machine saw can be answered",
                "audience: a group's members come from the REST API; a person's conversation is named",
                "group references use the REST API's group id (group.<base64>)",
            ),
        )

    def parse_ref(self, id: str) -> ConversationRef:
        """``""`` (the account's stream), a phone number or uuid, or ``group.<id>``."""
        if id == "":
            return ConversationRef(channel=NAME, id="", kind="account")
        if PERSON_RE.match(id):
            return ConversationRef(channel=NAME, id=id, kind="person")
        if GROUP_RE.match(id):
            return ConversationRef(channel=NAME, id=id, kind="group")
        raise InvalidRef(
            f"signal references are signal:, signal:+<number>, signal:<uuid> or signal:group.<id>, not signal:{id}"
        )

    # ------------------------------------------------------------------ plumbing

    def _base(self) -> str:
        return (value(NAME, "url") or DEFAULT_URL).rstrip("/")

    def _call(self, method: str, path: str, body: Mapping[str, Any] | None = None) -> Any:
        response = self.http(
            method,
            f"{self._base()}{path}",
            headers={"Content-Type": "application/json"} if body is not None else None,
            body=json.dumps(body).encode() if body is not None else None,
        )
        data = response.json()
        if response.status >= 300:
            detail = data.get("error") if isinstance(data, dict) else None
            raise classify(
                response.status,
                f"signal-cli-rest-api refused {method} {path.split('/')[1:3][-1]} ({response.status}{': ' + str(detail) if detail else ''})",
                response.headers,
            )
        return data

    def _remember(self, message: Message) -> None:
        self.log[_log_key(message.conversation.id, message.id)] = message.to_dict()

    # --------------------------------------------------------------- listening

    def _message(
        self,
        envelope: Mapping[str, Any],
        data: Mapping[str, Any],
        account: str,
        *,
        synced: bool = False,
    ) -> Message:
        """The :class:`Message` of one envelope.

        ``synced``: ``data`` is a ``syncMessage.sentMessage``, which the user sent from another
        device of the account. The author is then the account itself, and a one-to-one
        conversation is the recipient's, not the sender's.
        """
        source = str(
            envelope.get("sourceNumber")
            or envelope.get("source")
            or envelope.get("sourceUuid")
            or ""
        )
        group = (data.get("groupInfo") or {}).get("groupId")
        if synced:
            source = account or source
            peer = str(
                data.get("destinationNumber")
                or data.get("destination")
                or data.get("destinationUuid")
                or ""
            )
            if not group and not peer:
                raise InvalidRef("the synced message names no recipient")
        else:
            peer = source
        if not group and not peer:
            raise InvalidRef("the envelope names no sender")
        conversation = (
            self.parse_ref(_group_ref(group)) if group else self.parse_ref(peer)
        )
        quote_ = data.get("quote") or {}
        return Message(
            id=str(data.get("timestamp") or envelope.get("timestamp")),
            conversation=conversation,
            author=ChannelIdentity(
                channel=NAME,
                native_id=source,
                handle=source if source.startswith("+") else None,
                display_name=envelope.get("sourceName") or None,
                is_self=bool(account) and source == account,
            ),
            authenticity=_PLATFORM,
            sent_at=_at(data.get("timestamp") or envelope.get("timestamp")),
            text=data.get("message") or "",
            reply_to=str(quote_["id"]) if quote_.get("id") else None,
            native={
                "group": bool(group),
                "quote": quote_ or None,
                **({"synced": True} if synced else {}),
            },
            raw=dict(envelope),
        )

    def _events(
        self, frames: Iterable[str], account: str
    ) -> tuple[list[Event], str | None]:
        events, last = [], None
        for frame in frames:
            try:
                item = json.loads(frame)
            except json.JSONDecodeError:
                continue
            envelope = item.get("envelope") if isinstance(item, dict) else None
            if not isinstance(envelope, dict):
                continue
            stamp = envelope.get("timestamp")
            edit = envelope.get("editMessage")
            data = envelope.get("dataMessage")
            synced = False
            if not isinstance(data, dict):  # sent from the user's other device
                sent = (envelope.get("syncMessage") or {}).get("sentMessage")
                if isinstance(sent, dict):
                    data, synced = sent, True
            kind, payload = "message.created", {}
            if edit and edit.get("dataMessage"):
                data, kind = edit["dataMessage"], "message.updated"
                payload = {"target_sent_timestamp": edit.get("targetSentTimestamp")}
            if not isinstance(data, dict):
                continue
            last = str(stamp) if stamp else last
            if data.get("reaction"):
                reaction = data["reaction"]
                events.append(
                    Event(
                        kind="reaction.added",
                        channel=NAME,
                        delivery_id=f"signal:{stamp}",
                        cursor=str(stamp),
                        payload={
                            "emoji": reaction.get("emoji"),
                            "remove": bool(reaction.get("isRemove")),
                            "target_author": reaction.get("targetAuthor"),
                            "target_sent_timestamp": reaction.get("targetSentTimestamp"),
                            "from": envelope.get("sourceNumber")
                            or envelope.get("source"),
                        },
                    )
                )
                continue
            try:
                message = self._message(envelope, data, account, synced=synced)
            except (
                InvalidRef
            ):  # sealed sender or an odd source: nothing to attribute it to
                continue
            if kind == "message.updated":
                message = replace(
                    message,
                    id=str(payload["target_sent_timestamp"] or message.id),
                    edited_at=_at(stamp),
                )
            self._remember(message)
            events.append(
                Event(
                    kind=kind,
                    channel=NAME,
                    delivery_id=f"signal:{stamp}",
                    cursor=str(stamp),
                    message=message,
                    payload=payload,
                )
            )
        return events, last

    def _identity_changes(self, account: str) -> list[Event]:
        """``identity.changed`` for every contact whose safety number differs from the one last seen; the first sight is a baseline, not a change."""
        known = self._call("GET", f"/v1/identities/{quote(account, safe='')}")
        events = []
        for identity in known if isinstance(known, list) else []:
            who = str(identity.get("number") or identity.get("uuid") or "")
            safety = str(
                identity.get("safety_number") or identity.get("fingerprint") or ""
            )
            if not who or not safety:
                continue
            key = f"identities/{who}.json"
            before = self.log.get(key)
            self.log[key] = {
                "safety_number": safety,
                "trust_level": identity.get("trust_level"),
            }
            if before and before.get("safety_number") != safety:
                events.append(
                    Event(
                        kind="identity.changed",
                        channel=NAME,
                        delivery_id=f"signal:identity-{who}-{safety[:12]}",
                        payload={
                            "who": who,
                            "trust_level": identity.get("trust_level"),
                            "previous_trust_level": before.get("trust_level"),
                        },
                    )
                )
        return events

    def poll(
        self, ref: ConversationRef, *, cursor: str | None = None, limit: int | None = None
    ):
        """What the WebSocket delivered within ``listen_wait_s``, then any safety-number changes.

        Signal hands each message to the receiver once, so the cursor (the last envelope
        timestamp) only marks progress; ``delivery_id`` is what dedupes.
        """
        if ref.id:
            raise NotSupported(
                "listen to one conversation",
                NAME,
                alternatives=("listen to signal: and route by conversation",),
            )
        account = require(NAME, "number")
        wait = float(value(NAME, "listen_wait_s") or DEFAULT_LISTEN_WAIT_S)
        frames = self.ws(_ws_url(self._base(), account), timeout=wait)
        events, last = self._events(frames, account)
        # ``limit`` is not applied: Signal delivers each message once, so cutting the
        # batch here would lose the rest for good.
        try:
            events += self._identity_changes(account)
        except (
            ChannelError
        ):  # a failed identity lookup must not lose the messages in hand
            pass
        return with_final_cursor(events, last or cursor)

    # --------------------------------------------------------------- operations

    def read(self, ref: ConversationRef, *, since=None, limit=None) -> list[Message]:
        """What the log holds for a conversation, oldest first."""
        if not ref.id:
            raise ChannelError(
                "read one conversation: signal:+<number> or signal:group.<id>",
                kind="validation",
            )
        messages = [
            Message.from_dict(self.log[k])
            for k in list(self.log)
            if not k.startswith("identities/") and k.rpartition("/")[0] == ref.id
        ]
        messages.sort(key=lambda m: (m.sent_at, m.id))
        return window(messages, since=since, limit=limit)

    def audience(self, ref: ConversationRef, *, draft: Draft | None = None) -> Audience:
        """Who reads a conversation: a person's is ``named``; a group's members are listed by the REST API."""
        if not ref.id:
            return Audience.unknown(
                ref.encoded,
                "signal: is the account's receive stream, not a conversation: name one",
            )
        common = dict(
            ref=ref.encoded,
            retractable=False,
            durability=("copies_pushed",),
            widening=("forwarding",),
        )
        if ref.kind == "person":
            return Audience(
                scope=Scope.NAMED,
                readers=(ChannelIdentity(channel=NAME, native_id=ref.id),),
                complete=True,
                external=None,
                evidence=(
                    "a one-to-one Signal conversation: the account and this contact",
                ),
                **common,
            )
        account = require(NAME, "number")
        group = self._call(
            "GET", f"/v1/groups/{quote(account, safe='')}/{quote(ref.id, safe='')}"
        )
        group = group if isinstance(group, dict) else {}
        members = [str(m) for m in group.get("members") or []]
        link = group.get("invite_link")
        evidence = [f"GET groups: {len(members)} members listed"]
        if link:
            evidence.append(
                "the group has an invite link: anyone holding it can join and read what follows"
            )
        return Audience(
            scope=Scope.PUBLIC if link else Scope.GROUP,
            readers=tuple(ChannelIdentity(channel=NAME, native_id=m) for m in members),
            complete=bool(members) and not link,
            classes=("anyone with the invite link",) if link else (),
            external=True if link else None,
            evidence=tuple(evidence),
            **common,
        )

    def _quote_author(self, ref: ConversationRef, reply_to: str) -> str:
        found = self.log.get(_log_key(ref.id, reply_to))
        if not found:
            raise ChannelError(
                f"message {reply_to} was not seen on this machine, so its author is unknown: Signal quotes need one",
                kind="validation",
            )
        return str(found["author"]["native_id"])

    def send(
        self, ref: ConversationRef, draft: Draft, *, dry_run: bool = False
    ) -> SendResult:
        """``POST /v2/send`` to a person or group, the title (if any) prepended; a reply quotes a logged message."""
        if not ref.id:
            raise ChannelError(
                "name a recipient: signal:+<number> or signal:group.<id>",
                kind="validation",
            )
        if draft.attachments:
            raise ChannelError(
                "signal sends text only here: attachments are not supported",
                kind="validation",
            )
        account = require(NAME, "number")
        text = f"{draft.title}\n\n{draft.text}" if draft.title else draft.text
        body: dict[str, Any] = {
            "message": text,
            "number": account,
            "recipients": [ref.id],
        }
        if draft.reply_to:
            if not str(draft.reply_to).isdigit():
                raise ChannelError(
                    f"Signal message ids are timestamps, not {draft.reply_to!r}",
                    kind="validation",
                )
            body["quote_timestamp"] = int(draft.reply_to)
            body["quote_author"] = self._quote_author(ref, draft.reply_to)
        plan = {
            "action": "send",
            "server": self._base(),
            "from": mask(account),
            "to": mask(ref.id) if ref.kind == "person" else ref.id,
            "reply_to": draft.reply_to,
            "title_prepended": bool(draft.title),
            "text": text,
        }
        if dry_run:
            return SendResult(
                ok=True, channel=NAME, conversation=ref.encoded, dry_run=True, plan=plan
            )
        result = self._call("POST", "/v2/send", body)
        stamp = str((result or {}).get("timestamp") or "")
        if stamp:
            self._remember(
                Message(
                    id=stamp,
                    conversation=ref,
                    author=ChannelIdentity(
                        channel=NAME, native_id=account, handle=account, is_self=True
                    ),
                    authenticity=_PLATFORM,
                    sent_at=_at(stamp),
                    text=text,
                    reply_to=draft.reply_to,
                    native={"group": ref.kind == "group", "quote": None},
                )
            )
        return SendResult(
            ok=True,
            channel=NAME,
            conversation=ref.encoded,
            message_id=stamp or None,
            account=Account(channel=NAME, id=mask(account), acts_as="user"),
            plan=plan,
        )
