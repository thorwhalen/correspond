"""The ``before_send`` check: what every send and edit runs, with the conversation's audience, before anything leaves.

A check is a callable ``before_send(ref, draft, audience) -> None``. Returning lets the write
go ahead; raising :class:`~correspond.errors.Refused` blocks it; raising
:class:`~correspond.errors.NeedsApproval` holds it for the operator. :func:`correspond.send`
and :func:`correspond.edit` compute the audience, run the check on the real path and on the
dry run alike, and put both into the plan, so a dry run shows the verdict a send would get.

Which check runs:

1. the ``before_send=`` argument, when a Python caller passes one;
2. else ``before_send = "module:attr"`` at the top of the config file, imported when a write
   first needs it. A value that does not import, or is not callable, stops every write
   (``before_send_unavailable``): a configured check that cannot run is never skipped;
3. else :func:`notice`, which lets the write go ahead: the audience line it adds to the plan
   is the whole of it.

Anything else the check does (raise another exception, return a value) also stops the write
(``before_send_failed``). liaise supplies a real check (``liaise.vet.before_send``). The
command line and the MCP tools take no such argument: a model driving them cannot choose or
skip the check.

>>> resolve(None, config={})[1]
'correspond.outbound:notice'
>>> resolve(None, config={"before_send": "no.such.module:check"})
Traceback (most recent call last):
    ...
correspond.errors.BeforeSendUnavailable: before_send = 'no.such.module:check' in the config does not import (ModuleNotFoundError: No module named 'no'), so nothing is sent
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Mapping
from typing import Any

from correspond.errors import (
    BeforeSendFailed,
    BeforeSendUnavailable,
    CorrespondError,
    Stopped,
)
from correspond.model import Audience, ConversationRef, Draft

__all__ = ["CONFIG_KEY", "DEFAULT", "BeforeSend", "check", "notice", "resolve"]

#: A ``before_send`` check: returns ``None`` to let the write go ahead, raises ``Refused`` or ``NeedsApproval`` to stop it.
BeforeSend = Callable[[ConversationRef, Draft, Audience], None]
#: The top-level key of the config file naming the check, as ``"module:attr"``.
CONFIG_KEY = "before_send"
#: The check that runs when neither the argument nor the config names one.
DEFAULT = "correspond.outbound:notice"


def notice(ref: ConversationRef, draft: Draft, audience: Audience) -> None:
    """The default check: every write goes ahead, and its plan and summary say who can read it."""
    return None


def _name_of(func: Callable) -> str:
    module = getattr(func, "__module__", None) or "?"
    return f"{module}:{getattr(func, '__qualname__', type(func).__name__)}"


def _load(reference: Any) -> BeforeSend:
    where = f"before_send = {reference!r} in the config"
    if not isinstance(reference, str) or reference.count(":") != 1:
        raise BeforeSendUnavailable(
            f'{where} is not a "module:attr" reference, so nothing is sent'
        )
    module_name, _, attr = reference.strip().partition(":")
    try:
        found: Any = importlib.import_module(module_name)
        for part in attr.split("."):
            found = getattr(found, part)
    except Exception as error:  # an import can fail in any way; each one means no check
        raise BeforeSendUnavailable(
            f"{where} does not import ({type(error).__name__}: {error}), so nothing is sent"
        ) from None
    if not callable(found):
        raise BeforeSendUnavailable(
            f"{where} names a {type(found).__name__}, not a callable, so nothing is sent"
        )
    return found


def resolve(
    before_send: BeforeSend | None = None, *, config: Mapping[str, Any] | None = None
) -> tuple[BeforeSend, str]:
    """The check to run and the name it is reported by: the argument, else the config's, else :func:`notice`.

    Raises :class:`~correspond.errors.BeforeSendUnavailable` when the config names a check
    that cannot be loaded, or cannot itself be read.
    """
    if before_send is not None:
        if not callable(before_send):
            raise BeforeSendUnavailable(
                f"before_send must be callable, not {type(before_send).__name__}, so nothing is sent"
            )
        return before_send, _name_of(before_send)
    if config is None:
        from correspond.settings import load_config

        try:
            config = load_config()
        except CorrespondError as error:
            raise BeforeSendUnavailable(
                f"{error}; the before_send check it may name is unknown, so nothing is sent"
            ) from None
    if CONFIG_KEY not in config:
        return notice, DEFAULT
    reference = config[CONFIG_KEY]
    return _load(reference), str(reference).strip()


def _verdict_line(name: str | None, stopped: Stopped | None) -> str:
    if stopped is not None:
        return f"{stopped.error_kind}: {stopped.reason}" + (f" ({name})" if name else "")
    if name == DEFAULT:
        return f"no check configured ({DEFAULT} shows the audience and lets the write go ahead)"
    return f"passed ({name})"


def check(
    ref: ConversationRef,
    draft: Draft,
    *,
    before_send: BeforeSend | None = None,
    registry: Mapping[str, Any] | None = None,
) -> tuple[dict, Stopped | None]:
    """Compute the audience, run the check, and return the plan lines with what stopped the write (``None``: it may go ahead).

    The audience is computed first, so a write that cannot be checked still shows who it
    would have reached. Never raises for anything the check does.
    """
    from correspond.ops import audience as compute_audience

    found = compute_audience(ref, draft, registry=registry)
    name: str | None = None  # known once the check is resolved
    stopped: Stopped | None = None
    try:
        func, name = resolve(before_send)
        answer = func(ref, draft, found)
        if answer is not None:
            raise BeforeSendFailed(
                f"the check returned {answer!r}; a check returns None to let the write go ahead, "
                "or raises Refused or NeedsApproval"
            )
    except Stopped as error:
        stopped = error
    except Exception as error:  # a check that crashes has not let the write through
        stopped = BeforeSendFailed(f"the check raised {type(error).__name__}: {error}")
    lines = {
        "audience": found.in_words(),
        "audience_hash": found.hash,
        "before_send": _verdict_line(name, stopped),
    }
    return lines, stopped
