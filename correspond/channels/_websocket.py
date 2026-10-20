"""A websocket-client connection with a quiet-aware ``recv``: the default ``connect=`` of correspond's socket listeners.

``websocket_connect(url, timeout=..., extra=..., service=...)`` returns an object with
``send(text)``, ``recv(timeout=None) -> text`` (raising ``TimeoutError`` when the connection
stays quiet, ``ConnectionError`` when it closes) and ``close()``. websocket-client is an
optional dependency, so its absence is a :class:`~correspond.errors.MissingRequirement`
naming the extra that brings it.
"""

from __future__ import annotations

from typing import Any

from correspond.errors import ChannelError, MissingRequirement

__all__ = ["websocket_connect"]


def websocket_connect(
    url: str, *, timeout: float, extra: str, service: str
) -> Any:
    """A websocket-client connection to ``url`` whose ``recv`` raises ``TimeoutError`` when quiet.

    ``extra`` is the correspond extra that installs websocket-client (and the channel the
    missing requirement is reported for); ``service`` names what is being reached, in errors.
    """
    try:
        import websocket
    except ImportError:
        raise MissingRequirement(
            extra,
            f"listening over {service} needs the websocket-client package",
            fix=f"pip install 'correspond[{extra}]'",
            kind="unavailable",
        ) from None

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
