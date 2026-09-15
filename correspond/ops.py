"""The operations: small protocols an adapter implements a subset of, and the verbs that call them.

An adapter is any object with a ``name``, ``capabilities`` and ``parse_ref(id)``, plus the
methods of whichever protocols it can honour:

==============  =====================================================================
Reader          ``read(ref, *, since=None, limit=None) -> Iterable[Message]``
Listener        ``poll(ref, *, cursor=None, limit=None) -> Iterable[Event]``
Writer          ``send(ref, draft, *, dry_run=False) -> SendResult``
Editor          ``edit(ref, message_id, draft, *, dry_run=False) -> SendResult``
Reactor         ``react(ref, message_id, reaction, *, dry_run=False) -> SendResult``
Uploader        ``upload(ref, name, data, *, media_type, dry_run=False) -> SendResult``
Verifier        ``verify(headers, body) -> Authenticity``
AudienceReader  ``audience(ref, *, draft=None) -> Audience``
==============  =====================================================================

The verbs here (:func:`read`, :func:`listen`, :func:`send`, …) take a reference string,
find the adapter in the registry, and raise :class:`~correspond.errors.NotSupported`
naming the operation when the adapter lacks it; never a silent no-op. Writes check the
draft against the channel's capabilities first, and turn a
:class:`~correspond.errors.ChannelError` into a ``SendResult`` with ``ok=False``, so a
failed notification never crashes its caller. ``dry_run=True`` contacts nothing.
:func:`audience` is the exception to refusing: it never raises, because an audience
nobody can compute is public.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, MutableMapping
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from correspond.errors import ChannelError, NotSupported, UnknownChannel
from correspond.model import (
    Audience,
    Authenticity,
    Capabilities,
    ConversationRef,
    Draft,
    Event,
    Message,
    SendResult,
    Support,
    parse_time,
)

__all__ = [
    "PROTOCOLS",
    "AudienceReader",
    "Channel",
    "Editor",
    "Listener",
    "Reactor",
    "Reader",
    "Uploader",
    "Verifier",
    "Writer",
    "audience",
    "capabilities",
    "edit",
    "get_channel",
    "implemented",
    "listen",
    "parse_ref",
    "react",
    "read",
    "send",
    "upload",
    "verify",
    "window",
    "with_final_cursor",
]


@runtime_checkable
class Channel(Protocol):
    """What every adapter has: a name, its capabilities, and the grammar of its conversation ids."""

    name: str

    @property
    def capabilities(self) -> Capabilities: ...

    def parse_ref(self, id: str) -> ConversationRef: ...


@runtime_checkable
class Reader(Protocol):
    """Messages of a conversation, oldest first; ``limit`` keeps the most recent."""

    def read(
        self,
        ref: ConversationRef,
        *,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> Iterable[Message]: ...


@runtime_checkable
class Listener(Protocol):
    """Events since ``cursor``, oldest first, each carrying the cursor to resume after it."""

    def poll(
        self, ref: ConversationRef, *, cursor: str | None = None, limit: int | None = None
    ) -> Iterable[Event]: ...


@runtime_checkable
class Writer(Protocol):
    """Send a draft to a conversation."""

    def send(
        self, ref: ConversationRef, draft: Draft, *, dry_run: bool = False
    ) -> SendResult: ...


@runtime_checkable
class Editor(Protocol):
    """Replace the text of a message correspond's account wrote."""

    def edit(
        self,
        ref: ConversationRef,
        message_id: str,
        draft: Draft,
        *,
        dry_run: bool = False,
    ) -> SendResult: ...


@runtime_checkable
class Reactor(Protocol):
    """Add a reaction to a message."""

    def react(
        self,
        ref: ConversationRef,
        message_id: str,
        reaction: str,
        *,
        dry_run: bool = False,
    ) -> SendResult: ...


@runtime_checkable
class Uploader(Protocol):
    """Send a file to a conversation."""

    def upload(
        self,
        ref: ConversationRef,
        name: str,
        data: bytes,
        *,
        media_type: str = "application/octet-stream",
        dry_run: bool = False,
    ) -> SendResult: ...


@runtime_checkable
class Verifier(Protocol):
    """Grade an inbound delivery (a webhook, a posted report) from its headers and raw body."""

    def verify(self, headers: Mapping[str, str], body: bytes) -> Authenticity: ...


@runtime_checkable
class AudienceReader(Protocol):
    """Who can read a conversation, now and plausibly later, asked of the platform at the time of the call."""

    def audience(
        self, ref: ConversationRef, *, draft: Draft | None = None
    ) -> Audience: ...


#: Operation name → the protocol an adapter implements to have it.
PROTOCOLS: dict[str, type] = {
    "read": Reader,
    "listen": Listener,
    "send": Writer,
    "edit": Editor,
    "react": Reactor,
    "upload": Uploader,
    "verify": Verifier,
    "audience": AudienceReader,
}


def implemented(adapter: Any) -> tuple[str, ...]:
    """The operations an adapter implements, in :data:`~correspond.model.OPERATIONS` order."""
    return tuple(
        op for op, protocol in PROTOCOLS.items() if isinstance(adapter, protocol)
    )


# ------------------------------------------------------------------ adapter helpers


def window(
    messages: Iterable[Message],
    *,
    since: datetime | None = None,
    limit: int | None = None,
) -> list[Message]:
    """Messages sent or edited at or after ``since``, oldest first, keeping the last ``limit``."""
    kept = [
        m
        for m in messages
        if since is None
        or m.sent_at >= since
        or (m.edited_at is not None and m.edited_at >= since)
    ]
    kept.sort(key=lambda m: m.sent_at)
    return kept[-limit:] if limit and limit > 0 else kept


def with_final_cursor(
    events: Iterable[Event], final_cursor: str | None
) -> Iterator[Event]:
    """Yield ``events``, then hand ``final_cursor`` to :func:`listen`, so a quiet poll still moves the cursor forward."""
    yield from events
    return final_cursor


# -------------------------------------------------------------------------- lookup


def _registry(registry: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if registry is not None:
        return registry
    from correspond.registry import channels

    return channels()


def get_channel(name: str, *, registry: Mapping[str, Any] | None = None) -> Any:
    """The adapter registered under ``name``; :class:`~correspond.errors.UnknownChannel` says what to do if there is none."""
    registry = _registry(registry)
    try:
        return registry[name]
    except KeyError:
        from correspond.registry import CHANNELS

        built_in = {c.name for c in CHANNELS}
        hint = (
            f"run `correspond requirements {name}` to see what it needs"
            if name in built_in
            else ""
        )
        raise UnknownChannel(name, known=list(registry), hint=hint) from None


def parse_ref(
    ref: str | ConversationRef, *, registry: Mapping[str, Any] | None = None
) -> ConversationRef:
    """A reference normalised by its channel's adapter (kind and parent filled in, the id validated)."""
    generic = ConversationRef.parse(ref)
    return get_channel(generic.channel, registry=registry).parse_ref(generic.id)


def capabilities(
    channel: str, *, registry: Mapping[str, Any] | None = None
) -> Capabilities:
    """What a channel can do, graded, with its limits."""
    return get_channel(channel, registry=registry).capabilities


_ALTERNATIVES = {
    "edit": ("send", "send a follow-up message"),
    "react": ("send", "reply with a message"),
    "read": ("listen", "listen, which returns new messages"),
    "send": ("read", "reply on another channel the sender gave"),
}


def _adapter_for(
    ref: str | ConversationRef, operation: str, registry: Mapping[str, Any] | None
) -> tuple[Any, ConversationRef]:
    generic = ConversationRef.parse(ref)
    adapter = get_channel(generic.channel, registry=registry)
    if not isinstance(adapter, PROTOCOLS[operation]):
        needs, hint = _ALTERNATIVES.get(operation, (None, None))
        alternatives = (hint,) if needs and isinstance(adapter, PROTOCOLS[needs]) else ()
        raise NotSupported(operation, adapter.name, alternatives=alternatives)
    return adapter, adapter.parse_ref(generic.id)


# ---------------------------------------------------------------------------- reads


def read(
    ref: str | ConversationRef,
    *,
    since: str | datetime | None = None,
    limit: int | None = None,
    registry: Mapping[str, Any] | None = None,
) -> list[Message]:
    """The messages of a conversation, oldest first (``limit`` keeps the most recent ones)."""
    adapter, ref = _adapter_for(ref, "read", registry)
    return list(adapter.read(ref, since=parse_time(since), limit=limit))


def _committing(
    events: Iterable[Event], cursors: MutableMapping[str, str], key: str, commit: bool
) -> Iterator[Event]:
    iterator = iter(events)
    while True:
        try:
            event = next(iterator)
        except StopIteration as done:
            # A poll may end by returning where to resume (see `with_final_cursor`).
            if commit and done.value is not None:
                cursors[key] = done.value
            return
        yield event
        # Reached only when the consumer asks for the next event: delivery is at-least-once.
        if commit and event.cursor is not None:
            cursors[key] = event.cursor


def listen(
    ref: str | ConversationRef,
    *,
    cursors: MutableMapping[str, str] | None = None,
    limit: int | None = None,
    commit: bool = True,
    registry: Mapping[str, Any] | None = None,
) -> Iterator[Event]:
    """Events since the cursor stored for ``ref``; each cursor is stored once the consumer moves past its event.

    ``cursors`` defaults to files under the data root. Events can repeat after a crash or an
    early ``break``: deduplicate on ``delivery_id``.
    """
    adapter, ref = _adapter_for(ref, "listen", registry)
    if cursors is None:
        from correspond.stores import cursor_store

        cursors = cursor_store()
    key = ref.encoded
    return _committing(
        adapter.poll(ref, cursor=cursors.get(key), limit=limit), cursors, key, commit
    )


def verify(
    channel: str,
    headers: Mapping[str, str],
    body: bytes,
    *,
    registry: Mapping[str, Any] | None = None,
) -> Authenticity:
    """Grade an inbound delivery on ``channel`` from its headers and raw body."""
    adapter = get_channel(channel, registry=registry)
    if not isinstance(adapter, Verifier):
        raise NotSupported("verify", adapter.name)
    return adapter.verify(headers, body)


def audience(
    ref: str | ConversationRef,
    draft: Draft | None = None,
    *,
    registry: Mapping[str, Any] | None = None,
) -> Audience:
    """Who can read a conversation, asked of its channel now. Never raises: unknown resolves to public.

    A malformed reference, an unknown or planned channel, an adapter without an audience
    reader, and any error while computing all give ``scope="public"``, ``complete=False``,
    ``defaulted=True``, with the reason in ``evidence``. Nothing is cached: call it again
    right before sending.
    """
    label = ref.encoded if isinstance(ref, ConversationRef) else str(ref).strip()
    try:
        generic = ConversationRef.parse(ref)
        try:
            adapter = get_channel(generic.channel, registry=registry)
        except UnknownChannel as error:
            from correspond.registry import CHANNELS

            planned = {c.name: c.planned for c in CHANNELS if c.planned}
            if generic.channel in planned:
                return Audience.unknown(
                    label,
                    f"{generic.channel} is planned, not built ({planned[generic.channel]}), so nothing can tell who reads it",
                )
            return Audience.unknown(label, str(error))
        label = adapter.parse_ref(generic.id).encoded
        if not isinstance(adapter, AudienceReader):
            return Audience.unknown(label, f"{adapter.name} has no audience reader")
        found = adapter.audience(adapter.parse_ref(generic.id), draft=draft)
        if not isinstance(found, Audience):
            return Audience.unknown(
                label,
                f"{adapter.name}'s audience reader returned {type(found).__name__}, not an Audience",
            )
        if found.ref != label:
            return Audience.unknown(
                label,
                f"{adapter.name}'s audience reader answered for {found.ref}, not {label}",
            )
        return found
    except Exception as error:  # an audience nobody could compute is public, whatever failed
        return Audience.unknown(
            label, f"computing the audience failed ({type(error).__name__}): {error}"
        )


# --------------------------------------------------------------------------- writes


def _feature_check(adapter: Any, draft: Draft) -> str | None:
    """Raise for a feature the channel lacks; return a validation problem, or ``None``."""
    caps: Capabilities = adapter.capabilities
    if draft.priority not in (None, "normal") and caps.priority is Support.NONE:
        raise NotSupported(
            "priority", adapter.name, alternatives=("send without a priority",)
        )
    if draft.reply_to and caps.reply is Support.NONE:
        raise NotSupported("reply", adapter.name, alternatives=("send without reply_to",))
    if not draft.text.strip():
        return "nothing to send: the text is empty"
    if caps.max_text_length and len(draft.text) > caps.max_text_length:
        return f"the text is {len(draft.text)} characters; {adapter.name} accepts at most {caps.max_text_length}"
    if draft.title and caps.max_title_length and len(draft.title) > caps.max_title_length:
        return f"the title is {len(draft.title)} characters; {adapter.name} accepts at most {caps.max_title_length}"
    return None


def _write(
    adapter: Any, ref: ConversationRef, operation: str, dry_run: bool, call
) -> SendResult:
    try:
        return call()
    except ChannelError as error:
        return SendResult.failure(
            error,
            channel=adapter.name,
            conversation=ref.encoded,
            operation=operation,
            dry_run=dry_run,
        )


def _invalid(
    adapter: Any, ref: ConversationRef, operation: str, dry_run: bool, problem: str
):
    return SendResult(
        ok=False,
        channel=adapter.name,
        conversation=ref.encoded,
        operation=operation,
        dry_run=dry_run,
        error=problem,
        error_kind="validation",
    )


def send(
    ref: str | ConversationRef,
    text: str | Draft,
    *,
    title: str | None = None,
    reply_to: str | None = None,
    priority: str | None = None,
    dry_run: bool = False,
    registry: Mapping[str, Any] | None = None,
) -> SendResult:
    """Send ``text`` (or a :class:`~correspond.model.Draft`) to a conversation; ``dry_run`` shows the plan and contacts nothing."""
    adapter, ref = _adapter_for(ref, "send", registry)
    draft = (
        text
        if isinstance(text, Draft)
        else Draft(text=text, title=title, reply_to=reply_to, priority=priority)
    )
    problem = _feature_check(adapter, draft)
    if problem:
        return _invalid(adapter, ref, "send", dry_run, problem)
    return _write(
        adapter, ref, "send", dry_run, lambda: adapter.send(ref, draft, dry_run=dry_run)
    )


def edit(
    ref: str | ConversationRef,
    message_id: str,
    text: str,
    *,
    dry_run: bool = False,
    registry: Mapping[str, Any] | None = None,
) -> SendResult:
    """Replace the text of a message correspond's account wrote."""
    adapter, ref = _adapter_for(ref, "edit", registry)
    draft = Draft(text=text)
    problem = _feature_check(adapter, draft) or (
        None if str(message_id).strip() else "which message? the message id is empty"
    )
    if problem:
        return _invalid(adapter, ref, "edit", dry_run, problem)
    return _write(
        adapter,
        ref,
        "edit",
        dry_run,
        lambda: adapter.edit(ref, str(message_id), draft, dry_run=dry_run),
    )


def react(
    ref: str | ConversationRef,
    message_id: str,
    reaction: str,
    *,
    dry_run: bool = False,
    registry: Mapping[str, Any] | None = None,
) -> SendResult:
    """Add a reaction to a message (the channel's capabilities list the reactions it accepts)."""
    adapter, ref = _adapter_for(ref, "react", registry)
    allowed = adapter.capabilities.reactions
    problem = None
    if not str(message_id).strip():
        problem = "which message? the message id is empty"
    elif not reaction or (allowed and reaction not in allowed):
        problem = f"{adapter.name} accepts the reactions {', '.join(allowed) or '(any single emoji)'}, not {reaction!r}"
    if problem:
        return _invalid(adapter, ref, "react", dry_run, problem)
    return _write(
        adapter,
        ref,
        "react",
        dry_run,
        lambda: adapter.react(ref, str(message_id), reaction, dry_run=dry_run),
    )


def upload(
    ref: str | ConversationRef,
    name: str,
    data: bytes,
    *,
    media_type: str = "application/octet-stream",
    dry_run: bool = False,
    registry: Mapping[str, Any] | None = None,
) -> SendResult:
    """Send a file to a conversation."""
    adapter, ref = _adapter_for(ref, "upload", registry)
    limit = adapter.capabilities.max_upload_bytes
    if limit and len(data) > limit:
        return _invalid(
            adapter,
            ref,
            "upload",
            dry_run,
            f"{len(data)} bytes; {adapter.name} accepts at most {limit}",
        )
    return _write(
        adapter,
        ref,
        "upload",
        dry_run,
        lambda: adapter.upload(ref, name, data, media_type=media_type, dry_run=dry_run),
    )
