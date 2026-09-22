# correspond.ops

The operations: small protocols an adapter implements a subset of, and the verbs that call them.

An adapter is any object with a `name`, `capabilities` and `parse_ref(id)`, plus the
methods of whichever protocols it can honour:

| Reader         | `read(ref, *, since=None, limit=None) -> Iterable[Message]`           |
|----------------|-----------------------------------------------------------------------|
| Listener       | `poll(ref, *, cursor=None, limit=None) -> Iterable[Event]`            |
| Writer         | `send(ref, draft, *, dry_run=False) -> SendResult`                    |
| Editor         | `edit(ref, message_id, draft, *, dry_run=False) -> SendResult`        |
| Reactor        | `react(ref, message_id, reaction, *, dry_run=False) -> SendResult`    |
| Uploader       | `upload(ref, name, data, *, media_type, dry_run=False) -> SendResult` |
| Verifier       | `verify(headers, body) -> Authenticity`                               |
| AudienceReader | `audience(ref, *, draft=None) -> Audience`                            |

The verbs here ([`read()`](#correspond.ops.read), [`listen()`](#correspond.ops.listen), [`send()`](#correspond.ops.send), …) take a reference string,
find the adapter in the registry, and raise [`NotSupported`](correspond.errors.md#correspond.errors.NotSupported)
naming the operation when the adapter lacks it; never a silent no-op. Writes check the
draft against the channel’s capabilities first, and turn a
[`ChannelError`](correspond.errors.md#correspond.errors.ChannelError) into a `SendResult` with `ok=False`, so a
failed notification never crashes its caller. `dry_run=True` sends nothing and changes
nothing; the only call it makes is the audience lookup.
[`audience()`](#correspond.ops.audience) is the exception to refusing: it never raises, because an audience
nobody can compute is public.

Before every write ([`send()`](#correspond.ops.send), [`edit()`](#correspond.ops.edit), [`react()`](#correspond.ops.react), [`upload()`](#correspond.ops.upload)), and in its
dry run, the `before_send` check runs with the conversation’s audience
([`correspond.outbound`](correspond.outbound.md#module-correspond.outbound)). Its verdict and the audience in words go into the plan, and a
check that refuses, holds for approval, cannot be loaded or fails stops the write with that
`error_kind`. Calling an adapter’s own methods skips the check: write through these verbs.

### Module Attributes

| [`PROTOCOLS`](#correspond.ops.PROTOCOLS)   | Operation name → the protocol an adapter implements to have it.   |
|--------------------------------------------------------------|-------------------------------------------------------------------|

### Functions

| [`audience`](#correspond.ops.audience)(ref[, draft, registry])                | Who can read a conversation, asked of its channel now.                                                                                                                  |
|--------------------------------------------------------------------------------------------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| [`capabilities`](#correspond.ops.capabilities)(channel, \*[, registry])           | What a channel can do, graded, with its limits.                                                                                                                         |
| [`edit`](#correspond.ops.edit)(ref, message_id, text, \*[, dry_run, ...]) | Replace the text of a message correspond's account wrote; the `before_send` check runs first, as for [`send()`](#correspond.ops.send).           |
| [`get_channel`](#correspond.ops.get_channel)(name, \*[, registry])               | The adapter registered under `name`; [`UnknownChannel`](correspond.errors.md#correspond.errors.UnknownChannel) says what to do if there is none. |
| [`implemented`](#correspond.ops.implemented)(adapter)                            | The operations an adapter implements, in [`OPERATIONS`](correspond.model.md#correspond.model.OPERATIONS) order.                                 |
| [`listen`](#correspond.ops.listen)(ref, \*[, cursors, limit, commit, ...])  | Events since the cursor stored for `ref`; each cursor is stored once the consumer moves past its event.                                                                 |
| [`parse_ref`](#correspond.ops.parse_ref)(ref, \*[, registry])                  | A reference normalised by its channel's adapter (kind and parent filled in, the id validated).                                                                          |
| [`react`](#correspond.ops.react)(ref, message_id, reaction, \*[, ...])     | Add a reaction to a message (the channel's capabilities list the reactions it accepts); the `before_send` check sees the reaction as the draft's text.                  |
| [`read`](#correspond.ops.read)(ref, \*[, since, limit, registry])         | The messages of a conversation, oldest first (`limit` keeps the most recent ones).                                                                                      |
| [`send`](#correspond.ops.send)(ref, text, \*[, title, reply_to, ...])     | Send `text` (or a [`Draft`](correspond.model.md#correspond.model.Draft)) to a conversation; `dry_run` shows the plan and sends nothing.         |
| [`upload`](#correspond.ops.upload)(ref, name, data, \*[, media_type, ...])  | Send a file to a conversation.                                                                                                                                          |
| [`verify`](#correspond.ops.verify)(channel, headers, body, \*[, registry])  | Grade an inbound delivery on `channel` from its headers and raw body.                                                                                                   |
| [`window`](#correspond.ops.window)(messages, \*[, since, limit])            | Messages sent or edited at or after `since`, oldest first, keeping the last `limit`.                                                                                    |
| [`with_final_cursor`](#correspond.ops.with_final_cursor)(events, final_cursor)         | Yield `events`, then hand `final_cursor` to [`listen()`](#correspond.ops.listen), so a quiet poll still moves the cursor forward.                  |

### Classes

| [`AudienceReader`](#correspond.ops.AudienceReader)(\*args, \*\*kwargs)   | Who can read a conversation, now and plausibly later, asked of the platform at the time of the call.   |
|---------------------------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------|
| [`Channel`](#correspond.ops.Channel)(\*args, \*\*kwargs)          | What every adapter has: a name, its capabilities, and the grammar of its conversation ids.             |
| [`Editor`](#correspond.ops.Editor)(\*args, \*\*kwargs)           | Replace the text of a message correspond's account wrote.                                              |
| [`Listener`](#correspond.ops.Listener)(\*args, \*\*kwargs)         | Events since `cursor`, oldest first, each carrying the cursor to resume after it.                      |
| [`Reactor`](#correspond.ops.Reactor)(\*args, \*\*kwargs)          | Add a reaction to a message.                                                                           |
| [`Reader`](#correspond.ops.Reader)(\*args, \*\*kwargs)           | Messages of a conversation, oldest first; `limit` keeps the most recent.                               |
| [`Uploader`](#correspond.ops.Uploader)(\*args, \*\*kwargs)         | Send a file to a conversation.                                                                         |
| [`Verifier`](#correspond.ops.Verifier)(\*args, \*\*kwargs)         | Grade an inbound delivery (a webhook, a posted report) from its headers and raw body.                  |
| [`Writer`](#correspond.ops.Writer)(\*args, \*\*kwargs)           | Send a draft to a conversation.                                                                        |

### *class* correspond.ops.AudienceReader(\*args, \*\*kwargs)

Bases: [`Protocol`](https://docs.python.org/3/library/typing.html#typing.Protocol)

Who can read a conversation, now and plausibly later, asked of the platform at the time of the call.

### *class* correspond.ops.Channel(\*args, \*\*kwargs)

Bases: [`Protocol`](https://docs.python.org/3/library/typing.html#typing.Protocol)

What every adapter has: a name, its capabilities, and the grammar of its conversation ids.

### *class* correspond.ops.Editor(\*args, \*\*kwargs)

Bases: [`Protocol`](https://docs.python.org/3/library/typing.html#typing.Protocol)

Replace the text of a message correspond’s account wrote.

### *class* correspond.ops.Listener(\*args, \*\*kwargs)

Bases: [`Protocol`](https://docs.python.org/3/library/typing.html#typing.Protocol)

Events since `cursor`, oldest first, each carrying the cursor to resume after it.

### correspond.ops.PROTOCOLS *: [dict](https://docs.python.org/3/builtins/stdtypes.html#dict)[[str](https://docs.python.org/3/builtins/stdtypes.html#str), [type](https://docs.python.org/3/builtins/functions.html#type)]* *= {'audience': <class 'correspond.ops.AudienceReader'>, 'edit': <class 'correspond.ops.Editor'>, 'listen': <class 'correspond.ops.Listener'>, 'react': <class 'correspond.ops.Reactor'>, 'read': <class 'correspond.ops.Reader'>, 'send': <class 'correspond.ops.Writer'>, 'upload': <class 'correspond.ops.Uploader'>, 'verify': <class 'correspond.ops.Verifier'>}*

Operation name → the protocol an adapter implements to have it.

### *class* correspond.ops.Reactor(\*args, \*\*kwargs)

Bases: [`Protocol`](https://docs.python.org/3/library/typing.html#typing.Protocol)

Add a reaction to a message.

### *class* correspond.ops.Reader(\*args, \*\*kwargs)

Bases: [`Protocol`](https://docs.python.org/3/library/typing.html#typing.Protocol)

Messages of a conversation, oldest first; `limit` keeps the most recent.

### *class* correspond.ops.Uploader(\*args, \*\*kwargs)

Bases: [`Protocol`](https://docs.python.org/3/library/typing.html#typing.Protocol)

Send a file to a conversation.

### *class* correspond.ops.Verifier(\*args, \*\*kwargs)

Bases: [`Protocol`](https://docs.python.org/3/library/typing.html#typing.Protocol)

Grade an inbound delivery (a webhook, a posted report) from its headers and raw body.

### *class* correspond.ops.Writer(\*args, \*\*kwargs)

Bases: [`Protocol`](https://docs.python.org/3/library/typing.html#typing.Protocol)

Send a draft to a conversation.

### correspond.ops.audience(ref, draft=None, , registry=None)

Who can read a conversation, asked of its channel now. Never raises: unknown resolves to public.

A malformed reference, an unknown or planned channel, an adapter without an audience
reader, and any error while computing all give `scope="public"`, `complete=False`,
`defaulted=True`, with the reason in `evidence`. Nothing is cached: call it again
right before sending.

* **Return type:**
  [`Audience`](correspond.model.md#correspond.model.Audience)

### correspond.ops.capabilities(channel, , registry=None)

What a channel can do, graded, with its limits.

* **Return type:**
  [`Capabilities`](correspond.model.md#correspond.model.Capabilities)

### correspond.ops.edit(ref, message_id, text, , dry_run=False, registry=None, before_send=None)

Replace the text of a message correspond’s account wrote; the `before_send` check runs first, as for [`send()`](#correspond.ops.send).

* **Return type:**
  [`SendResult`](correspond.model.md#correspond.model.SendResult)

### correspond.ops.get_channel(name, , registry=None)

The adapter registered under `name`; [`UnknownChannel`](correspond.errors.md#correspond.errors.UnknownChannel) says what to do if there is none.

* **Return type:**
  [`Any`](https://docs.python.org/3/library/typing.html#typing.Any)

### correspond.ops.implemented(adapter)

The operations an adapter implements, in [`OPERATIONS`](correspond.model.md#correspond.model.OPERATIONS) order.

* **Return type:**
  [`tuple`](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`...`](https://docs.python.org/3/builtins/constants.html#Ellipsis)]

### correspond.ops.listen(ref, , cursors=None, limit=None, commit=True, registry=None)

Events since the cursor stored for `ref`; each cursor is stored once the consumer moves past its event.

`cursors` defaults to files under the data root. Events can repeat after a crash or an
early `break`: deduplicate on `delivery_id`.

* **Return type:**
  [`Iterator`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Iterator)[[`Event`](correspond.model.md#correspond.model.Event)]

### correspond.ops.parse_ref(ref, , registry=None)

A reference normalised by its channel’s adapter (kind and parent filled in, the id validated).

* **Return type:**
  [`ConversationRef`](correspond.model.md#correspond.model.ConversationRef)

### correspond.ops.react(ref, message_id, reaction, , dry_run=False, registry=None, before_send=None)

Add a reaction to a message (the channel’s capabilities list the reactions it accepts); the `before_send` check sees the reaction as the draft’s text.

* **Return type:**
  [`SendResult`](correspond.model.md#correspond.model.SendResult)

### correspond.ops.read(ref, , since=None, limit=None, registry=None)

The messages of a conversation, oldest first (`limit` keeps the most recent ones).

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`Message`](correspond.model.md#correspond.model.Message)]

### correspond.ops.send(ref, text, , title=None, reply_to=None, priority=None, cc=(), bcc=(), dry_run=False, registry=None, before_send=None)

Send `text` (or a [`Draft`](correspond.model.md#correspond.model.Draft)) to a conversation; `dry_run` shows the plan and sends nothing.

`cc` and `bcc` copy further recipients, on channels that grade `cc` (email).
`before_send(ref, draft, audience)` runs first, on the dry run too; when `None`, the
config’s `before_send` reference, else [`correspond.outbound.notice()`](correspond.outbound.md#correspond.outbound.notice).

* **Return type:**
  [`SendResult`](correspond.model.md#correspond.model.SendResult)

### correspond.ops.upload(ref, name, data, , media_type='application/octet-stream', dry_run=False, registry=None, before_send=None)

Send a file to a conversation. The `before_send` check sees only the file name as the draft’s text (`operation="upload"`): a check that cannot vet the bytes should hold or refuse.

* **Return type:**
  [`SendResult`](correspond.model.md#correspond.model.SendResult)

### correspond.ops.verify(channel, headers, body, , registry=None)

Grade an inbound delivery on `channel` from its headers and raw body.

* **Return type:**
  [`Authenticity`](correspond.model.md#correspond.model.Authenticity)

### correspond.ops.window(messages, , since=None, limit=None)

Messages sent or edited at or after `since`, oldest first, keeping the last `limit`.

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`Message`](correspond.model.md#correspond.model.Message)]

### correspond.ops.with_final_cursor(events, final_cursor)

Yield `events`, then hand `final_cursor` to [`listen()`](#correspond.ops.listen), so a quiet poll still moves the cursor forward.

* **Return type:**
  [`Iterator`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Iterator)[[`Event`](correspond.model.md#correspond.model.Event)]
