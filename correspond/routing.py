"""Deciding what a message is about: a transparent rule chain.

The rules belong to the caller; correspond runs them in a fixed order and reports which
rule decided and why.

1. **Bindings**, ``pattern → target``. A pattern is a conversation-reference glob (``*``
   and ``[…]``; ``?`` always starts the conditions), optionally followed by
   ``?field=glob&…`` conditions on the message (``%``-escapes decoded, ``+`` kept): a ``native`` field
   (``labels``, ``state``, …), ``author`` (handle, native id or address) or ``grade``. A
   pattern without wildcards also matches the conversations under it, so
   ``github:example/app`` matches ``github:example/app#12``.
2. **Thread continuity**: ``threads`` maps a thread root, a replied-to message id or a
   conversation reference to the target it already belongs to.
3. **Metadata rules**: callables ``message -> target | None``, tried in order
   (:func:`metadata_rule` builds one from ``field=glob`` conditions).
4. **The classifier**: one optional callable, last, for what rules cannot see. It returns a
   target, ``(target, reason)``, or ``None``.

Nothing matched: ``route`` returns ``None`` and the message is unrouted. What that means
(an operator queue, a drop) is the caller's decision. A condition on a field the channel's
messages never carry (``?label=`` where GitHub messages carry ``labels``) never matches;
:func:`check_binding` reports that when bindings are loaded.

>>> from correspond.testing import demo_message
>>> message = demo_message(conversation="github:example/app#12", labels=["partner:ada"])
>>> route(message, bindings={"github:example/app?labels=partner:*": "subject:app"})
RouteDecision(target='subject:app', rule='binding', reason='github:example/app?labels=partner:* matched github:example/app#12')
>>> route(message, bindings={"github:example/other": "subject:other"}) is None
True
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any
from urllib.parse import unquote

from correspond.errors import CorrespondError
from correspond.model import Message

__all__ = [
    "MESSAGE_FIELDS",
    "RULES",
    "RouteDecision",
    "binding_matches",
    "check_binding",
    "metadata_rule",
    "route",
]

#: The order the chain runs in.
RULES = ("binding", "thread", "metadata", "classifier")
_WILDCARDS = set("*?[")


@dataclass(frozen=True, kw_only=True)
class RouteDecision:
    """Where a message goes, which rule decided, and why."""

    target: str
    rule: str
    reason: str

    def to_dict(self) -> dict:
        """JSON-ready."""
        return {"target": self.target, "rule": self.rule, "reason": self.reason}


def _field_values(message: Message, field: str) -> list[str]:
    if field == "author":
        author = message.author
        return [v for v in (author.handle, author.native_id, author.address) if v]
    if field == "grade":
        return [message.authenticity.grade.value]
    found = message.native.get(field)
    if found is None:
        return []
    if isinstance(found, (list, tuple, set, frozenset)):
        return [str(v) for v in found]
    return [str(found)]


def _conditions_hold(message: Message, conditions: Iterable[tuple[str, str]]) -> bool:
    return all(
        any(fnmatchcase(v, glob) for v in _field_values(message, field))
        for field, glob in conditions
    )


def _conditions(query: str) -> list[tuple[str, str]]:
    """``field=glob`` pairs split on ``&``, with ``%``-escapes decoded and ``+`` left a plus."""
    pairs = []
    for part in query.split("&"):
        if part:
            field, _, glob = part.partition("=")
            pairs.append((unquote(field), unquote(glob)))
    return pairs


def binding_matches(pattern: str, message: Message) -> str | None:
    """The reason ``pattern`` matches ``message``, or ``None``."""
    ref_glob, _, query = pattern.partition("?")
    conversation = message.conversation
    refs = [conversation.encoded]
    parent = conversation.parent
    while parent is not None:
        refs.append(parent.encoded)
        parent = parent.parent
    matched = any(fnmatchcase(r, ref_glob) for r in refs) or (
        not _WILDCARDS & set(ref_glob)
        and any(conversation.encoded.startswith(ref_glob + sep) for sep in "#/")
    )
    if not matched or not _conditions_hold(message, _conditions(query)):
        return None
    return f"{pattern} matched {conversation.encoded}"


def metadata_rule(
    target: str, *, name: str | None = None, **conditions: str
) -> Callable[[Message], str | None]:
    """A rule sending messages to ``target`` when every ``field=glob`` condition holds.

    >>> from correspond.testing import demo_message
    >>> rule = metadata_rule("urgent-queue", labels="priority:high")
    >>> rule(demo_message(labels=["priority:high"])), rule(demo_message())
    ('urgent-queue', None)
    """
    pairs = tuple(conditions.items())

    def rule(message: Message) -> str | None:
        return target if _conditions_hold(message, pairs) else None

    rule.__name__ = name or "metadata_rule"
    rule.reason = f"{' & '.join(f'{f}={g}' for f, g in pairs) or 'always'} → {target}"
    return rule


def route(
    message: Message,
    *,
    bindings: Mapping[str, str] | Iterable[tuple[str, str]] | None = None,
    threads: Mapping[str, str] | None = None,
    rules: Iterable[Callable[[Message], str | None]] = (),
    classifier: Callable[[Message], str | tuple[str, str] | None] | None = None,
) -> RouteDecision | None:
    """Run the chain (bindings, thread continuity, metadata rules, classifier) and return the first decision, or ``None``."""
    pairs = bindings.items() if isinstance(bindings, Mapping) else (bindings or ())
    for pattern, target in pairs:
        reason = binding_matches(pattern, message)
        if reason:
            return RouteDecision(target=target, rule="binding", reason=reason)
    if threads:
        for label, key in (
            ("thread root", message.thread_root),
            ("reply to", message.reply_to),
            ("conversation", message.conversation.encoded),
        ):
            if key and key in threads:
                return RouteDecision(
                    target=threads[key],
                    rule="thread",
                    reason=f"{label} {key} belongs to {threads[key]}",
                )
    for rule in rules:
        target = rule(message)
        if target:
            reason = getattr(rule, "reason", None) or getattr(rule, "__name__", "rule")
            return RouteDecision(target=str(target), rule="metadata", reason=reason)
    if classifier is not None:
        verdict = classifier(message)
        if verdict:
            target, reason = (
                verdict if isinstance(verdict, tuple) else (verdict, "classifier")
            )
            return RouteDecision(target=str(target), rule="classifier", reason=reason)
    return None


#: Condition fields every message has, whatever its channel's ``native`` fields.
MESSAGE_FIELDS = ("author", "grade")


def check_binding(
    pattern: str, *, registry: Mapping[str, Any] | None = None
) -> list[str]:
    """What would make a binding never match, found when bindings are loaded instead of by messages quietly going unrouted.

    Reports a pattern without a channel, an unknown channel, and a condition on a field the
    channel's messages never carry (its ``native_fields``, plus ``author`` and ``grade``). A
    channel written as a wildcard, or one that does not declare its fields
    (``native_fields`` is ``None``), is not checked for fields.

    >>> from correspond.channels.github import GitHub
    >>> check_binding("github:example/app?labels=bug", registry={"github": GitHub()})
    []
    >>> check_binding("github:example/app?label=bug", registry={"github": GitHub()})[0].split(":")[0]
    'the condition label=bug never matches'
    """
    from correspond.ops import get_channel

    ref_glob, _, query = pattern.partition("?")
    channel, separator, _ = ref_glob.partition(":")
    if not separator or not channel:
        if ":" in query:
            return [
                f"{pattern!r}: '?' starts a binding's conditions, so it cannot come before the channel's ':'"
            ]
        return [f"{pattern!r} is not a binding: a binding starts with <channel>:"]
    if _WILDCARDS & set(channel):
        return []
    try:
        adapter = get_channel(channel, registry=registry)
    except CorrespondError as error:
        return [str(error)]
    caps = getattr(adapter, "capabilities", None)
    if caps is None:
        return [f"{channel} has no capabilities to check the binding against"]
    if caps.native_fields is None:
        return []
    carried = (*caps.native_fields, *MESSAGE_FIELDS)
    return [
        f"the condition {field}={glob} never matches: {channel} messages carry no field {field!r} (they carry {', '.join(carried)})"
        for field, glob in _conditions(query)
        if field not in carried
    ]
