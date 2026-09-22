"""Idempotent sends: an ``idempotency_key`` keeps a message from going out twice.

A channel can post a message and then fail: the platform accepted the post and the
connection dropped before its answer, or a call after the post raised. :func:`~correspond.send`
then reports ``ok=False``, and whoever sends it again (a retry, an operator releasing the
draft by hand) posts it a second time. With ``idempotency_key=`` :func:`~correspond.send`
remembers, in a store of its own (``sends=``, by default files under the data root), what
each key did:

- right before the real write the key is claimed (:data:`ATTEMPTED`), and after it the key
  is :data:`SENT`, with the result; a failure the platform reported before accepting
  anything (:data:`NOTHING_POSTED_KINDS`) marks it :data:`FAILED`, which a new try may
  claim again;
- the same key after :data:`SENT` posts nothing and answers the stored result, marked a
  replay;
- the same key after an attempt whose outcome is unknown (still :data:`ATTEMPTED`: a
  network or platform failure, a crash) reads the conversation back, where the channel can
  be read, for a message by correspond's own account with the same text since the attempt.
  Found, that message is the result, and nothing is posted. Not found, or not readable, the
  send is not made: it fails with ``error_kind="unconfirmed"``, saying to check the
  conversation, since a reader may not show a message that did go out (an email's inbox
  does not list what was sent, and Telegram's posted text starts with the title). The
  caller who has checked sends with a new key;
- a second send with a key another one claimed meanwhile (a retry that overlapped the
  first) writes nothing: the default store claims a key with an exclusive create, so of
  two processes exactly one sends;
- a key used again for another conversation or another draft is refused (``validation``).

A dry run reads the store and writes nothing. Nothing is added to the text posted.

>>> record = claimed("k1", "fake:example/demo", fingerprint="f", at=parse_time("2026-09-22T12:00:00Z"))
>>> record["state"], record["attempted_at"]
('attempted', '2026-09-22T12:00:00+00:00')
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any

from correspond.model import Draft, Message, parse_time

__all__ = [
    "ATTEMPTED",
    "FAILED",
    "MAX_KEY_LENGTH",
    "NOTHING_POSTED_KINDS",
    "READ_BACK_SKEW",
    "SENT",
    "STATES",
    "check_key",
    "attempted_at",
    "claimed",
    "fingerprint",
    "matching_message",
    "now",
    "settled",
]

#: The key was claimed for a real write whose outcome is not known yet (or never became known).
ATTEMPTED = "attempted"
#: The write went out; the record keeps its result.
SENT = "sent"
#: The platform refused the write before accepting anything; the key may be claimed again.
FAILED = "failed"
STATES = (ATTEMPTED, SENT, FAILED)

#: The ``error_kind`` of failures that mean the platform accepted nothing, so a new try is safe.
#: A network failure and an unavailable platform are not among them: the post may have landed.
#: ``validation`` is not among them either: a platform can report an error with no type
#: after it committed the write (GitHub's GraphQL mutations do on a timeout).
NOTHING_POSTED_KINDS = frozenset({"auth", "permission", "not_found", "rate_limited"})

#: How far before an attempt a message read back may be dated and still be that attempt's:
#: the platform's clock and this machine's differ.
READ_BACK_SKEW = timedelta(minutes=5)

#: The longest key accepted (it names a file in the default store).
MAX_KEY_LENGTH = 128


def check_key(key: Any) -> str:
    """``key`` stripped, or ``ValueError`` for one that is not a non-blank string of at most :data:`MAX_KEY_LENGTH` characters.

    >>> check_key(" case-12/reply-3 ")
    'case-12/reply-3'
    """
    if not isinstance(key, str) or not key.strip():
        raise ValueError(f"an idempotency key is a non-blank string, not {key!r}")
    key = key.strip()
    if len(key) > MAX_KEY_LENGTH or not key.isprintable():
        raise ValueError(
            f"an idempotency key is at most {MAX_KEY_LENGTH} printable characters; "
            f"this one has {len(key)}"
        )
    return key


def fingerprint(conversation: str, draft: Draft) -> str:
    """What a key is bound to: the conversation and everything the draft says, as SHA-256 hex."""
    data = {"conversation": conversation, "draft": draft.to_dict()}
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode("utf-8")).hexdigest()


def claimed(
    key: str, conversation: str, *, fingerprint: str, at: datetime
) -> dict[str, Any]:
    """The record of ``key`` claimed at ``at`` for a real write to ``conversation``."""
    return {
        "key": key,
        "conversation": conversation,
        "fingerprint": fingerprint,
        "state": ATTEMPTED,
        "attempted_at": at.isoformat(),
        "result": None,
    }


def settled(
    record: Mapping[str, Any], state: str, result: Mapping[str, Any] | None
) -> dict[str, Any]:
    """``record`` moved to ``state`` (:data:`SENT` or :data:`FAILED`), with the write's ``result``."""
    if state not in STATES:
        raise ValueError(f"state must be one of {STATES}, not {state!r}")
    return {**record, "state": state, "result": dict(result) if result else None}


def matching_message(
    messages: Iterable[Message], draft: Draft, *, since: datetime
) -> Message | None:
    """The latest of ``messages`` written by correspond's own account with ``draft``'s text, at or after ``since`` less :data:`READ_BACK_SKEW`."""
    earliest = since - READ_BACK_SKEW
    text = draft.text.strip()
    found = [
        m
        for m in messages
        if m.author.is_self and m.sent_at >= earliest and (m.text or "").strip() == text
    ]
    return max(found, key=lambda m: m.sent_at) if found else None


def now() -> datetime:
    """The moment a key is claimed (UTC)."""
    return datetime.now(timezone.utc)


def attempted_at(record: Mapping[str, Any]) -> datetime:
    """When ``record``'s key was claimed."""
    return parse_time(record["attempted_at"])
