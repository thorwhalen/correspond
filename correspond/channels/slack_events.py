"""Slack Events API receiver: Slack pushes a channel's messages, edits and deletions, so listen need not poll.

:func:`mk_events_app` is the receiver, an ASGI app that checks every request with
:meth:`~correspond.channels.slack.Slack.verify` (``X-Slack-Signature`` under the signing
secret, over a timestamp at most five minutes old: nothing unverified is read), answers
Slack's ``url_verification`` challenge, and keeps the ``message`` events under the data
root (``slack-events/``). With ``delivery = "events"`` in the ``slack`` config, ``listen``
reads what the receiver stored instead of polling ``conversations.history``, and reports
edits (``message.updated``) and deletions (``message.deleted``) too.

:func:`serve_events` runs the receiver (with uvicorn, the ``server`` extra) on
127.0.0.1:8083 unless ``webhook_allow_remote`` is set; put your HTTPS reverse proxy in
front, and give its URL as the app's Event Subscriptions Request URL, subscribed to the
bot events ``message.channels`` (and ``message.groups`` for private channels).
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable, Mapping, MutableMapping
from typing import Any

from correspond.channels._asgi import TOO_LARGE, lifespan, read_body, respond
from correspond.channels._webhook import (
    WEBHOOK_HOST,
    configured_store,
    serve_main as _serve_main,
    serve_receiver,
)
from correspond.channels.slack import (
    EVENTS_KIND,
    EVENTS_RETENTION_NS,
    NAME,
    RECEIVED_KEY_RE,
    Slack,
    configured_kept_channels,
    event_record,
    received_key,
)
from correspond.errors import ChannelError
from correspond.model import Authenticity, Grade
from correspond.registry import require

__all__ = [
    "EVENTS_PORT",
    "events_app_from_env",
    "events_store",
    "mk_events_app",
    "prune_events",
    "serve_events",
    "serve_main",
]

EVENTS_PORT = 8083
MAX_EVENT_BODY_BYTES = 1_000_000
PRUNE_EVERY_NS = 3600 * 10**9  # how often (at most) the receiver prunes on a write


def prune_events(
    events: MutableMapping[str, Any],
    *,
    now_ns: int,
    retention_ns: int = EVENTS_RETENTION_NS,
) -> int:
    """Delete the stored events that arrived more than ``retention_ns`` before ``now_ns``; return how many.

    Only keys the receiver writes are considered: anything else in the store is left alone.

    >>> store = {"00000000000000000001-Ev1.json": {}, "00000000000000000009-Ev2.json": {}, "x": 0}
    >>> prune_events(store, now_ns=10, retention_ns=5)
    1
    >>> sorted(store)
    ['00000000000000000009-Ev2.json', 'x']
    """
    pruned = 0
    for key in list(events):
        match = RECEIVED_KEY_RE.match(key)
        if match and int(match["stamp"]) < now_ns - retention_ns:
            events.pop(key, None)
            pruned += 1
    return pruned


_record = event_record  # kept for importers of the old private name


def mk_events_app(
    *,
    verify: Callable[[Mapping[str, str], bytes], Authenticity] | None = None,
    events: MutableMapping[str, Any] | None = None,
    max_body_bytes: int = MAX_EVENT_BODY_BYTES,
    clock: Callable[[], int] = time.time_ns,
    prune_every_ns: int = PRUNE_EVERY_NS,
    keep: Callable[[str], bool] | None = None,
    stamps: Any = None,
) -> Callable[..., Awaitable[None]]:
    """The receiver as an ASGI app: Slack POSTs signed requests, the app stores the ``message`` events in ``events``.

    ``verify`` grades a request from its headers and raw body; only ``crypto`` passes,
    anything else gets 401 and nothing is read from it. It defaults to
    :meth:`Slack.verify <correspond.channels.slack.Slack.verify>` (the signing secret,
    five-minute window). A verified ``url_verification`` gets its challenge back; any
    other verified request that is not a ``message`` event is acknowledged (200) and
    dropped. A kept event is stored under ``<arrival ns>-<event id>.json`` as
    ``{event_id, team_id, channel, event, received_ns}``; a retry Slack sends for an event
    already stored (a retry, ``X-Slack-Retry-Num``, or a replay within the signature's
    window) is acknowledged and not stored twice.
    ``events`` defaults to files under the data root (``slack-events/``).

    ``keep`` says which channel ids' events are stored (the others are acknowledged and
    dropped); it defaults to the ``keep_channels`` setting, and to every channel when that
    is unset. ``stamps`` (an :class:`~correspond.stores.ArrivalStamps`, by default the one
    Socket Mode listens share) stamps each event and holds its lock while the event is
    stored, so writers on one machine take turns and none stores an event below a cursor
    a listen already passed.

    The receiver deletes events older than the retention itself (:func:`prune_events`),
    on a write, at most once per ``prune_every_ns``.
    """
    if verify is None:
        verify = Slack().verify
    if events is None:
        events = configured_store(NAME, EVENTS_KIND)
    if keep is None:
        kept = configured_kept_channels()
        keep = (lambda channel: True) if kept is None else kept.__contains__
    if stamps is None:
        from correspond.stores import arrival_stamps

        stamps = arrival_stamps(EVENTS_KIND)
    last = [0]
    pruned_at = [None]
    seen: set[str] = set()
    scanned = [False]

    def stored(event_id: str) -> bool:
        """Whether this event was stored already: the store is scanned once, then remembered."""
        if not scanned[0]:
            seen.update(
                match["event"]
                for key in list(events)
                if (match := RECEIVED_KEY_RE.match(key))
            )
            scanned[0] = True
        return event_id in seen

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
        declared = headers.get("content-length", "")
        if declared.isascii() and declared.isdigit() and int(declared) > max_body_bytes:
            return await respond(send, 413, {"error": "the request is too large"})
        body = await read_body(receive, max_body_bytes)
        if body is None:
            return None
        if body is TOO_LARGE:
            return await respond(send, 413, {"error": "the request is too large"})
        try:
            verdict = verify(headers, body)
        except ChannelError:  # no signing secret: a configuration error, not a verdict
            return await respond(send, 503, {"error": "the receiver is not configured"})
        if verdict.grade is not Grade.CRYPTO:
            return await respond(
                send, 401, {"error": "wrong, stale or missing signature"}
            )
        try:
            payload = json.loads(body)
        except ValueError:  # not JSON, not UTF-8
            return await respond(send, 400, {"error": "the body is not JSON"})
        kind = payload.get("type") if isinstance(payload, dict) else None
        if kind == "url_verification":
            challenge = payload.get("challenge")
            if not isinstance(challenge, str) or len(challenge) > 200:
                return await respond(send, 400, {"error": "no challenge"})
            return await respond(send, 200, {"challenge": challenge})
        record = _record(payload) if kind == "event_callback" else None
        if record is None:
            return await respond(send, 200, None)
        if stored(record["event_id"]):  # Slack's retry, or a replay within the window
            return await respond(send, 200, None)
        if not keep(record["channel"]):  # a channel nobody listens to
            return await respond(send, 200, None)
        # strictly increasing, and stored before any other writer takes a stamp
        with stamps.issue(clock(), floor=last[0]) as stamp:
            events[received_key(stamp, record["event_id"])] = {
                **record,
                "received_ns": stamp,
            }
        last[0] = stamp
        seen.add(record["event_id"])
        await respond(send, 200, None)
        # after the response: the event is stored, a failing prune must not undo that
        if pruned_at[0] is None or last[0] - pruned_at[0] >= prune_every_ns:
            pruned_at[0] = last[0]
            try:
                prune_events(events, now_ns=last[0])
            except Exception:  # an unreadable entry; the next prune (or listen) retries
                pass
        return None

    return app


def events_store() -> MutableMapping[str, Any]:
    """Where the receiver keeps events: the ``slack`` ``webhook_store`` setting's ``module:attr``, else files under the data root."""
    return configured_store(NAME, EVENTS_KIND)


def events_app_from_env() -> Callable[..., Awaitable[None]]:
    """:func:`mk_events_app` configured from the settings, for ``uvicorn --factory``.

    The signing secret is required here, so a receiver without one refuses to start
    rather than refusing every request.
    """
    require(NAME, "signing_secret")
    return mk_events_app(events=events_store())


def serve_events(
    *,
    host: str = WEBHOOK_HOST,
    port: int = EVENTS_PORT,
    allow_remote: bool | None = None,
    app: Callable[..., Awaitable[None]] | None = None,
    run: Callable[..., Any] | None = None,
) -> None:
    """Run the receiver (with uvicorn) on ``host``, this machine unless ``allow_remote``.

    ``allow_remote`` defaults to the ``slack`` ``webhook_allow_remote`` setting. Put your
    HTTPS reverse proxy in front.
    """
    serve_receiver(
        events_app_from_env,
        name=NAME,
        host=host,
        port=port,
        allow_remote=allow_remote,
        app=app,
        run=run,
    )


def serve_main(argv: list[str] | None = None) -> None:
    """``correspond-slack-events``: run the Events API receiver (:func:`serve_events`)."""
    _serve_main(
        serve_events,
        prog="correspond-slack-events",
        description="Run correspond's Slack Events API receiver behind your HTTPS reverse proxy.",
        port=EVENTS_PORT,
        argv=argv,
    )
