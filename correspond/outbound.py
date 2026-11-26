"""The ``before_send`` check: what every write runs, with the conversation's audience, before anything leaves.

A check is a callable::

    before_send(ref, draft, audience, *, operation, dry_run, message_id, **context) -> None

``operation`` is ``send``, ``edit``, ``react``, ``upload``, ``label``, ``unlabel``, ``close``, ``reopen``,
``set_webhook`` (its draft's text the URL that will receive every update) or
``delete_webhook``; ``message_id`` is the message
an edit or a reaction targets (``None`` otherwise); a reaction's draft text is the reaction,
an upload's is the file name. Accept ``**context``: later versions may pass more. Returning
lets the write go ahead; raising :class:`~correspond.errors.Refused` blocks it; raising
:class:`~correspond.errors.NeedsApproval` holds it for the operator; either may carry keyword
details, which go into the plan. The write verbs of :mod:`correspond.ops` compute the
audience, run the check once on the real path or on the dry run, and put both into the plan,
so a dry run shows the verdict a send would get.

Which check runs:

1. the ``before_send=`` argument, when a Python caller passes one;
2. else ``before_send = "module:attr"`` at the top of the config file, imported when a write
   first needs it. A value that does not import, or is not callable, stops every write
   (``before_send_unavailable``): a configured check that cannot run is never skipped. So
   does a ``before_send`` key inside a table, where TOML puts a line written after one;
3. else :func:`notice`, which lets the write go ahead: the audience line it adds to the plan
   is the whole of it.

A caller that promises every write was checked (an unattended writer, a connector) asks for a
real check: ``require_check=True`` on the write verbs, ``--require-check`` on the command
line, or ``CORRESPOND_REQUIRE_CHECK=1`` in the environment (any value but empty, ``0``,
``false``, ``no`` or ``off``). Then a write whose check resolves to :func:`notice`, by any of
the three steps (also through a bound ``__call__``, a :func:`functools.partial` or a
:func:`functools.wraps` wrapper of it), stops with ``before_send_unavailable``. Either source
can only add the requirement: an argument of ``False`` does not lift the environment's. A
function of the operator's own that checks nothing still counts as a check: whoever writes
the config chooses it.

A send replayed by its ``idempotency_key`` answers the earlier result and posts nothing, so
no check runs for it; that earlier result's plan says which check it went out under.

The plan's ``before_send_check`` line names the check that ran (``module:attr``), on every
write, so a log of results shows which writes went out under :func:`notice`.

Anything else the check does (raise another exception, exit, return a value, stop with an
``error_kind`` outside :data:`~correspond.errors.CHECK_KINDS`) also stops the write
(``before_send_failed``). liaise supplies a real check (``liaise.vet.before_send``).

The command line and the MCP tools take no ``before_send`` argument. The check guards
drafts, not the process running correspond: whoever sets its environment or edits the
config file (``$CORRESPOND_CONFIG`` can name another file) chooses the check, and a write
made without correspond never meets it. liaise's hook covers those.

>>> resolve(None, config={})[1]
'correspond.outbound:notice'
>>> resolve(None, config={}, require=True)
Traceback (most recent call last):
    ...
correspond.errors.CheckRequired: no check configured, and this caller requires one (require_check, --require-check or $CORRESPOND_REQUIRE_CHECK): name one with before_send = "module:attr" at the top of the config; until then nothing is sent
>>> resolve(None, config={"before_send": "no.such.module:check"})
Traceback (most recent call last):
    ...
correspond.errors.BeforeSendUnavailable: before_send = 'no.such.module:check' in the config does not import (ModuleNotFoundError: No module named 'no'), so nothing is sent
"""

from __future__ import annotations

import functools
import importlib
import inspect
import os
from collections.abc import Callable, Mapping
from typing import Any

from correspond.errors import (
    CHECK_KINDS,
    BeforeSendFailed,
    BeforeSendUnavailable,
    CheckRequired,
    CorrespondError,
    Stopped,
)
from correspond.model import Audience, ConversationRef, Draft

__all__ = [
    "CONFIG_KEY",
    "DEFAULT",
    "REQUIRE_CHECK_ENVVAR",
    "BeforeSend",
    "check",
    "check_required",
    "notice",
    "resolve",
]

#: A ``before_send`` check, called ``(ref, draft, audience, *, operation, dry_run, message_id)``: returns ``None`` to let the write go ahead, raises ``Refused`` or ``NeedsApproval`` to stop it.
BeforeSend = Callable[..., None]
#: The top-level key of the config file naming the check, as ``"module:attr"``.
CONFIG_KEY = "before_send"
#: The check that runs when neither the argument nor the config names one.
DEFAULT = "correspond.outbound:notice"
#: The environment variable that makes every write in the process require a real check.
REQUIRE_CHECK_ENVVAR = "CORRESPOND_REQUIRE_CHECK"
#: The values of :data:`REQUIRE_CHECK_ENVVAR` that leave the requirement off; any other value turns it on.
_OFF = frozenset({"", "0", "false", "no", "off"})


def notice(
    ref: ConversationRef, draft: Draft, audience: Audience, **context: Any
) -> None:
    """The default check: every write goes ahead, and its plan and summary say who can read it."""
    return None


def check_required(require: bool = False) -> bool:
    """Whether a write must run a real check: ``require``, or :data:`REQUIRE_CHECK_ENVVAR` set to anything but an off value.

    An unrecognised value turns the requirement on: a typo must not lift it.

    >>> check_required(True)
    True
    """
    value = os.environ.get(REQUIRE_CHECK_ENVVAR, "")
    return bool(require) or value.strip().lower() not in _OFF


def _is_notice(func: Any) -> bool:
    """Whether ``func`` is :func:`notice` under another name: its bound ``__call__``, a partial, or a ``functools.wraps`` wrapper.

    >>> _is_notice(functools.partial(notice)), _is_notice(notice.__call__), _is_notice(print)
    (True, True, False)
    """
    seen: set[int] = set()
    while id(func) not in seen:
        if func is notice:
            return True
        seen.add(id(func))
        if isinstance(func, functools.partial):
            func = func.func
        elif getattr(func, "__self__", None) is not None:
            func = func.__self__
        elif getattr(func, "__wrapped__", None) is not None:
            func = inspect.unwrap(func)
        else:
            return False
    return False


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
    before_send: BeforeSend | None = None,
    *,
    config: Mapping[str, Any] | None = None,
    require: bool = False,
) -> tuple[BeforeSend, str]:
    """The check to run and the name it is reported by: the argument, else the config's, else :func:`notice`.

    Raises :class:`~correspond.errors.BeforeSendUnavailable` when the config names a check
    that cannot be loaded, or cannot itself be read, and, when a real check is required
    (``require`` or :data:`REQUIRE_CHECK_ENVVAR`, see :func:`check_required`), when the
    check found is :func:`notice`.
    """
    func, name = _resolve(before_send, config=config)
    if _is_notice(func):
        name = DEFAULT  # one name for "no real check", whatever alias the config used
    if name == DEFAULT and check_required(require):
        raise CheckRequired(
            "no check configured, and this caller requires one "
            f"(require_check, --require-check or ${REQUIRE_CHECK_ENVVAR}): "
            f'name one with {CONFIG_KEY} = "module:attr" at the top of the config; '
            "until then nothing is sent"
        )
    return func, name


def _resolve(
    before_send: BeforeSend | None, *, config: Mapping[str, Any] | None
) -> tuple[BeforeSend, str]:
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
        misplaced = sorted(
            name
            for name, table in config.items()
            if isinstance(table, Mapping) and CONFIG_KEY in table
        )
        if misplaced:
            raise BeforeSendUnavailable(
                f"before_send is under [{misplaced[0]}] in the config, where it counts for nothing: "
                "move it to the top of the file, before any table; until then nothing is sent"
            )
        return notice, DEFAULT
    reference = config[CONFIG_KEY]
    return _load(reference), str(reference).strip()


def _verdict_line(name: str | None, stopped: Stopped | None) -> str:
    if stopped is not None:
        return f"{stopped.error_kind}: {stopped.reason}" + (f" ({name})" if name else "")
    if name == DEFAULT:
        return f"no check configured ({DEFAULT} shows the audience and lets the write go ahead)"
    return f"passed ({name})"


def _plain(value: Any) -> Any:
    return value if isinstance(value, (str, int, float, bool, type(None))) else str(value)


def check(
    ref: ConversationRef,
    draft: Draft,
    *,
    operation: str = "send",
    dry_run: bool = False,
    message_id: str | None = None,
    before_send: BeforeSend | None = None,
    require_check: bool = False,
    registry: Mapping[str, Any] | None = None,
) -> tuple[dict, Stopped | None]:
    """Compute the audience, run the check, and return the plan lines with what stopped the write (``None``: it may go ahead).

    The audience is computed first, so a write that cannot be checked still shows who it
    would have reached. ``require_check`` stops the write when no real check would run
    (see :func:`resolve`). Never raises for anything the check does (``KeyboardInterrupt``
    aside, which stops everything).
    """
    from correspond.ops import audience as compute_audience

    found = compute_audience(ref, draft, registry=registry)
    name: str | None = (
        None  # known once the check is resolved (or stopped for want of one)
    )
    stopped: Stopped | None = None
    try:
        func, name = resolve(before_send, require=require_check)
        answer = func(
            ref,
            draft,
            found,
            operation=operation,
            dry_run=dry_run,
            message_id=message_id,
        )
        if answer is not None:
            raise BeforeSendFailed(
                f"the check returned {answer!r}; a check returns None to let the write go ahead, "
                "or raises Refused or NeedsApproval"
            )
    except Stopped as error:
        stopped = error
        if isinstance(error, CheckRequired):
            name = DEFAULT
        if error.error_kind not in CHECK_KINDS:
            stopped = BeforeSendFailed(
                f"the check stopped the write with error_kind {error.error_kind!r}, "
                f"which is not one of {CHECK_KINDS}: {error.reason}",
                **error.details,
            )
    except (
        Exception,
        SystemExit,
    ) as error:  # a check that crashes or exits let nothing through
        stopped = BeforeSendFailed(f"the check raised {type(error).__name__}: {error}")
    lines: dict[str, Any] = {
        "audience": found.in_words(),
        "audience_hash": found.hash,
        "before_send": _verdict_line(name, stopped),
        "before_send_check": name,
    }
    if stopped is not None and stopped.details:
        lines["before_send_details"] = {
            str(k): _plain(v) for k, v in stopped.details.items()
        }
    return lines, stopped
