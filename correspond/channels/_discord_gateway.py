"""Discord's gateway, as a short listening window that resumes rather than re-identifies.

The default ``gateway=`` of the Discord adapter. One call opens the gateway, resumes the
session in ``state`` when there is one (Discord then replays what was missed), collects
dispatches until the connection has been quiet for ``wait`` seconds, and returns them with
the state the next call resumes from. Identifying again is the fallback, not the rule:
Discord resets a bot token after 1,000 identifies in a day, so a listener that
re-identified on every poll would lose its token within a day of one-minute polls.

The protocol is spoken over an injected ``connect(url, *, timeout)`` returning an object
with ``send(text)``, ``recv(timeout=None) -> text`` (raising ``TimeoutError`` when quiet) and
``close()``; the default wraps websocket-client (the ``discord`` extra).
"""

from __future__ import annotations

import json
import time
from urllib.parse import urlsplit
from collections.abc import Callable, Mapping
from typing import Any

from correspond.errors import ChannelError, MissingRequirement

__all__ = [
    "GATEWAY_URL",
    "DEFAULT_INTENTS",
    "LISTENED_EVENTS",
    "gateway_window",
    "websocket_connect",
]

GATEWAY_URL = "wss://gateway.discord.gg"
GATEWAY_QUERY = "?v=10&encoding=json"
#: GUILDS (1) | GUILD_MESSAGES (1 << 9) | DIRECT_MESSAGES (1 << 12) | MESSAGE_CONTENT (1 << 15).
DEFAULT_INTENTS = (1 << 0) | (1 << 9) | (1 << 12) | (1 << 15)
#: The dispatches a listener keeps; the rest of the gateway's traffic is dropped.
LISTENED_EVENTS = ("MESSAGE_CREATE", "MESSAGE_UPDATE", "MESSAGE_DELETE")

# Gateway opcodes.
OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_RESUME = 6
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11
#: Hosts a resume URL may name: the token is sent there, so a tampered cursor must not redirect it.
GATEWAY_HOST_SUFFIX = ".discord.gg"
#: How long a window waits for READY or RESUMED, even when ``wait`` is shorter.
READY_TIMEOUT_S = 15.0
#: Identifies one listener may spend in a day before it refuses (Discord's limit is 1,000 a token).
MAX_IDENTIFIES_PER_DAY = 100
DAY_S = 86_400
#: Close codes after which reconnecting cannot help (bad token, bad intents, ...).
FATAL_CLOSE_CODES = frozenset({4004, 4010, 4011, 4012, 4013, 4014})


class _Quiet(Exception):
    """The connection said nothing for the whole wait."""


def websocket_connect(url: str, *, timeout: float) -> Any:
    """A websocket-client connection whose ``recv`` raises ``TimeoutError`` when quiet (the default ``connect=``)."""
    try:
        import websocket
    except ImportError:
        raise MissingRequirement(
            "discord",
            "gateway listening needs the websocket-client package",
            fix="pip install 'correspond[discord]'",
            kind="unavailable",
        ) from None

    class Connection:
        def __init__(self):
            try:
                self._ws = websocket.create_connection(url, timeout=timeout)
            except (OSError, websocket.WebSocketException) as error:
                raise ChannelError(
                    f"cannot reach Discord's gateway: {type(error).__name__}",
                    kind="network",
                    retryable=True,
                ) from None

        def send(self, text: str) -> None:
            self._ws.send(text)

        def recv(self, timeout: float | None = None) -> str:
            if timeout is not None:
                self._ws.settimeout(timeout)
            try:
                frame = self._ws.recv()
            except websocket.WebSocketTimeoutException:
                raise TimeoutError from None
            except websocket.WebSocketConnectionClosedException:
                raise ConnectionError(getattr(self._ws, "status", None)) from None
            return frame if isinstance(frame, str) else frame.decode()

        def close(self) -> None:
            self._ws.close()

    return Connection()


def _discord_gateway_url(url: Any) -> bool:
    """Whether ``url`` is a ``wss://`` URL on one of Discord's gateway hosts.

    >>> _discord_gateway_url("wss://gateway-us-east1-b.discord.gg"), _discord_gateway_url("wss://discord.gg.example.org")
    (True, False)
    """
    try:
        parts = urlsplit(str(url))
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    # The bare discord.gg is the invite domain, not a gateway.
    return parts.scheme == "wss" and host.endswith(GATEWAY_HOST_SUFFIX)


def _resumable(state: Mapping[str, Any] | None) -> bool:
    return bool(
        state
        and state.get("session_id")
        and _discord_gateway_url(state.get("resume_gateway_url"))
        and state.get("seq") is not None
    )


def gateway_window(
    token: str,
    state: Mapping[str, Any] | None = None,
    *,
    wait: float,
    intents: int = DEFAULT_INTENTS,
    connect: Callable[..., Any] = websocket_connect,
    clock: Callable[[], float] = time.monotonic,
    wall: Callable[[], float] = time.time,
    ready_timeout: float = READY_TIMEOUT_S,
    max_identifies_per_day: int = MAX_IDENTIFIES_PER_DAY,
) -> tuple[list[dict], dict]:
    """Dispatches (``{"t": ..., "d": ...}``) seen within a window of ``wait`` quiet seconds, and the state to resume from.

    ``state`` is what the previous call returned (``session_id``, ``seq``,
    ``resume_gateway_url``); with it the session is resumed, without it (or when Discord
    refuses the resume) the bot identifies, at most once per window and at most
    ``max_identifies_per_day`` times a day per state (``identified_at`` keeps the times),
    after which a retryable ``rate_limited`` error stands in for the identify. The window
    does not end on silence before the session is ready (READY or RESUMED), up to
    ``ready_timeout`` seconds, and it heartbeats on schedule while it waits.
    """
    state = dict(state or {})
    resuming = _resumable(state)
    base = state["resume_gateway_url"] if resuming else GATEWAY_URL
    if not resuming:
        _spend_identify(state, wall(), max_identifies_per_day)  # refuses before connecting
    connection = connect(base.rstrip("/") + "/" + GATEWAY_QUERY, timeout=wait)
    dispatches: list[dict] = []
    heartbeat_s = None
    next_beat = None
    ready = False
    identified = not resuming  # the identify spent above, sent on HELLO
    started = clock()
    quiet_until = started + wait

    def beat():
        connection.send(json.dumps({"op": OP_HEARTBEAT, "d": state.get("seq")}))

    try:
        while True:
            now = clock()
            deadline = quiet_until if ready else max(quiet_until, started + ready_timeout)
            if next_beat is not None and now >= next_beat:
                beat()
                next_beat = now + heartbeat_s
            if now >= deadline:
                break
            timeout = deadline - now
            if next_beat is not None:
                timeout = min(timeout, max(next_beat - now, 0.01))
            try:
                frame = json.loads(_recv(connection, timeout))
            except TimeoutError:
                continue
            except ConnectionError as closed:
                code = closed.args[0] if closed.args else None
                if code in FATAL_CLOSE_CODES:
                    raise ChannelError(
                        f"Discord closed the gateway with code {code} (token or intents refused)",
                        kind="auth" if code == 4004 else "permission",
                    ) from None
                break  # the next window resumes
            quiet_until = clock() + wait
            op, data = frame.get("op"), frame.get("d")
            if frame.get("s") is not None:
                state["seq"] = frame["s"]
            if op == OP_HELLO:
                heartbeat_s = (data or {}).get("heartbeat_interval", 0) / 1000 or None
                next_beat = clock() + heartbeat_s if heartbeat_s else None
                if resuming:
                    payload = {
                        "token": token,
                        "session_id": state["session_id"],
                        "seq": state["seq"],
                    }
                    connection.send(json.dumps({"op": OP_RESUME, "d": payload}))
                else:
                    connection.send(json.dumps(_identify(token, intents)))
            elif op == OP_DISPATCH:
                kind = frame.get("t")
                if kind == "READY":
                    ready = True
                    state["session_id"] = data["session_id"]
                    resume_url = data.get("resume_gateway_url")
                    # Anything else is never trusted with the token: resume on the main gateway.
                    state["resume_gateway_url"] = (
                        resume_url if _discord_gateway_url(resume_url) else GATEWAY_URL
                    )
                elif kind == "RESUMED":
                    ready = True
                elif kind in LISTENED_EVENTS:
                    dispatches.append({"t": kind, "d": data})
            elif op == OP_HEARTBEAT:
                beat()
            elif op == OP_INVALID_SESSION:
                if data:
                    break  # resumable after a pause Discord asks for: the next window resumes
                for key in ("session_id", "resume_gateway_url", "seq"):
                    state.pop(key, None)
                if identified:
                    break  # at most one identify per window
                _spend_identify(state, wall(), max_identifies_per_day)
                identified, resuming = True, False
                connection.send(json.dumps(_identify(token, intents)))
            elif op == OP_RECONNECT:
                break  # the next window resumes at resume_gateway_url
    finally:
        connection.close()
    return dispatches, state


def _recv(connection: Any, timeout: float) -> str:
    """One frame, waiting at most ``timeout`` seconds when the connection can be told so."""
    try:
        return connection.recv(timeout=timeout)
    except TypeError:  # a connection whose recv takes no timeout
        return connection.recv()


def _spend_identify(state: dict, now: float, cap: int) -> None:
    """Record one identify in ``state``, or refuse it when today's budget is spent."""
    recent = [t for t in state.get("identified_at", ()) if now - t < DAY_S]
    if len(recent) >= cap:
        raise ChannelError(
            f"{len(recent)} gateway identifies in the last day for this listener; refusing more "
            f"(Discord resets the token at 1,000): wait, or listen over REST",
            kind="rate_limited",
            retryable=True,
        )
    state["identified_at"] = [*recent, now]
    state["identifies"] = state.get("identifies", 0) + 1


def _identify(token: str, intents: int) -> dict:
    return {
        "op": OP_IDENTIFY,
        "d": {
            "token": token,
            "intents": intents,
            "properties": {"os": "linux", "browser": "correspond", "device": "correspond"},
        },
    }
