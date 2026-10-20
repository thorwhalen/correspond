"""Slack's Socket Mode, as a short listening window: push delivery without a public URL.

The default ``socket=`` of the Slack adapter, used when ``delivery = "socket"``. One call
asks Slack for a connection (``apps.connections.open`` with the app-level ``xapp-`` token,
which needs ``connections:write``), opens the websocket it names, hands every
``events_api`` payload to a ``sink`` and only then acknowledges its envelope (Slack sends an
unacknowledged one again, so an event whose sink failed is not lost), until the connection
has been quiet for ``wait`` seconds, or for at most ``max_window_s`` in all. Socket Mode keeps nothing for a closed connection beyond
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
from correspond.channels._http import retry_after_seconds
from correspond.channels._websocket import require_websocket
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
        url,
        timeout=timeout,
        extra="slack",
        service="Slack's Socket Mode",
        control_frames=True,  # Slack pings: the window keeps its own deadline
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
            retry_after=retry_after_seconds(reply.headers),
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
        auth = error in (
            "invalid_auth",
            "not_authed",
            "token_revoked",
            "account_inactive",
        )
        raise ChannelError(
            f"Slack apps.connections.open: {error}"
            + (
                " (the app_token is an app-level token, xapp-, with connections:write, and Socket Mode is enabled in the app's settings)"
                if auth or error in ("not_allowed_token_type", "missing_scope")
                else ""
            ),
            kind="auth"
            if auth
            else "permission"
            if error == "missing_scope"
            else "validation",
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
    sink: Callable[[dict], None] | None = None,
    connect: Callable[..., Any] = websocket_connect,
    http: Callable[..., Any] = urllib_http,
    clock: Callable[[], float] = time.monotonic,
    max_window_s: float = MAX_WINDOW_S,
) -> list[dict]:
    """One Socket Mode window: the ``events_api`` payloads Slack pushed, in arrival order.

    Each payload (an ``event_callback`` body, as the Events API would POST it) goes to
    ``sink`` before its envelope is acknowledged; a sink that raises leaves the envelope
    unacknowledged, so Slack sends it again. Other envelopes (slash commands,
    interactions) are acknowledged and dropped. A ``disconnect`` envelope, or the
    connection closing or failing, ends the window with what it has; before Slack's
    ``hello`` either is an error (Socket Mode off for the app, a refused link).
    """
    if connect is websocket_connect:  # before the token is spent on a connection
        require_websocket(extra="slack", service="Slack's Socket Mode")
    url = socket_url(app_token, http=http)
    connection = connect(url, timeout=max(wait, HELLO_TIMEOUT_S))
    payloads: list[dict] = []
    started = clock()
    greeted = False

    def unwelcome(reason: str) -> ChannelError:
        if reason == "link_disabled":
            return ChannelError(
                "Slack's Socket Mode is off for this app: turn it on in the app's settings (Socket Mode)",
                kind="validation",
            )
        return ChannelError(
            f"Slack's Socket Mode closed before it said hello ({reason})",
            kind="network",
            retryable=True,
        )

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
                        "Slack's Socket Mode never said hello",
                        kind="network",
                        retryable=True,
                    ) from None
                break
            except ConnectionError:
                if not greeted:
                    raise unwelcome("the connection closed") from None
                break
            try:
                envelope = json.loads(text) if text else None
            except ValueError:
                continue
            if not isinstance(envelope, dict):
                continue  # a ping, or not JSON
            kind = envelope.get("type")
            if kind == "hello":
                greeted = True
                continue
            if kind == "disconnect":
                if not greeted:
                    raise unwelcome(str(envelope.get("reason") or "disconnect"))
                break
            payload = envelope.get("payload")
            if kind == "events_api" and isinstance(payload, dict):
                if sink is not None:
                    try:
                        sink(payload)
                    except (
                        Exception
                    ):  # not kept: leave it unacknowledged, Slack sends it again
                        continue
                payloads.append(payload)
            envelope_id = envelope.get("envelope_id")
            if isinstance(envelope_id, str) and envelope_id:
                try:
                    connection.send(json.dumps({"envelope_id": envelope_id}))
                except ConnectionError:
                    break  # unacknowledged: Slack sends it again; what was kept stays kept
    finally:
        try:
            connection.close()
        except Exception:  # closing a dead socket: nothing left to lose
            pass
    return payloads
