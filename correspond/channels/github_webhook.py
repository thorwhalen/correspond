"""GitHub webhook receiver: ``discussion_comment`` deliveries, so listen sees every discussion reply.

GitHub has no ``since`` filter for discussions, so a poll walks them by latest activity
and can miss a reply that moves neither the discussion's ``updatedAt`` nor its place in
that order (when the discussion is outside the ``recent_discussions`` window). A
``discussion_comment`` webhook names every such discussion as it happens.

:func:`mk_webhook_app` is the receiver, an ASGI app that checks ``X-Hub-Signature-256``
against the ``webhook_secret`` (the HMAC over the body: nothing unsigned is stored) and
keeps, under the data root (``github-webhook/``), only which discussion of which
repository had a comment, and when it arrived. With ``delivery = "webhook"`` in the
``github`` config, ``listen`` still polls, and also reads whole every discussion the
receiver saw since the cursor, finding the reply by its own creation time: the webhook
is a hint, the poll stays the source of the messages and of the cursor.

:func:`serve_webhook` runs the receiver (with uvicorn, the ``server`` extra) on
127.0.0.1:8082 unless ``webhook_allow_remote`` is set; put your HTTPS reverse proxy in
front, and point a repository or organization webhook (content type
``application/json``, the "Discussion comments" event) at it.
"""

from __future__ import annotations

import hmac
import json
import re
import time
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from correspond.channels._asgi import TOO_LARGE, lifespan, read_body, respond
from correspond.channels._webhook import (
    WEBHOOK_HOST,
    configured_store,
    serve_main as _serve_main,
    serve_receiver,
)
from correspond.channels.github import NAME, WEBHOOK_RETENTION, signature
from correspond.registry import require

__all__ = [
    "EVENTS",
    "WEBHOOK_KIND",
    "WEBHOOK_PORT",
    "mk_webhook_app",
    "serve_main",
    "serve_webhook",
    "webhook_app_from_env",
    "webhook_store",
]

#: The events the receiver keeps; any other signed event is acknowledged and dropped.
EVENTS = ("discussion_comment",)
#: The data-root folder the receiver stores deliveries in.
WEBHOOK_KIND = "github-webhook"
WEBHOOK_PORT = 8082
MAX_WEBHOOK_BODY_BYTES = 1_000_000
SIGNATURE_HEADER = "x-hub-signature-256"
EVENT_HEADER = "x-github-event"
DELIVERY_HEADER = "x-github-delivery"
_DELIVERY_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
#: A stored delivery's key: ``<delivery id>.json`` (a replayed delivery overwrites itself).
RECEIVED_KEY_RE = re.compile(r"^[A-Za-z0-9-]{1,64}\.json$")
PRUNE_EVERY_NS = 3600 * 10**9  # how often (at most) the receiver prunes on a write


def prune_deliveries(
    deliveries: MutableMapping[str, Any],
    *,
    now_ns: int,
    retention_ns: int = int(WEBHOOK_RETENTION.total_seconds() * 10**9),
) -> int:
    """Delete the deliveries received more than ``retention_ns`` before ``now_ns``; return how many.

    Only delivery keys with an integer ``received_ns`` are considered: anything else in
    the store is left alone.

    >>> store = {"old.json": {"received_ns": 1}, "new.json": {"received_ns": 9}, "x": 0}
    >>> prune_deliveries(store, now_ns=10, retention_ns=5)
    1
    >>> sorted(store)
    ['new.json', 'x']
    """
    expired = now_ns - retention_ns
    pruned = 0
    for key in list(deliveries):
        if not RECEIVED_KEY_RE.match(key):
            continue
        stamp = (deliveries.get(key) or {}).get("received_ns")
        if isinstance(stamp, int) and not isinstance(stamp, bool) and stamp < expired:
            deliveries.pop(key, None)
            pruned += 1
    return pruned


def _signed(received: str, secret: str, body: bytes) -> bool:
    return (
        bool(received)
        and received.isascii()
        and hmac.compare_digest(received, signature(secret, body))
    )


def _record(event: str, payload: Any) -> dict | None:
    """What is kept of a delivery: the repository, the discussion, the comment. ``None`` if it has none."""
    if not isinstance(payload, dict):
        return None
    repository = (payload.get("repository") or {}).get("full_name")
    discussion = (payload.get("discussion") or {}).get("number")
    comment = (payload.get("comment") or {}).get("id")
    if (
        not isinstance(repository, str)
        or not _REPOSITORY_RE.match(repository)
        or isinstance(discussion, bool)
        or not isinstance(discussion, int)
        or discussion < 1
    ):
        return None
    return {
        "event": event,
        "action": str(payload.get("action") or ""),
        "repository": repository,
        "discussion": discussion,
        "comment": comment if isinstance(comment, int) else None,
    }


def mk_webhook_app(
    *,
    secret: str,
    deliveries: MutableMapping[str, Any] | None = None,
    events: tuple[str, ...] = EVENTS,
    max_body_bytes: int = MAX_WEBHOOK_BODY_BYTES,
    clock: Callable[[], int] = time.time_ns,
    prune_every_ns: int = PRUNE_EVERY_NS,
) -> Callable[..., Awaitable[None]]:
    """The receiver as an ASGI app: GitHub POSTs signed deliveries, the app stores the ``events`` ones in ``deliveries``.

    A request whose ``X-Hub-Signature-256`` is not the HMAC of its body under ``secret`` is
    refused (401) and nothing is read from it: the signature is the only gate, since behind
    a reverse proxy every connection comes from this machine. A signed ``ping`` (sent when
    the webhook is created) and any event not in ``events`` get 202 and are not stored. A
    kept delivery is stored under ``<delivery id>.json`` as ``{event, action,
    repository, discussion, comment, received_ns}``: no comment text. Keyed by
    GitHub's delivery id, a replayed request (GitHub signatures carry no timestamp)
    overwrites its first copy instead of growing the store. ``deliveries``
    defaults to files under the data root (``github-webhook/``).

    The receiver also deletes deliveries older than ``WEBHOOK_RETENTION`` itself
    (:func:`prune_deliveries`), on a write, at most once per ``prune_every_ns``, so the
    store stays bounded in time even with no ``delivery = webhook`` listen to prune it.
    """
    if not isinstance(secret, str) or not secret:
        raise ValueError("the GitHub webhook secret is a non-empty string")
    if deliveries is None:
        deliveries = configured_store(NAME, WEBHOOK_KIND)
    last = [0]
    pruned_at = [None]

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
        if declared.isdigit() and int(declared) > max_body_bytes:
            return await respond(send, 413, {"error": "the delivery is too large"})
        body = await read_body(receive, max_body_bytes)
        if body is None:
            return None
        if body is TOO_LARGE:
            return await respond(send, 413, {"error": "the delivery is too large"})
        if not _signed(headers.get(SIGNATURE_HEADER, ""), secret, body):
            return await respond(send, 401, {"error": "wrong or missing signature"})
        event = headers.get(EVENT_HEADER, "")
        if event not in events:
            return await respond(send, 202, None)
        delivery = headers.get(DELIVERY_HEADER, "")
        if not _DELIVERY_RE.match(delivery):
            return await respond(send, 400, {"error": "a delivery carries its id"})
        try:
            record = _record(event, json.loads(body))
        except ValueError:  # not JSON, not UTF-8
            return await respond(send, 400, {"error": "the body is not JSON"})
        if record is None:
            return await respond(send, 400, {"error": "no repository or discussion"})
        last[0] = max(clock(), last[0] + 1)  # strictly increasing
        deliveries[f"{delivery}.json"] = {**record, "received_ns": last[0]}
        if pruned_at[0] is None or last[0] - pruned_at[0] >= prune_every_ns:
            pruned_at[0] = last[0]
            prune_deliveries(deliveries, now_ns=last[0])
        return await respond(send, 200, None)

    return app


def webhook_store() -> MutableMapping[str, Any]:
    """Where the receiver keeps deliveries: the ``github`` ``webhook_store`` setting's ``module:attr``, else files under the data root."""
    return configured_store(NAME, WEBHOOK_KIND)


def webhook_app_from_env() -> Callable[..., Awaitable[None]]:
    """:func:`mk_webhook_app` configured from the settings, for ``uvicorn --factory``."""
    return mk_webhook_app(
        secret=require(NAME, "webhook_secret"), deliveries=webhook_store()
    )


def serve_webhook(
    *,
    host: str = WEBHOOK_HOST,
    port: int = WEBHOOK_PORT,
    allow_remote: bool | None = None,
    app: Callable[..., Awaitable[None]] | None = None,
    run: Callable[..., Any] | None = None,
) -> None:
    """Run the receiver (with uvicorn) on ``host``, this machine unless ``allow_remote``.

    ``allow_remote`` defaults to the ``github`` ``webhook_allow_remote`` setting. Put your
    HTTPS reverse proxy in front.
    """
    serve_receiver(
        webhook_app_from_env,
        name=NAME,
        host=host,
        port=port,
        allow_remote=allow_remote,
        app=app,
        run=run,
    )


def serve_main(argv: list[str] | None = None) -> None:
    """``correspond-github-webhook``: run the webhook receiver (:func:`serve_webhook`)."""
    _serve_main(
        serve_webhook,
        prog="correspond-github-webhook",
        description="Run correspond's GitHub discussion_comment webhook receiver behind your HTTPS reverse proxy.",
        port=WEBHOOK_PORT,
        argv=argv,
    )
