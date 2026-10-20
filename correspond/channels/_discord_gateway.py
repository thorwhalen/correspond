"""Discord's gateway, as a short listening window that resumes rather than re-identifies.

The default ``gateway=`` of the Discord adapter. One call opens the gateway, resumes the
session in ``state`` when there is one (Discord then replays what was missed), collects
dispatches until the connection has been quiet for ``wait`` seconds, and returns them with
the state the next call resumes from. Identifying again is the fallback, not the rule:
Discord resets a bot token after 1,000 identifies in a day, so a listener that
re-identified on every poll would lose its token within a day of one-minute polls.

The protocol is spoken over an injected ``connect(url, *, timeout)`` returning an object
with ``send(text)``, ``recv() -> text`` (raising ``TimeoutError`` when quiet) and
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

        def recv(self) -> str:
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
    return parts.scheme == "wss" and (
        host == GATEWAY_HOST_SUFFIX[1:] or host.endswith(GATEWAY_HOST_SUFFIX)
    )


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
) -> tuple[list[dict], dict]:
    """Dispatches (``{"t": ..., "d": ...}``) seen within a window of ``wait`` quiet seconds, and the state to resume from.

    ``state`` is what the previous call returned (``session_id``, ``seq``,
    ``resume_gateway_url``); with it the session is resumed, without it (or when Discord
    refuses the resume) the bot identifies. The returned state also counts
    ``identifies``, so a caller can see how often the fallback ran.
    """
    state = dict(state or {})
    resuming = _resumable(state)
    base = state["resume_gateway_url"] if resuming else GATEWAY_URL
    connection = connect(base.rstrip("/") + "/" + GATEWAY_QUERY, timeout=wait)
    dispatches: list[dict] = []
    heartbeat_s = None
    next_beat = None
    try:
        while True:
            try:
                frame = json.loads(connection.recv())
            except TimeoutError:
                break
            except ConnectionError as closed:
                code = closed.args[0] if closed.args else None
                if code in FATAL_CLOSE_CODES:
                    raise ChannelError(
                        f"Discord closed the gateway with code {code} (token or intents refused)",
                        kind="auth" if code == 4004 else "permission",
                    ) from None
                break  # the next window resumes
            op, data = frame.get("op"), frame.get("d")
            if frame.get("s") is not None:
                state["seq"] = frame["s"]
            if op == OP_HELLO:
                heartbeat_s = (data or {}).get("heartbeat_interval", 0) / 1000
                next_beat = clock() + heartbeat_s
                if resuming:
                    payload = {
                        "token": token,
                        "session_id": state["session_id"],
                        "seq": state["seq"],
                    }
                    connection.send(json.dumps({"op": OP_RESUME, "d": payload}))
                else:
                    connection.send(json.dumps(_identify(token, intents)))
                    state["identifies"] = state.get("identifies", 0) + 1
            elif op == OP_DISPATCH:
                kind = frame.get("t")
                if kind == "READY":
                    state["session_id"] = data["session_id"]
                    resume_url = data.get("resume_gateway_url")
                    if _discord_gateway_url(resume_url):
                        state["resume_gateway_url"] = resume_url
                    else:
                        state.pop("resume_gateway_url", None)
                elif kind in LISTENED_EVENTS:
                    dispatches.append({"t": kind, "d": data})
            elif op == OP_HEARTBEAT:
                connection.send(json.dumps({"op": OP_HEARTBEAT, "d": state.get("seq")}))
            elif op == OP_INVALID_SESSION:
                # d is whether the session may still be resumed; when it may not, identify.
                if not data:
                    for key in ("session_id", "resume_gateway_url", "seq"):
                        state.pop(key, None)
                resuming = bool(data) and _resumable(state)
                if not resuming:
                    connection.send(json.dumps(_identify(token, intents)))
                    state["identifies"] = state.get("identifies", 0) + 1
            elif op == OP_RECONNECT:
                break  # the next window resumes at resume_gateway_url
            if next_beat is not None and clock() >= next_beat:
                connection.send(json.dumps({"op": OP_HEARTBEAT, "d": state.get("seq")}))
                next_beat = clock() + heartbeat_s
    finally:
        connection.close()
    return dispatches, state


def _identify(token: str, intents: int) -> dict:
    return {
        "op": OP_IDENTIFY,
        "d": {
            "token": token,
            "intents": intents,
            "properties": {"os": "linux", "browser": "correspond", "device": "correspond"},
        },
    }
