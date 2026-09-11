"""The data model every channel is described in.

Accounts, conversation references, channel identities, authenticity, messages,
attachments, events, capabilities, drafts and send results: frozen dataclasses that
round-trip through JSON-ready dicts (``to_dict`` / ``from_dict``), because every surface
(the CLI, MCP, a ledger on disk) moves them as JSON.

There is no person here. A :class:`ChannelIdentity` is who the platform says sent a
message and :class:`Authenticity` is how sure the channel is. Linking an identity to a
person is a job for a people registry, with its own evidence.

>>> ref = ConversationRef.parse("github:octocat/hello-world#1")
>>> ref.channel, ref.id, str(ref)
('github', 'octocat/hello-world#1', 'github:octocat/hello-world#1')
>>> ConversationRef.from_dict(ref.to_dict()) == ref
True
>>> Grade("bound") in {Grade.PLATFORM, Grade.BOUND, Grade.CRYPTO}
True
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from correspond.errors import ERROR_KINDS, ChannelError, CorrespondError, InvalidRef

__all__ = [
    "ACTS_AS",
    "EVENT_KINDS",
    "OPERATIONS",
    "PRIORITIES",
    "Account",
    "Attachment",
    "Authenticity",
    "Capabilities",
    "ChannelIdentity",
    "ConversationRef",
    "Draft",
    "Event",
    "Grade",
    "HistoryDepth",
    "Message",
    "SendResult",
    "Support",
    "format_time",
    "parse_time",
]

#: The seven operations, in the order capabilities and surfaces list them.
OPERATIONS = ("read", "listen", "send", "edit", "react", "upload", "verify")
#: Whose name a write goes out under.
ACTS_AS = ("bot", "user", "app", "service")
#: What an :class:`Event` reports.
EVENT_KINDS = ("message.created", "message.updated", "reaction.added")
#: The priorities a draft may ask for; channels without priorities refuse anything but ``None``.
PRIORITIES = ("low", "normal", "high", "urgent")

CHANNEL_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_UNSAFE_ID_RE = re.compile(r"[\s\x00-\x1f\x7f]")


# ----------------------------------------------------------------------------- time


def format_time(value: datetime | None) -> str | None:
    """ISO 8601 in UTC with a ``Z`` suffix; a naive datetime is taken to be UTC.

    >>> format_time(datetime(2026, 9, 11, 12, 30, tzinfo=timezone.utc))
    '2026-09-11T12:30:00Z'
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_time(value: str | datetime | None) -> datetime | None:
    """The inverse of :func:`format_time`; also accepts offsets and datetimes. Naive means UTC.

    >>> parse_time("2026-09-11T12:30:00Z") == datetime(2026, 9, 11, 12, 30, tzinfo=timezone.utc)
    True
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        moment = value
    else:
        try:
            moment = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError:
            raise CorrespondError(
                f"{value!r} is not a time; use ISO 8601, e.g. 2026-09-11T12:30:00Z"
            ) from None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------- vocabularies


class Grade(StrEnum):
    """How sure the channel is about who sent a message: a vocabulary, not a ranking.

    - ``forged``: verification was attempted and failed.
    - ``claimed``: nothing verifiable (a typed name, an unauthenticated From header, anonymous input).
    - ``platform``: the platform authenticated the account (GitHub, Telegram).
    - ``domain``: email authenticated by *your own* receiving server's Authentication-Results.
    - ``bound``: a host application signed an assertion about its logged-in user.
    - ``crypto``: a signed payload (a GitHub webhook's ``X-Hub-Signature-256``).

    A policy names the grades it accepts for a permission. Comparing grades with ``<``
    raises, because ``domain`` is neither stronger nor weaker than ``platform``.

    >>> Grade.CLAIMED < Grade.PLATFORM
    Traceback (most recent call last):
    ...
    TypeError: authenticity grades are not ranked; test membership in the grades a policy accepts
    """

    FORGED = "forged"
    CLAIMED = "claimed"
    PLATFORM = "platform"
    DOMAIN = "domain"
    BOUND = "bound"
    CRYPTO = "crypto"

    def _not_ranked(self, other):
        raise TypeError(
            "authenticity grades are not ranked; test membership in the grades a policy accepts"
        )

    __lt__ = __le__ = __gt__ = __ge__ = _not_ranked


class Support(StrEnum):
    """How well a channel does something: ``full``, ``partial`` (with limits in ``notes``), or ``none``."""

    FULL = "full"
    PARTIAL = "partial"
    NONE = "none"


class HistoryDepth(StrEnum):
    """How far back ``read`` sees: all history, a 24-hour buffer, only what correspond has seen since it was linked or started listening, or nothing."""

    FULL = "full"
    BUFFER_24H = "buffer_24h"
    SINCE_LINK = "since_link"
    NONE = "none"


# ------------------------------------------------------------------------ references


@dataclass(frozen=True, kw_only=True)
class Account:
    """The credentialed endpoint correspond acts through: whose name a write goes out under."""

    channel: str
    id: str
    acts_as: str = "user"

    def __post_init__(self):
        if self.acts_as not in ACTS_AS:
            raise ValueError(f"acts_as must be one of {ACTS_AS}, not {self.acts_as!r}")

    def to_dict(self) -> dict:
        """JSON-ready."""
        return {"channel": self.channel, "id": self.id, "acts_as": self.acts_as}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Account:
        """The inverse of :meth:`to_dict`."""
        return cls(
            channel=data["channel"], id=data["id"], acts_as=data.get("acts_as", "user")
        )


@dataclass(frozen=True, kw_only=True)
class ConversationRef:
    """Where a conversation lives: a channel and that channel's own id, encoded ``<channel>:<id>``.

    Two references are equal when channel and id are. ``kind`` (``issue``, ``discussion``,
    ``chat``, ``address``, …) and ``parent`` describe the conversation and are filled in by
    the channel's adapter, so a freshly parsed reference equals its normalised form.

    >>> ConversationRef.parse("telegram:-4001/7") == ConversationRef(channel="telegram", id="-4001/7", kind="topic")
    True
    >>> ConversationRef.parse("nonsense")
    Traceback (most recent call last):
    ...
    correspond.errors.InvalidRef: 'nonsense' is not a conversation reference: the form is <channel>:<id>, e.g. github:octocat/hello-world#1
    """

    channel: str
    id: str = ""
    kind: str = field(default="", compare=False)
    parent: ConversationRef | None = field(default=None, compare=False)

    def __post_init__(self):
        if not isinstance(self.channel, str) or not CHANNEL_NAME_RE.match(self.channel):
            raise InvalidRef(
                f"channel name {self.channel!r} must be lowercase letters, digits, '-' or '_', starting with a letter"
            )
        if not isinstance(self.id, str) or _UNSAFE_ID_RE.search(self.id):
            raise InvalidRef(
                f"conversation id {self.id!r} must not contain whitespace or control characters"
            )

    @property
    def encoded(self) -> str:
        """The stable string form, ``<channel>:<id>``."""
        return f"{self.channel}:{self.id}"

    def __str__(self) -> str:
        return self.encoded

    @classmethod
    def parse(cls, text: str | ConversationRef) -> ConversationRef:
        """Split ``<channel>:<id>`` without consulting the channel (its adapter normalises the id)."""
        if isinstance(text, ConversationRef):
            return text
        if not isinstance(text, str):
            raise InvalidRef(
                f"a conversation reference is a string, not {type(text).__name__}"
            )
        channel, separator, native_id = text.strip().partition(":")
        if not separator:
            raise InvalidRef(
                f"{text!r} is not a conversation reference: the form is <channel>:<id>, e.g. github:octocat/hello-world#1"
            )
        return cls(channel=channel, id=native_id)

    def to_dict(self) -> dict:
        """JSON-ready, with the encoded form under ``ref``."""
        return {
            "ref": self.encoded,
            "channel": self.channel,
            "id": self.id,
            "kind": self.kind,
            "parent": self.parent.to_dict() if self.parent else None,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | str) -> ConversationRef:
        """The inverse of :meth:`to_dict`; a bare encoded string also works."""
        if isinstance(data, str):
            return cls.parse(data)
        if "channel" not in data:
            return cls.parse(data["ref"])
        parent = data.get("parent")
        return cls(
            channel=data["channel"],
            id=data.get("id", ""),
            kind=data.get("kind", ""),
            parent=cls.from_dict(parent) if parent else None,
        )


@dataclass(frozen=True, kw_only=True)
class ChannelIdentity:
    """Who a channel says sent something: its native id and what the platform attests about it. Not a person."""

    channel: str
    native_id: str
    handle: str | None = None
    display_name: str | None = None
    is_bot: bool = False
    is_self: bool = False
    authority: str | None = None

    @property
    def address(self) -> str:
        """``<channel>:<handle>``, or the native id when there is no handle: the form a people registry resolves.

        >>> ChannelIdentity(channel="github", native_id="583231", handle="octocat").address
        'github:octocat'
        """
        return f"{self.channel}:{self.handle or self.native_id}"

    def label(self) -> str:
        """A short name for transcripts."""
        return self.handle or self.display_name or self.native_id or "anonymous"

    def to_dict(self) -> dict:
        """JSON-ready."""
        return {
            "channel": self.channel,
            "native_id": self.native_id,
            "handle": self.handle,
            "display_name": self.display_name,
            "is_bot": self.is_bot,
            "is_self": self.is_self,
            "authority": self.authority,
            "address": self.address,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ChannelIdentity:
        """The inverse of :meth:`to_dict`."""
        return cls(
            channel=data["channel"],
            native_id=data.get("native_id", ""),
            handle=data.get("handle"),
            display_name=data.get("display_name"),
            is_bot=bool(data.get("is_bot", False)),
            is_self=bool(data.get("is_self", False)),
            authority=data.get("authority"),
        )


@dataclass(frozen=True, kw_only=True)
class Authenticity:
    """A grade and its evidence, computed on correspond's side of the hop and never read from a payload.

    >>> Authenticity(grade="bound", evidence={"method": "hmac-sha256"}).to_dict()
    {'grade': 'bound', 'evidence': {'method': 'hmac-sha256'}}
    """

    grade: Grade
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "grade", Grade(self.grade))
        object.__setattr__(self, "evidence", dict(self.evidence))

    def to_dict(self) -> dict:
        """JSON-ready."""
        return {"grade": self.grade.value, "evidence": dict(self.evidence)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Authenticity:
        """The inverse of :meth:`to_dict`."""
        return cls(grade=data["grade"], evidence=data.get("evidence") or {})


# -------------------------------------------------------------------------- messages


@dataclass(frozen=True, kw_only=True)
class Attachment:
    """A file that came with a message: referenced by ``ref``, fetched only on :meth:`content`, never inlined."""

    ref: str
    media_type: str = "application/octet-stream"
    name: str | None = None
    size: int | None = None
    sha256: str | None = None
    loader: Callable[[], bytes] | None = field(default=None, compare=False, repr=False)

    def content(self) -> bytes:
        """The bytes, fetched now through the channel that produced the attachment."""
        if self.loader is None:
            raise CorrespondError(
                f"attachment {self.ref} did not come through a channel in this process, so it cannot be fetched; read the message again"
            )
        return self.loader()

    def to_dict(self) -> dict:
        """JSON-ready (without the loader)."""
        return {
            "ref": self.ref,
            "media_type": self.media_type,
            "name": self.name,
            "size": self.size,
            "sha256": self.sha256,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Attachment:
        """The inverse of :meth:`to_dict` (the result has no loader)."""
        return cls(
            ref=data["ref"],
            media_type=data.get("media_type") or "application/octet-stream",
            name=data.get("name"),
            size=data.get("size"),
            sha256=data.get("sha256"),
        )


@dataclass(frozen=True, kw_only=True)
class Message:
    """One message in a conversation, normalised, with the channel's own fields kept in ``native``."""

    id: str
    conversation: ConversationRef
    author: ChannelIdentity
    authenticity: Authenticity
    sent_at: datetime
    text: str
    body: str | None = None
    body_format: str = "plain"
    attachments: tuple[Attachment, ...] = ()
    reply_to: str | None = None
    thread_root: str | None = None
    edited_at: datetime | None = None
    url: str | None = None
    native: Mapping[str, Any] = field(default_factory=dict)
    raw: Any = field(default=None, compare=False, repr=False)

    def __post_init__(self):
        object.__setattr__(self, "attachments", tuple(self.attachments))
        object.__setattr__(self, "native", dict(self.native))

    def to_dict(self, *, include_raw: bool = False) -> dict:
        """JSON-ready; ``raw`` (the untouched payload) only when asked for."""
        data = {
            "id": self.id,
            "conversation": self.conversation.to_dict(),
            "author": self.author.to_dict(),
            "authenticity": self.authenticity.to_dict(),
            "sent_at": format_time(self.sent_at),
            "text": self.text,
            "body": self.body,
            "body_format": self.body_format,
            "attachments": [a.to_dict() for a in self.attachments],
            "reply_to": self.reply_to,
            "thread_root": self.thread_root,
            "edited_at": format_time(self.edited_at),
            "url": self.url,
            "native": dict(self.native),
        }
        if include_raw:
            data["raw"] = self.raw
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Message:
        """The inverse of :meth:`to_dict`."""
        return cls(
            id=data["id"],
            conversation=ConversationRef.from_dict(data["conversation"]),
            author=ChannelIdentity.from_dict(data["author"]),
            authenticity=Authenticity.from_dict(data["authenticity"]),
            sent_at=parse_time(data["sent_at"]),
            text=data.get("text", ""),
            body=data.get("body"),
            body_format=data.get("body_format", "plain"),
            attachments=tuple(
                Attachment.from_dict(a) for a in data.get("attachments") or ()
            ),
            reply_to=data.get("reply_to"),
            thread_root=data.get("thread_root"),
            edited_at=parse_time(data.get("edited_at")),
            url=data.get("url"),
            native=data.get("native") or {},
            raw=data.get("raw"),
        )


@dataclass(frozen=True, kw_only=True)
class Event:
    """Something that happened on a channel, as a listener reports it: dedupe on ``delivery_id``, resume from ``cursor``."""

    kind: str
    channel: str
    delivery_id: str
    cursor: str | None = None
    message: Message | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.kind not in EVENT_KINDS:
            raise ValueError(
                f"event kind must be one of {EVENT_KINDS}, not {self.kind!r}"
            )
        object.__setattr__(self, "payload", dict(self.payload))

    def to_dict(self) -> dict:
        """JSON-ready."""
        return {
            "kind": self.kind,
            "channel": self.channel,
            "delivery_id": self.delivery_id,
            "cursor": self.cursor,
            "message": self.message.to_dict() if self.message else None,
            "payload": dict(self.payload),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Event:
        """The inverse of :meth:`to_dict`."""
        message = data.get("message")
        return cls(
            kind=data["kind"],
            channel=data["channel"],
            delivery_id=data["delivery_id"],
            cursor=data.get("cursor"),
            message=Message.from_dict(message) if message else None,
            payload=data.get("payload") or {},
        )


# ---------------------------------------------------------------------- capabilities


_SUPPORT_FIELDS = (*OPERATIONS, "initiate", "reply", "priority")
_TUPLE_FIELDS = (
    "listen_modes",
    "reactions",
    "formats",
    "rate_limits",
    "notes",
)


@dataclass(frozen=True, kw_only=True)
class Capabilities:
    """What a channel can do, graded, with its limits.

    One ``Support`` per operation in :data:`OPERATIONS`, plus three features of writing:
    ``initiate`` (can a write start a conversation; Telegram bots cannot), ``reply``
    (can a draft answer a specific message) and ``priority``. ``history_depth`` says how
    far back ``read`` sees; ``grades`` are the authenticity grades the channel can attest;
    ``native_fields`` are the keys its messages may carry in ``native`` (what a routing
    condition can test), or ``None`` when the channel does not declare them.
    """

    channel: str
    read: Support = Support.NONE
    listen: Support = Support.NONE
    send: Support = Support.NONE
    edit: Support = Support.NONE
    react: Support = Support.NONE
    upload: Support = Support.NONE
    verify: Support = Support.NONE
    initiate: Support = Support.NONE
    reply: Support = Support.NONE
    priority: Support = Support.NONE
    history_depth: HistoryDepth = HistoryDepth.NONE
    listen_modes: tuple[str, ...] = ()
    grades: tuple[Grade, ...] = ()
    max_text_length: int | None = None
    max_title_length: int | None = None
    edit_max_age_s: int | None = None
    reactions: tuple[str, ...] = ()
    reactions_per_message: int | None = None
    max_upload_bytes: int | None = None
    formats: tuple[str, ...] = ("plain",)
    native_fields: tuple[str, ...] | None = None
    rate_limits: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def __post_init__(self):
        for name in _SUPPORT_FIELDS:
            object.__setattr__(self, name, Support(getattr(self, name)))
        object.__setattr__(self, "history_depth", HistoryDepth(self.history_depth))
        object.__setattr__(self, "grades", tuple(Grade(g) for g in self.grades))
        for name in _TUPLE_FIELDS:
            object.__setattr__(self, name, tuple(getattr(self, name)))
        if self.native_fields is not None:
            object.__setattr__(self, "native_fields", tuple(self.native_fields))

    def supports(self, operation: str) -> Support:
        """The support level for one of :data:`OPERATIONS`."""
        if operation not in OPERATIONS:
            raise ValueError(
                f"unknown operation {operation!r}; expected one of {OPERATIONS}"
            )
        return getattr(self, operation)

    @property
    def operations(self) -> tuple[str, ...]:
        """The operations the channel has at all (full or partial)."""
        return tuple(op for op in OPERATIONS if getattr(self, op) is not Support.NONE)

    def to_dict(self) -> dict:
        """JSON-ready."""
        data: dict[str, Any] = {"channel": self.channel}
        data.update({name: getattr(self, name).value for name in _SUPPORT_FIELDS})
        data["history_depth"] = self.history_depth.value
        data["grades"] = [g.value for g in self.grades]
        for name in (
            "max_text_length",
            "max_title_length",
            "edit_max_age_s",
            "reactions_per_message",
            "max_upload_bytes",
        ):
            data[name] = getattr(self, name)
        data.update({name: list(getattr(self, name)) for name in _TUPLE_FIELDS})
        data["native_fields"] = (
            None if self.native_fields is None else list(self.native_fields)
        )
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Capabilities:
        """The inverse of :meth:`to_dict`."""
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


# ------------------------------------------------------------------------- writing


@dataclass(frozen=True, kw_only=True)
class Draft:
    """What to write: the text, and the few things channels share (a title, the message answered, a priority)."""

    text: str
    title: str | None = None
    reply_to: str | None = None
    priority: str | None = None

    def __post_init__(self):
        if not isinstance(self.text, str):
            raise TypeError(
                f"draft text must be a string, not {type(self.text).__name__}"
            )
        if self.priority is not None and self.priority not in PRIORITIES:
            raise ValueError(
                f"priority must be one of {PRIORITIES}, not {self.priority!r}"
            )

    def to_dict(self) -> dict:
        """JSON-ready."""
        return {
            "text": self.text,
            "title": self.title,
            "reply_to": self.reply_to,
            "priority": self.priority,
        }


@dataclass(frozen=True, kw_only=True)
class SendResult:
    """What a write did, or would do (``dry_run``), or why it failed and whether a retry could help."""

    ok: bool
    channel: str
    conversation: str
    operation: str = "send"
    dry_run: bool = False
    message_id: str | None = None
    url: str | None = None
    account: Account | None = None
    plan: Mapping[str, Any] = field(default_factory=dict)
    error: str | None = None
    error_kind: str | None = None
    retryable: bool = False
    retry_after: float | None = None

    def __post_init__(self):
        if self.error_kind is not None and self.error_kind not in ERROR_KINDS:
            raise ValueError(
                f"error_kind must be one of {ERROR_KINDS}, not {self.error_kind!r}"
            )
        object.__setattr__(self, "plan", dict(self.plan))

    @classmethod
    def failure(
        cls,
        error: ChannelError,
        *,
        channel: str,
        conversation: str,
        operation: str = "send",
        dry_run: bool = False,
        plan: Mapping[str, Any] | None = None,
    ) -> SendResult:
        """A result carrying a :class:`~correspond.errors.ChannelError`'s classification."""
        return cls(
            ok=False,
            channel=channel,
            conversation=conversation,
            operation=operation,
            dry_run=dry_run,
            plan=plan or {},
            error=str(error),
            error_kind=error.kind,
            retryable=error.retryable,
            retry_after=error.retry_after,
        )

    def to_dict(self) -> dict:
        """JSON-ready."""
        return {
            "ok": self.ok,
            "channel": self.channel,
            "conversation": self.conversation,
            "operation": self.operation,
            "dry_run": self.dry_run,
            "message_id": self.message_id,
            "url": self.url,
            "account": self.account.to_dict() if self.account else None,
            "plan": dict(self.plan),
            "error": self.error,
            "error_kind": self.error_kind,
            "retryable": self.retryable,
            "retry_after": self.retry_after,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SendResult:
        """The inverse of :meth:`to_dict`."""
        account = data.get("account")
        known = set(cls.__dataclass_fields__)
        fields = {k: v for k, v in data.items() if k in known and k != "account"}
        return cls(**fields, account=Account.from_dict(account) if account else None)
