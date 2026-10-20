"""Slack's Socket Mode, as a short listening window: push delivery without a public URL.

The default ``socket=`` of the Slack adapter, used when ``delivery = "socket"``. One call
asks Slack for a connection (``apps.connections.open`` with the app-level ``xapp-`` token,
which needs ``connections:write``), opens the websocket it names, acknowledges every
envelope Slack sends (an envelope left unacknowledged is sent again), and collects the
``events_api`` payloads until the connection has been quiet for ``wait`` seconds, or for
at most ``max_window_s`` in all. Socket Mode keeps nothing for a closed connection beyond
Slack's own retries, so what arrives while no window is open may be missed: the Events
API receiver (``delivery = "events"``) is the route that misses nothing.

The protocol is spoken over an injected ``connect(url, *, timeout)`` returning an object
with ``send(text)``, ``recv(timeout=None) -> text`` (raising ``TimeoutError`` when quiet)
and ``close()``; the default wraps websocket-client (the ``slack`` extra). The connection
request goes through an injected ``http`` (the stdlib transport by default).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from correspond.channels._http import urllib_http
from correspond.channels._websocket import websocket_connect as _websocket_connect
from correspond.errors import ChannelError

__all__ = [
    "CONNECTIONS_OPEN_URL",
    "MAX_WINDOW_S",
    "socket_window",
    "socket_url",
    "websocket_connect",
]

CONNECTIONS_OPEN_URL = "https://slack.com/api/apps.connections.open"
#: Hosts a Socket Mode URL may name.
SOCKET_HOST_SUFFIX = ".slack.com"
#: How long a window waits for Slack's ``hello``, even when ``wait`` is shorter.
HELLO_TIMEOUT_S = 15.0
#: The longest one window stays open, however busy the workspace.
MAX_WINDOW_S = 60.0

def websocket_connect(url: str, *, timeout: float) -> Any:
    """A websocket-client connection for Socket Mode (the default ``connect=``; the ``slack`` extra)."""
    return _websocket_connect(
        url, timeout=timeout, extra="slack", service="Slack's Socket Mode"
    )


def socket_url(app_token: str, *, http: Callable[..., Any] = urllib_http) -> str:
    """The ``wss://`` URL ``apps.connections.open`` hands out for one connection.

    The token goes only to ``slack.com``; the URL it returns must be ``wss://`` on a Slack
    host, or it is refused (a tampered answer must not point the listener elsewhere).
    """
    try:
        reply = http(
            "POST",
            CONNECTIONS_OPEN_URL,
            headers={
                "Authorization": f"Bearer {app_token}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            body=b"",
        )
    except ChannelError as error:
        raise ChannelError(
            f"Slack apps.connections.open: {error}".replace(app_token, "<token>"),
            kind=error.kind,
            retryable=error.retryable,
        ) from None
    if reply.status == 429:
        raise ChannelError(
            "Slack apps.connections.open: rate limited",
            kind="rate_limited",
            retryable=True,
        )
    data = reply.json() if reply.status == 200 else None
    if not isinstance(data, dict):
        raise ChannelError(
            f"Slack apps.connections.open: HTTP {reply.status}",
            kind="unavailable" if reply.status >= 500 else "validation",
            retryable=reply.status >= 500,
        )
    if not data.get("ok"):
        error = str(data.get("error") or "unknown_error")
        auth = error in ("invalid_auth", "not_authed", "token_revoked", "account_inactive")
        raise ChannelError(
            f"Slack apps.connections.open: {error}"
            + (
                " (the app_token is an app-level token, xapp-, with connections:write, and Socket Mode is enabled in the app's settings)"
                if auth or error in ("not_allowed_token_type", "missing_scope")
                else ""
            ),
            kind="auth" if auth else "permission" if error == "missing_scope" else "validation",
        )
    url = str(data.get("url") or "")
    if not _slack_socket_url(url):
        raise ChannelError(
            "Slack apps.connections.open answered with a URL that is not wss:// on a Slack host",
            kind="validation",
        )
    return url


def _slack_socket_url(url: str) -> bool:
    """Whether ``url`` is ``wss://`` on one of Slack's hosts.

    >>> _slack_socket_url("wss://wss-primary.slack.com/link/?ticket=t"), _slack_socket_url("wss://slack.com.example.org/")
    (True, False)
    >>> _slack_socket_url("ws://wss-primary.slack.com/link/")
    False
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return parts.scheme == "wss" and host.endswith(SOCKET_HOST_SUFFIX)


def socket_window(
    app_token: str,
    *,
    wait: float,
    connect: Callable[..., Any] = websocket_connect,
    http: Callable[..., Any] = urllib_http,
    clock: Callable[[], float] = time.monotonic,
    max_window_s: float = MAX_WINDOW_S,
) -> list[dict]:
    """One Socket Mode window: the ``events_api`` payloads Slack pushed, each acknowledged.

    Returns the payloads (``event_callback`` bodies, as the Events API would POST them) in
    arrival order. Every envelope with an ``envelope_id`` is acknowledged, whatever its
    type (slash commands and interactions are acknowledged and dropped). A ``disconnect``
    envelope, or the connection closing, ends the window with what it has.
    """
    url = socket_url(app_token, http=http)
    connection = connect(url, timeout=max(wait, HELLO_TIMEOUT_S))
    payloads: list[dict] = []
    started = clock()
    greeted = False
    try:
        while True:
            elapsed = clock() - started
            if elapsed >= max_window_s:
                break
            quiet = wait if greeted else max(wait, HELLO_TIMEOUT_S)
            try:
                text = connection.recv(timeout=min(quiet, max_window_s - elapsed))
            except TimeoutError:
                if not greeted:
                    raise ChannelError(
                        "Slack's Socket Mode never said hello", kind="network", retryable=True
                    ) from None
                break
            except ConnectionError:
                break
            try:
                envelope = json.loads(text)
            except ValueError:
                continue
            if not isinstance(envelope, dict):
                continue
            kind = envelope.get("type")
            if kind == "hello":
                greeted = True
                continue
            if kind == "disconnect":
                break
            envelope_id = envelope.get("envelope_id")
            if isinstance(envelope_id, str) and envelope_id:
                connection.send(json.dumps({"envelope_id": envelope_id}))
            payload = envelope.get("payload")
            if kind == "events_api" and isinstance(payload, dict):
                payloads.append(payload)
    finally:
        try:
            connection.close()
        except Exception:  # closing a dead socket: nothing left to lose
            pass
    return payloads
