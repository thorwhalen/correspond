# correspond.channel

Serve listeners as a Claude Code channel: new messages pushed into a running session, replies through the ordinary tools.

Run `correspond-channel` (install the extra first: `pip install "correspond[mcp]"`). It
is an MCP stdio server that declares `experimental['claude/channel']`, polls
[`correspond.listen()`](correspond.html.md#correspond.listen) for the conversations it was given, and emits one
`notifications/claude/channel` per new message, from an allowlisted sender, as:

```default
{"content": "<the text>",
 "meta": {"channel": "github", "conversation": "github:example/demo#1",
          "message_id": "...", "sender": "github:ada", "grade": "platform"}}
```

which Claude sees as `<channel source="correspond" channel="github" ...>text</channel>`.
Meta keys are identifiers only (Claude Code silently drops a key with a hyphen).

**Replies are the ordinary correspond tools** ([`correspond.tools`](correspond.tools.html.md#module-correspond.tools), through
[`correspond.mcp.mk_server()`](correspond.mcp.html.md#correspond.mcp.mk_server)): the reads by default, the writes only with
`--allow-send`, each write dry-run first and through the operator’s `before_send` check.

**Senders.** Only messages from senders on the allowlist (`--allow github:ada`, matched on
the sender’s attested address or `<channel>:<native id>`, never the room) are forwarded;
an empty allowlist forwards nothing unless `--allow-any-sender` opens it. A `forged`
message is never forwarded, the bot’s own messages never loop back, and events without an
attested sender (a bare reaction) are not forwarded.

**Permission relay** (`claude/channel/permission`). With `--relay-to <ref>` (which needs
`--allow-send`, because relaying is a send) the server declares the permission capability;
each `notifications/claude/channel/permission_request` is sent to that conversation as a
prompt ending in `Reply "yes <id>" or "no <id>"`; and a reply in that conversation of
that shape becomes a `notifications/claude/channel/permission` verdict, never chat, but
only when its sender is on the explicit allowlist (`--allow-any-sender` does not count),
its authenticity grade is one of [`APPROVE_GRADES`](#correspond.channel.APPROVE_GRADES) (authenticated: not `claimed` or
`forged`), and the id is one this server relayed and has not yet answered.

How the Python MCP SDK does this (`mcp` 1.27, `fastmcp` 3.3, verified by the tests):

- *Declaring the capability*: the low-level `mcp.server.lowlevel.Server` takes it as
  `create_initialization_options(experimental_capabilities={"claude/channel": {}})`;
  FastMCP (what `py2mcp` builds) keeps a public `experimental_capabilities` dict that its
  low-level server merges into the same call. This module sets that dict.
- *Emitting a custom method*: `ServerSession.send_notification` only `model_dump``s its
  argument into a ``JSONRPCNotification`, so any model with `method` and `params`
  (`mcp.types.Notification`) goes out unvalidated. A background task has no session handle
  in FastMCP, so this module writes the same `SessionMessage(JSONRPCNotification(...))` onto
  the transport’s write stream, after the client’s `notifications/initialized`.
- *Receiving a custom method*: the SDK validates incoming notifications against the closed
  `ClientNotification` union and drops anything else with a warning, so
  `permission_request` is taken off the read stream by a tap in front of the session.

### Module Attributes

| [`META_KEYS`](#correspond.channel.META_KEYS)      | identifiers only.                                                                    |
|-----------------------------------------------------------------|--------------------------------------------------------------------------------------|
| [`FORWARD_GRADES`](#correspond.channel.FORWARD_GRADES) | everything but a failed verification.                                                |
| [`APPROVE_GRADES`](#correspond.channel.APPROVE_GRADES) | the authenticated ones.                                                              |
| [`CURSOR_KIND`](#correspond.channel.CURSOR_KIND)    | Where the channel server keeps its listen cursors, apart from `correspond listen`'s. |

### Functions

| [`channel_notification`](#correspond.channel.channel_notification)(event)                       | The `notifications/claude/channel` message for one event that carries a message.                                                         |
|----------------------------------------------------------------------------------------------------|------------------------------------------------------------------------------------------------------------------------------------------|
| [`main`](#correspond.channel.main)([argv])                                      | Serve the channel over stdio (see `options()` for the arguments).                                                                        |
| [`notifications`](#correspond.channel.notifications)(refs, \*, gate[, cursors, ...])     | One poll of every ref: the notifications its new events become, deduplicated on delivery id.                                             |
| [`parse_verdict`](#correspond.channel.parse_verdict)(text)                               | `("allow" | "deny", request_id)` for a reply like `yes abcde`, else `None`.                                                              |
| [`permission_prompt`](#correspond.channel.permission_prompt)(params)                         | The text sent to the relay conversation for one permission request.                                                                      |
| [`serve`](#correspond.channel.serve)(\*\*kwargs)                                 | Run the channel over stdio until the client goes away; `kwargs` as for [`serve_streams()`](#correspond.channel.serve_streams). |
| [`serve_streams`](#correspond.channel.serve_streams)(read_stream, write_stream, \*, ...) | Run the channel over a pair of MCP message streams (stdio in [`serve()`](#correspond.channel.serve), memory in tests). |
| [`verdict_notification`](#correspond.channel.verdict_notification)(request_id, behavior)        | The `notifications/claude/channel/permission` message carrying a verdict.                                                                |

### Classes

| [`Gate`](#correspond.channel.Gate)([allow, allow_any, forward_grades, ...])   | Who gets through, as a pure policy: the allowlist, the grades forwarded, and who may approve.   |
|--------------------------------------------------------------------------------------------------|-------------------------------------------------------------------------------------------------|

### correspond.channel.APPROVE_GRADES *= frozenset({Grade.BOUND, Grade.CRYPTO, Grade.DOMAIN, Grade.PLATFORM})*

the authenticated ones.

* **Type:**
  Grades whose sender may approve tool use

### correspond.channel.CURSOR_KIND *= 'channel-cursors'*

Where the channel server keeps its listen cursors, apart from `correspond listen`’s.

### correspond.channel.FORWARD_GRADES *= frozenset({Grade.BOUND, Grade.CLAIMED, Grade.CRYPTO, Grade.DOMAIN, Grade.PLATFORM})*

everything but a failed verification.

* **Type:**
  Grades forwarded by default

### *class* correspond.channel.Gate(allow=frozenset({}), allow_any=False, forward_grades=frozenset({Grade.BOUND, Grade.CLAIMED, Grade.CRYPTO, Grade.DOMAIN, Grade.PLATFORM}), approve_grades=frozenset({Grade.BOUND, Grade.CRYPTO, Grade.DOMAIN, Grade.PLATFORM}), relay_to=None)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

Who gets through, as a pure policy: the allowlist, the grades forwarded, and who may approve.

`allow` holds sender addresses (`github:ada`) or `<channel>:<native id>`; empty
forwards nothing unless `allow_any`. `relay_to` is the canonical relay conversation
(verdicts are read there only); approval needs the explicit allowlist and a grade in
`approve_grades`, which may hold only authenticated grades.

#### allowed(event)

Whether this event’s sender may put text in front of Claude.

* **Return type:**
  [`bool`](https://docs.python.org/3/builtins/functions.html#bool)

#### may_approve(event)

Whether this event’s sender may answer a permission prompt: allowlisted by name and authenticated.

* **Return type:**
  [`bool`](https://docs.python.org/3/builtins/functions.html#bool)

#### route(event, , pending=None)

The notification an event becomes (a channel message or a verdict), or `None` when it is dropped.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict) | [`None`](https://docs.python.org/3/builtins/constants.html#None)

### correspond.channel.META_KEYS *= ('channel', 'conversation', 'message_id', 'sender', 'grade')*

identifiers only.

* **Type:**
  The `meta` keys of every channel notification, in order

### correspond.channel.channel_notification(event)

The `notifications/claude/channel` message for one event that carries a message.

```pycon
>>> from correspond.testing import demo_message
>>> note = channel_notification(Event(kind="message.created", channel="fake", delivery_id="d1",
...                                   message=demo_message()))
>>> note["method"], note["params"]["content"]
('notifications/claude/channel', 'The export drops the last row.')
>>> note["params"]["meta"]
{'channel': 'fake', 'conversation': 'fake:example/demo', 'message_id': 'm1', 'sender': 'fake:ada', 'grade': 'platform'}
```

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)

### correspond.channel.main(argv=None)

Serve the channel over stdio (see `options()` for the arguments).

* **Return type:**
  [`None`](https://docs.python.org/3/builtins/constants.html#None)

### correspond.channel.notifications(refs, \*, gate, cursors=None, registry=None, seen=None, pending=None, limit=None, seen_limit=10000, log=<function \_log>)

One poll of every ref: the notifications its new events become, deduplicated on delivery id.

Lazy on purpose: [`correspond.listen()`](correspond.html.md#correspond.listen) stores an event’s cursor only when the next one
is asked for, so a consumer that emits each notification before asking for the next one
gets at-least-once delivery, and `seen` makes it once per process. A ref that fails is
logged and skipped; the others are still polled.

* **Return type:**
  [`Iterator`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Iterator)[[`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)]

### correspond.channel.parse_verdict(text)

`("allow" | "deny", request_id)` for a reply like `yes abcde`, else `None`.

```pycon
>>> parse_verdict("Yes ABCDE"), parse_verdict("no qwxyz"), parse_verdict("yes"), parse_verdict("yes hello")
(('allow', 'abcde'), ('deny', 'qwxyz'), None, None)
```

* **Return type:**
  [`tuple`](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)] | [`None`](https://docs.python.org/3/builtins/constants.html#None)

### correspond.channel.permission_prompt(params)

The text sent to the relay conversation for one permission request.

```pycon
>>> print(permission_prompt({"request_id": "abcde", "tool_name": "Bash",
...                          "description": "List files", "input_preview": '{"command": "ls"}'}))
Claude wants to run Bash: List files
{"command": "ls"}

Reply "yes abcde" or "no abcde"
```

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

### correspond.channel.serve(\*\*kwargs)

Run the channel over stdio until the client goes away; `kwargs` as for [`serve_streams()`](#correspond.channel.serve_streams).

* **Return type:**
  [`None`](https://docs.python.org/3/builtins/constants.html#None)

### *async* correspond.channel.serve_streams(read_stream, write_stream, \*, refs, gate, allow_send=False, interval=30.0, cursors=None, registry=None, send=None, server=None, log=<function \_log>)

Run the channel over a pair of MCP message streams (stdio in [`serve()`](#correspond.channel.serve), memory in tests).

`server` defaults to [`correspond.mcp.mk_server()`](correspond.mcp.html.md#correspond.mcp.mk_server) with the channel instructions;
`send` (the relay’s outbound) defaults to [`correspond.send()`](correspond.html.md#correspond.send) on `registry`.

* **Return type:**
  [`None`](https://docs.python.org/3/builtins/constants.html#None)

### correspond.channel.verdict_notification(request_id, behavior)

The `notifications/claude/channel/permission` message carrying a verdict.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)
