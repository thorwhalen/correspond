"""The few ASGI pieces correspond's own receivers share (the web inbox collector, the Telegram webhook).

Plain ASGI with the standard library: no framework, and no server. A receiver binds
nothing itself; run it under any ASGI server, on a local address.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

#: What :func:`read_body` returns for a body over its limit.
TOO_LARGE = object()


async def respond(
    send, status: int, payload: Any, headers: Mapping[str, str] | None = None
) -> None:
    """A JSON response (no body when ``payload`` is ``None``)."""
    body = b"" if payload is None else json.dumps(payload).encode("utf-8")
    raw = [(b"content-type", b"application/json")] if payload is not None else []
    raw += [
        (k.encode("latin-1"), str(v).encode("latin-1"))
        for k, v in (headers or {}).items()
    ]
    raw.append((b"content-length", str(len(body)).encode("latin-1")))
    await send({"type": "http.response.start", "status": status, "headers": raw})
    await send({"type": "http.response.body", "body": body})


async def read_body(receive, limit: int) -> Any:
    """The request body, :data:`TOO_LARGE` past ``limit`` bytes, or ``None`` if the client left."""
    chunks, size = [], 0
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return None
        chunk = message.get("body", b"") or b""
        size += len(chunk)
        if size > limit:
            return TOO_LARGE
        chunks.append(chunk)
        if not message.get("more_body", False):
            return b"".join(chunks)


async def lifespan(receive, send) -> None:
    """Answer an ASGI server's startup and shutdown messages."""
    while True:
        message = await receive()
        if message["type"] == "lifespan.startup":
            await send({"type": "lifespan.startup.complete"})
        elif message["type"] == "lifespan.shutdown":
            await send({"type": "lifespan.shutdown.complete"})
            return
