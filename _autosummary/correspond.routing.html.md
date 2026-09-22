# correspond.routing

Deciding what a message is about: a transparent rule chain.

The rules belong to the caller; correspond runs them in a fixed order and reports which
rule decided and why.

1. **Bindings**, `pattern → target`. A pattern is a conversation-reference glob (`*`
   and `[…]`; `?` always starts the conditions), optionally followed by
   `?field=glob&…` conditions on the message (`%`-escapes decoded, `+` kept): a `native` field
   (`labels`, `state`, …), `author` (handle, native id or address) or `grade`. A
   pattern without wildcards also matches the conversations under it, so
   `github:example/app` matches `github:example/app#12`.
2. **Thread continuity**: `threads` maps a thread root, a replied-to message id or a
   conversation reference to the target it already belongs to.
3. **Metadata rules**: callables `message -> target | None`, tried in order
   ([`metadata_rule()`](#correspond.routing.metadata_rule) builds one from `field=glob` conditions).
4. **The classifier**: one optional callable, last, for what rules cannot see. It returns a
   target, `(target, reason)`, or `None`.

Nothing matched: `route` returns `None` and the message is unrouted. What that means
(an operator queue, a drop) is the caller’s decision. A condition on a field the channel’s
messages never carry (`?label=` where GitHub messages carry `labels`) never matches;
[`check_binding()`](#correspond.routing.check_binding) reports that when bindings are loaded.

```pycon
>>> from correspond.testing import demo_message
>>> message = demo_message(conversation="github:example/app#12", labels=["partner:ada"])
>>> route(message, bindings={"github:example/app?labels=partner:*": "subject:app"})
RouteDecision(target='subject:app', rule='binding', reason='github:example/app?labels=partner:* matched github:example/app#12')
>>> route(message, bindings={"github:example/other": "subject:other"}) is None
True
```

### Module Attributes

| [`RULES`](#correspond.routing.RULES)          | The order the chain runs in.                                                |
|-----------------------------------------------------------------|-----------------------------------------------------------------------------|
| [`MESSAGE_FIELDS`](#correspond.routing.MESSAGE_FIELDS) | Condition fields every message has, whatever its channel's `native` fields. |

### Functions

| [`binding_matches`](#correspond.routing.binding_matches)(pattern, message)            | The reason `pattern` matches `message`, or `None`.                                                                   |
|-----------------------------------------------------------------------------------------------|----------------------------------------------------------------------------------------------------------------------|
| [`check_binding`](#correspond.routing.check_binding)(pattern, \*[, registry])       | What would make a binding never match, found when bindings are loaded instead of by messages quietly going unrouted. |
| [`metadata_rule`](#correspond.routing.metadata_rule)(target, \*[, name])            | A rule sending messages to `target` when every `field=glob` condition holds.                                         |
| [`route`](#correspond.routing.route)(message, \*[, bindings, threads, ...]) | Run the chain (bindings, thread continuity, metadata rules, classifier) and return the first decision, or `None`.    |

### Classes

| [`RouteDecision`](#correspond.routing.RouteDecision)(\*, target, rule, reason)   | Where a message goes, which rule decided, and why.   |
|--------------------------------------------------------------------------------------------|------------------------------------------------------|

### correspond.routing.MESSAGE_FIELDS *= ('author', 'grade')*

Condition fields every message has, whatever its channel’s `native` fields.

### correspond.routing.RULES *= ('binding', 'thread', 'metadata', 'classifier')*

The order the chain runs in.

### *class* correspond.routing.RouteDecision(, target, rule, reason)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

Where a message goes, which rule decided, and why.

#### to_dict()

JSON-ready.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)

### correspond.routing.binding_matches(pattern, message)

The reason `pattern` matches `message`, or `None`.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str) | [`None`](https://docs.python.org/3/builtins/constants.html#None)

### correspond.routing.check_binding(pattern, , registry=None)

What would make a binding never match, found when bindings are loaded instead of by messages quietly going unrouted.

Reports a pattern without a channel, an unknown channel, a ref that is not in the
channel’s canonical form (e.g. GitHub references are lower-cased by
[`parse_ref()`](correspond.channels.github.html.md#correspond.channels.github.GitHub.parse_ref), so `binding_matches`, which
compares the pattern literally, never matches a differently-cased pattern against it),
and a condition on a field the channel’s messages never carry (its `native_fields`,
plus `author` and `grade`). A channel written as a wildcard, or one that does not
declare its fields (`native_fields` is `None`), is not checked for fields. A ref
with wildcards, or one the channel’s own parser rejects outright, is not checked for
canonical form – a malformed ref is a different problem than a mis-cased one.

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]

```pycon
>>> from correspond.channels.github import GitHub
>>> check_binding("github:example/app?labels=bug", registry={"github": GitHub()})
[]
>>> check_binding("github:example/app?label=bug", registry={"github": GitHub()})[0].split(":")[0]
'the condition label=bug never matches'
>>> check_binding("github:Example/App", registry={"github": GitHub()})
["github:Example/App is not in canonical form: github references are normalised to 'github:example/app'; binding_matches compares refs literally, so this pattern will not match github:example/app"]
```

### correspond.routing.metadata_rule(target, , name=None, \*\*conditions)

A rule sending messages to `target` when every `field=glob` condition holds.

* **Return type:**
  [`Callable`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Callable)[[[`Message`](correspond.model.html.md#correspond.model.Message)], [`str`](https://docs.python.org/3/builtins/stdtypes.html#str) | [`None`](https://docs.python.org/3/builtins/constants.html#None)]

```pycon
>>> from correspond.testing import demo_message
>>> rule = metadata_rule("urgent-queue", labels="priority:high")
>>> rule(demo_message(labels=["priority:high"])), rule(demo_message())
('urgent-queue', None)
```

### correspond.routing.route(message, , bindings=None, threads=None, rules=(), classifier=None)

Run the chain (bindings, thread continuity, metadata rules, classifier) and return the first decision, or `None`.

* **Return type:**
  [`RouteDecision`](#correspond.routing.RouteDecision) | [`None`](https://docs.python.org/3/builtins/constants.html#None)
