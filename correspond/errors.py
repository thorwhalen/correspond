"""What correspond raises, and the vocabulary a failed write reports.

Two families, on purpose:

- **Mistakes a caller fixes in code or configuration raise**: an unknown channel
  (:class:`UnknownChannel`), a malformed conversation reference (:class:`InvalidRef`), an
  operation the channel does not have (:class:`NotSupported`). A retry changes nothing.
- **Failures of the platform or of the message** are :class:`ChannelError` inside an
  adapter, carrying an ``kind`` from :data:`ERROR_KINDS`, ``retryable`` and
  ``retry_after``. Reads let them propagate; the write verbs turn them into a
  ``SendResult`` with ``ok=False``, so a notifier never crashes its caller.

A third family stops a write before it leaves: the ``before_send`` check (see
:mod:`correspond.outbound`) raises :class:`Refused` or :class:`NeedsApproval`, and the write
verbs report it as a ``SendResult`` whose ``error_kind`` is one of :data:`CHECK_KINDS`.

>>> str(NotSupported("react", "ntfy"))
'ntfy does not support react'
>>> error = ChannelError("slow down", kind="rate_limited", retryable=True, retry_after=30)
>>> error.kind, error.retryable, error.retry_after
('rate_limited', True, 30.0)
>>> held = NeedsApproval("public audience")
>>> held.error_kind, held.reason
('needs_approval', 'public audience')
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

__all__ = [
    "CHECK_KINDS",
    "ERROR_KINDS",
    "BeforeSendFailed",
    "BeforeSendUnavailable",
    "ChannelError",
    "CorrespondError",
    "InvalidRef",
    "MissingRequirement",
    "NeedsApproval",
    "NotSupported",
    "Refused",
    "Stopped",
    "UnknownChannel",
]

#: Why a platform call failed, for a caller deciding whether to retry, fix the draft, or ask a human.
ERROR_KINDS = (
    "auth",  # the credential is missing, expired or refused
    "permission",  # authenticated, but not allowed to do this here
    "not_found",  # the conversation or message does not exist, or is not visible
    "rate_limited",  # slow down; `retry_after` says for how long when the platform said
    "network",  # the platform could not be reached
    "validation",  # the input was rejected (by the platform, or by correspond on its behalf)
    "unavailable",  # the platform failed, or something local (a binary, the OS) is missing
)

#: Why the ``before_send`` check stopped a write. Nothing was sent; a retry of the same draft changes nothing.
CHECK_KINDS = (
    "refused",  # the check blocks this draft on this conversation: change the text or the destination
    "needs_approval",  # the check holds the draft until the operator approves it
    "before_send_unavailable",  # the configured check could not be loaded, so no write goes ahead (fail closed)
    "before_send_failed",  # the check crashed or answered outside its contract, so the write did not go ahead
)


class CorrespondError(Exception):
    """An expected failure, with a message meant for the person or agent that asked."""


class InvalidRef(CorrespondError, ValueError):
    """A conversation reference that does not parse, or that its channel rejects."""


class UnknownChannel(CorrespondError, LookupError):
    """A channel name that no registered adapter answers to."""

    def __init__(self, channel: str, *, known: Iterable[str] = (), hint: str = ""):
        self.channel = channel
        self.known = tuple(sorted(known))
        message = f"unknown channel {channel!r}"
        if self.known:
            message += f"; available: {', '.join(self.known)}"
        if hint:
            message += f". {hint}"
        super().__init__(message)


class NotSupported(CorrespondError):
    """The channel does not have this operation (or this feature of it). Never a silent no-op."""

    def __init__(self, operation: str, channel: str, *, alternatives: Iterable[str] = ()):
        self.operation = operation
        self.channel = channel
        self.alternatives = tuple(alternatives)
        message = f"{channel} does not support {operation}"
        if self.alternatives:
            message += f"; instead: {'; '.join(self.alternatives)}"
        super().__init__(message)


class ChannelError(CorrespondError):
    """A platform call that failed, classified so the caller can decide what to do."""

    def __init__(
        self,
        message: str,
        *,
        kind: str,
        retryable: bool = False,
        retry_after: float | None = None,
    ):
        if kind not in ERROR_KINDS:
            raise ValueError(
                f"unknown error kind {kind!r}; expected one of {ERROR_KINDS}"
            )
        self.kind = kind
        self.retryable = retryable
        self.retry_after = None if retry_after is None else float(retry_after)
        super().__init__(message)


class Stopped(CorrespondError):
    """A write the ``before_send`` check did not let leave: ``error_kind`` says how, ``reason`` says why.

    Keyword ``details`` (an approval id, a draft hash) travel with the result, in the
    plan's ``before_send_details``.
    """

    error_kind: str = "before_send_failed"

    def __init__(self, reason: str, **details: Any):
        self.reason = str(reason)
        self.details = dict(details)
        super().__init__(self.reason)


class Refused(Stopped):
    """Block: this draft does not go to this conversation as written. Raised by a ``before_send`` check."""

    error_kind = "refused"


class NeedsApproval(Stopped):
    """Draft to operator: the write waits for the operator's approval. Raised by a ``before_send`` check."""

    error_kind = "needs_approval"


class BeforeSendUnavailable(Stopped):
    """The configured ``before_send`` check could not be loaded, so nothing is sent."""

    error_kind = "before_send_unavailable"


class BeforeSendFailed(Stopped):
    """The ``before_send`` check raised something else, or returned a value, so nothing is sent."""

    error_kind = "before_send_failed"


class MissingRequirement(ChannelError):
    """Something the channel needs is not here: a credential, a binary, an extra, an OS."""

    def __init__(
        self, channel: str, missing: str, *, fix: str, kind: str = "unavailable"
    ):
        self.channel = channel
        self.missing = missing
        self.fix = fix
        super().__init__(f"{channel}: {missing}. {fix}", kind=kind)
