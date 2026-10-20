"""What correspond's webhook receivers share (Telegram's, GitHub's): their store, and how they are served.

A receiver is a plain ASGI app (:mod:`correspond.channels._asgi`) that stores verified
deliveries under the data root; ``listen`` reads the same store. :func:`configured_store`
is that store, :func:`serve_receiver` runs a receiver on this machine unless the channel's
``webhook_allow_remote`` setting (or ``allow_remote=``) says otherwise, and
:func:`serve_main` is the console script around it.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Awaitable, Callable, Mapping, MutableMapping
from typing import Any

from correspond.errors import ChannelError, MissingRequirement
from correspond.registry import load_factory, value

WEBHOOK_HOST = "127.0.0.1"
_TRUE = ("1", "true", "yes", "on")


def configured_store(name: str, kind: str) -> MutableMapping[str, Any]:
    """The channel's ``webhook_store`` setting's ``module:attr``, else JSON files under ``<data root>/<kind>/``.

    The reference names a ``MutableMapping``, or a callable returning one, whose values are
    JSON-ready dicts: a bytes store, such as a plain ``s3dol`` one, needs a JSON codec
    around it. The receiver and ``listen`` read the same setting, so they meet in the same
    store.
    """
    reference = value(name, "webhook_store")
    if not reference:
        from correspond.stores import json_store

        return json_store(kind)
    if ":" not in str(reference):
        raise ChannelError(
            f'webhook_store is a "module:attr" reference, not {reference!r}',
            kind="validation",
        )
    try:
        found = load_factory(str(reference))
    except (ImportError, AttributeError) as error:
        raise ChannelError(
            f"webhook_store {reference!r} could not be loaded: {error}",
            kind="validation",
        ) from None
    store = found() if callable(found) and not isinstance(found, Mapping) else found
    if not isinstance(store, MutableMapping):
        raise ChannelError(
            f"webhook_store {reference!r} gave a {type(store).__name__}, not a mutable mapping",
            kind="validation",
        )
    return store


def is_loopback(host: str) -> bool:
    """Whether ``host`` is this machine only (``localhost`` or a loopback address)."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def serve_receiver(
    app_factory: Callable[[], Callable[..., Awaitable[None]]],
    *,
    name: str,
    host: str,
    port: int,
    allow_remote: bool | None = None,
    app: Callable[..., Awaitable[None]] | None = None,
    run: Callable[..., Any] | None = None,
) -> None:
    """Run a receiver (with uvicorn) on ``host``, this machine unless ``allow_remote``.

    ``allow_remote`` defaults to the channel's ``webhook_allow_remote`` setting; ``app``
    defaults to ``app_factory()``. Proxy headers are ignored, since a receiver needs no
    client address, and one worker keeps the arrival order.
    """
    if allow_remote is None:
        allow_remote = (
            str(value(name, "webhook_allow_remote") or "").strip().lower() in _TRUE
        )
    if not allow_remote and not is_loopback(host):
        raise ChannelError(
            f"the webhook receiver binds to this machine only ({WEBHOOK_HOST}), not {host!r}, "
            "unless webhook_allow_remote is set",
            kind="validation",
        )
    if run is None:
        try:
            import uvicorn
        except ImportError:
            raise MissingRequirement(
                name,
                "uvicorn, to serve the webhook receiver",
                fix="pip install 'correspond[server]'",
            ) from None
        run = uvicorn.run
    run(app or app_factory(), host=host, port=port, workers=1, proxy_headers=False)


def serve_main(
    serve: Callable[..., None],
    *,
    prog: str,
    description: str,
    port: int,
    argv: list[str] | None = None,
) -> None:
    """The console script of a receiver: ``--host``, ``--port``, ``--allow-remote``, then ``serve``."""
    import argparse

    parser = argparse.ArgumentParser(prog=prog, description=description)
    parser.add_argument("--host", default=WEBHOOK_HOST, help=f"default {WEBHOOK_HOST}")
    parser.add_argument("--port", type=int, default=port, help=f"default {port}")
    parser.add_argument(
        "--allow-remote",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="bind to an address other machines reach, or --no-allow-remote to refuse (else the webhook_allow_remote setting decides)",
    )
    args = parser.parse_args(argv)
    try:
        serve(host=args.host, port=args.port, allow_remote=args.allow_remote)
    except ChannelError as error:
        parser.exit(1, f"{prog}: {error}\n")
