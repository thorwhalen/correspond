"""A websocket-client connection with a quiet-aware ``recv``: the default ``connect=`` of correspond's socket listeners.

``websocket_connect(url, timeout=..., extra=..., service=...)`` returns an object with
``send(text)``, ``recv(timeout=None) -> text`` (raising ``TimeoutError`` when the connection
stays quiet, ``ConnectionError`` when it closes or fails) and ``close()``. With
``control_frames=True``, ``recv`` also returns ``""`` for a ping (answered with a pong), so
a caller keeps control of its own deadline on a connection the server keeps alive.
websocket-client is an optional dependency, so its absence is a
:class:`~correspond.errors.MissingRequirement` naming the extra that brings it
(:func:`require_websocket` checks it before anything is spent on a connection).
"""

from __future__ import annotations

from typing import Any

from correspond.errors import ChannelError, MissingRequirement

__all__ = ["require_websocket", "websocket_connect"]


def require_websocket(*, extra: str, service: str) -> Any:
    """The ``websocket`` module (websocket-client), or a :class:`MissingRequirement` naming ``extra``."""
    try:
        import websocket
    except ImportError:
        raise MissingRequirement(
            extra,
            f"listening over {service} needs the websocket-client package",
            fix=f"pip install 'correspond[{extra}]'",
            kind="unavailable",
        ) from None
    return websocket


def websocket_connect(
    url: str,
    *,
    timeout: float,
    extra: str,
    service: str,
    control_frames: bool = False,
) -> Any:
    """A websocket-client connection to ``url`` whose ``recv`` raises ``TimeoutError`` when quiet.

    ``extra`` is the correspond extra that installs websocket-client (and the channel the
    missing requirement is reported for); ``service`` names what is being reached, in errors.
    """
    websocket = require_websocket(extra=extra, service=service)
    failures = (websocket.WebSocketException, OSError)

    class Connection:
        def __init__(self):
            try:
                self._ws = websocket.create_connection(url, timeout=timeout)
            except (OSError, websocket.WebSocketException) as error:
                raise ChannelError(
                    f"cannot reach {service}: {type(error).__name__}",
                    kind="network",
                    retryable=True,
                ) from None

        def send(self, text: str) -> None:
            try:
                self._ws.send(text)
            except failures as error:
                raise ConnectionError(type(error).__name__) from None

        def recv(self, timeout: float | None = None) -> str:
            try:
                if timeout is not None:
                    self._ws.settimeout(timeout)
                if control_frames:
                    opcode, frame = self._ws.recv_data(control_frame=True)
                    if opcode == websocket.ABNF.OPCODE_CLOSE:
                        raise ConnectionError(getattr(self._ws, "status", None))
                    if opcode not in (
                        websocket.ABNF.OPCODE_TEXT,
                        websocket.ABNF.OPCODE_BINARY,
                    ):
                        return ""  # a ping (answered) or a pong: alive, nothing to read
                else:
                    frame = self._ws.recv()
            except (websocket.WebSocketTimeoutException, TimeoutError):
                raise TimeoutError from None
            except websocket.WebSocketConnectionClosedException:
                raise ConnectionError(getattr(self._ws, "status", None)) from None
            except failures as error:
                raise ConnectionError(type(error).__name__) from None
            return frame if isinstance(frame, str) else frame.decode()

        def close(self) -> None:
            self._ws.close()

    return Connection()
