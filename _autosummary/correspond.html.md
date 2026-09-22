# correspond

correspond: a channel facade for AI agents.

Read, listen, write and identify the sender over GitHub, email, push notifications,
Telegram and a web inbox, through one model and one set of verbs:

```default
>>> import correspond
>>> messages = correspond.read("github:octocat/hello-world#1")
>>> messages[0].author.handle, messages[0].authenticity.grade.value
('octocat', 'platform')
>>> correspond.send("ntfy:", "backup finished", dry_run=True).plan
```

A conversation reference is `<channel>:<id>`. Each channel implements the operations it
can (`read`, `listen`, `send`, `edit`, `react`, `upload`, `verify`,
`audience`, `label`, `unlabel`); asking for one it lacks raises [`NotSupported`](#correspond.NotSupported), and
[`capabilities()`](#correspond.capabilities) says so in advance. [`audience()`](#correspond.audience) is the exception: it never
refuses, because an audience nobody can compute is public.
correspond knows no people: a message’s `author` is what the platform attests, and its
`authenticity` is how sure the channel is.

The command line (`correspond read github:octocat/hello-world#1`) and the MCP server use
the same verbs, through [`correspond.tools`](correspond.tools.html.md#module-correspond.tools).

### Functions

| [`audience`](#correspond.audience)(ref[, draft, registry])                   | Who can read a conversation, asked of its channel now.                                                                                                                  |
|-----------------------------------------------------------------------------------------------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| [`capabilities`](#correspond.capabilities)(channel, \*[, registry])              | What a channel can do, graded, with its limits.                                                                                                                         |
| [`channel_registry`](#correspond.channel_registry)()                                 | The process registry: built on first use, then shared.                                                                                                                  |
| [`check_binding`](#correspond.check_binding)(pattern, \*[, registry])             | What would make a binding never match, found when bindings are loaded instead of by messages quietly going unrouted.                                                    |
| [`check_requirements`](#correspond.check_requirements)(channel, \*[, registry, ...])   | What a channel needs and what is missing: the install command, binaries, platform, and every setting's source.                                                          |
| [`edit`](#correspond.edit)(ref, message_id, text, \*[, dry_run, ...])    | Replace the text of a message correspond's account wrote; the `before_send` check runs first, as for [`send()`](#correspond.send).           |
| [`get_channel`](#correspond.get_channel)(name, \*[, registry])                  | The adapter registered under `name`; [`UnknownChannel`](correspond.errors.html.md#correspond.errors.UnknownChannel) says what to do if there is none. |
| [`label`](#correspond.label)(ref, labels, \*[, dry_run, registry, ...])   | Add `labels` to a conversation; the `before_send` check sees them, comma-joined, as the draft's text.                                                                   |
| [`listen`](#correspond.listen)(ref, \*[, cursors, limit, commit, ...])     | Events since the cursor stored for `ref`; each cursor is stored once the consumer moves past its event.                                                                 |
| [`metadata_rule`](#correspond.metadata_rule)(target, \*[, name])                  | A rule sending messages to `target` when every `field=glob` condition holds.                                                                                            |
| [`parse_ref`](#correspond.parse_ref)(ref, \*[, registry])                     | A reference normalised by its channel's adapter (kind and parent filled in, the id validated).                                                                          |
| [`react`](#correspond.react)(ref, message_id, reaction, \*[, ...])        | Add a reaction to a message (the channel's capabilities list the reactions it accepts); the `before_send` check sees the reaction as the draft's text.                  |
| [`read`](#correspond.read)(ref, \*[, since, limit, registry])            | The messages of a conversation, oldest first (`limit` keeps the most recent ones).                                                                                      |
| [`register_channel`](#correspond.register_channel)(adapter, \*[, name, ...])         | Add an adapter (tests, or a channel defined outside correspond).                                                                                                        |
| [`route`](#correspond.route)(message, \*[, bindings, threads, ...])       | Run the chain (bindings, thread continuity, metadata rules, classifier) and return the first decision, or `None`.                                                       |
| [`send`](#correspond.send)(ref, text, \*[, title, reply_to, ...])        | Send `text` (or a [`Draft`](correspond.model.html.md#correspond.model.Draft)) to a conversation; `dry_run` shows the plan and sends nothing.         |
| [`unlabel`](#correspond.unlabel)(ref, labels, \*[, dry_run, registry, ...]) | Remove `labels` from a conversation; the `before_send` check sees them, comma-joined, as the draft's text.                                                              |
| [`unregister_channel`](#correspond.unregister_channel)(name, \*[, registry])           | Remove a channel from the registry.                                                                                                                                     |
| [`upload`](#correspond.upload)(ref, name, data, \*[, media_type, ...])     | Send a file to a conversation.                                                                                                                                          |
| [`verify`](#correspond.verify)(channel, headers, body, \*[, registry])     | Grade an inbound delivery on `channel` from its headers and raw body.                                                                                                   |

### Classes

| [`Account`](#correspond.Account)(\*, channel, id[, acts_as])                | The credentialed endpoint correspond acts through: whose name a write goes out under.                                                        |
|-----------------------------------------------------------------------------------------------------|----------------------------------------------------------------------------------------------------------------------------------------------|
| [`Attachment`](#correspond.Attachment)(\*, ref[, media_type, name, size, ...]) | A file that came with a message: referenced by `ref`, fetched only on `content()`, never inlined.                                            |
| [`Audience`](#correspond.Audience)(\*, ref, scope[, readers, complete, ...]) | Who can read a conversation, now and plausibly later, as far as its channel can tell.                                                        |
| [`AudienceReader`](#correspond.AudienceReader)(\*args, \*\*kwargs)                 | Who can read a conversation, now and plausibly later, asked of the platform at the time of the call.                                         |
| [`Authenticity`](#correspond.Authenticity)(\*, grade[, evidence])                | A grade and its evidence, computed on correspond's side of the hop and never read from a payload.                                            |
| [`Capabilities`](#correspond.Capabilities)(\*, channel[, read, listen, ...])     | What a channel can do, graded, with its limits.                                                                                              |
| [`ChannelIdentity`](#correspond.ChannelIdentity)(\*, channel, native_id[, ...])     | Who a channel says sent something: its native id and what the platform attests about it.                                                     |
| [`ConversationRef`](#correspond.ConversationRef)(\*, channel[, id, kind, parent])   | Where a conversation lives: a channel and that channel's own id, encoded `<channel>:<id>`.                                                   |
| [`Draft`](#correspond.Draft)(\*, text[, title, reply_to, priority, ...])  | What to write: the text, and the few things channels share (a title, the message answered, a priority, copies).                              |
| [`Editor`](#correspond.Editor)(\*args, \*\*kwargs)                         | Replace the text of a message correspond's account wrote.                                                                                    |
| [`Event`](#correspond.Event)(\*, kind, channel, delivery_id[, ...])       | Something that happened on a channel, as a listener reports it: dedupe on `delivery_id`, resume from `cursor`.                               |
| [`Grade`](#correspond.Grade)(\*values)                                    | How sure the channel is about who sent a message: a vocabulary, not a ranking.                                                               |
| [`HistoryDepth`](#correspond.HistoryDepth)(\*values)                             | How far back `read` sees: all history, a 24-hour buffer, only what correspond has seen since it was linked or started listening, or nothing. |
| [`Labeler`](#correspond.Labeler)(\*args, \*\*kwargs)                        | Add labels to a conversation, apart from its opening send.                                                                                   |
| [`Listener`](#correspond.Listener)(\*args, \*\*kwargs)                       | Events since `cursor`, oldest first, each carrying the cursor to resume after it.                                                            |
| [`Message`](#correspond.Message)(\*, id, conversation, author, ...[, ...])  | One message in a conversation, normalised, with the channel's own fields kept in `native`.                                                   |
| [`Reactor`](#correspond.Reactor)(\*args, \*\*kwargs)                        | Add a reaction to a message.                                                                                                                 |
| [`Reader`](#correspond.Reader)(\*args, \*\*kwargs)                         | Messages of a conversation, oldest first; `limit` keeps the most recent.                                                                     |
| [`RouteDecision`](#correspond.RouteDecision)(\*, target, rule, reason)            | Where a message goes, which rule decided, and why.                                                                                           |
| [`Scope`](#correspond.Scope)(\*values)                                    | The widest class of reader a conversation can reach.                                                                                         |
| [`SendResult`](#correspond.SendResult)(\*, ok, channel, conversation[, ...])   | What a write did, or would do (`dry_run`), or why it failed and whether a retry could help.                                                  |
| [`Support`](#correspond.Support)(\*values)                                  | How well a channel does something: `full`, `partial` (with limits in `notes`), or `none`.                                                    |
| [`Unlabeler`](#correspond.Unlabeler)(\*args, \*\*kwargs)                      | Remove labels from a conversation.                                                                                                           |
| [`Uploader`](#correspond.Uploader)(\*args, \*\*kwargs)                       | Send a file to a conversation.                                                                                                               |
| [`Verifier`](#correspond.Verifier)(\*args, \*\*kwargs)                       | Grade an inbound delivery (a webhook, a posted report) from its headers and raw body.                                                        |
| [`Writer`](#correspond.Writer)(\*args, \*\*kwargs)                         | Send a draft to a conversation.                                                                                                              |

### Exceptions

| [`ChannelError`](#correspond.ChannelError)(message, \*, kind[, retryable, ...])   | A platform call that failed, classified so the caller can decide what to do.                 |
|------------------------------------------------------------------------------------------------------|----------------------------------------------------------------------------------------------|
| [`CorrespondError`](#correspond.CorrespondError)                                     | An expected failure, with a message meant for the person or agent that asked.                |
| [`InvalidRef`](#correspond.InvalidRef)                                          | A conversation reference that does not parse, or that its channel rejects.                   |
| [`MissingRequirement`](#correspond.MissingRequirement)(channel, missing, \*, fix)       | Something the channel needs is not here: a credential, a binary, an extra, an OS.            |
| [`NeedsApproval`](#correspond.NeedsApproval)(reason, \*\*details)                  | Draft to operator: the write waits for the operator's approval.                              |
| [`NotSupported`](#correspond.NotSupported)(operation, channel, \*[, ...])         | The channel does not have this operation (or this feature of it).                            |
| [`Refused`](#correspond.Refused)(reason, \*\*details)                        | Block: this draft does not go to this conversation as written.                               |
| [`Stopped`](#correspond.Stopped)(reason, \*\*details)                        | A write the `before_send` check did not let leave: `error_kind` says how, `reason` says why. |
| [`UnknownChannel`](#correspond.UnknownChannel)(channel, \*[, known, hint])          | A channel name that no registered adapter answers to.                                        |

### *class* correspond.Account(, channel, id, acts_as='user')

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

The credentialed endpoint correspond acts through: whose name a write goes out under.

#### *classmethod* from_dict(data)

The inverse of [`to_dict()`](#correspond.Account.to_dict).

* **Return type:**
  [`Account`](correspond.model.html.md#correspond.model.Account)

#### to_dict()

JSON-ready.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)

### *class* correspond.Attachment(, ref, media_type='application/octet-stream', name=None, size=None, sha256=None, loader=None)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

A file that came with a message: referenced by `ref`, fetched only on [`content()`](#correspond.Attachment.content), never inlined.

#### content()

The bytes, fetched now through the channel that produced the attachment.

* **Return type:**
  [`bytes`](https://docs.python.org/3/builtins/stdtypes.html#bytes)

#### *classmethod* from_dict(data)

The inverse of [`to_dict()`](#correspond.Attachment.to_dict) (the result has no loader).

* **Return type:**
  [`Attachment`](correspond.model.html.md#correspond.model.Attachment)

#### to_dict()

JSON-ready (without the loader).

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)

### *class* correspond.Audience(\*, ref, scope, readers=(), complete=False, classes=(), external=None, retractable=False, durability=(), widening=(), as_of=<factory>, evidence=(), defaulted=False)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

Who can read a conversation, now and plausibly later, as far as its channel can tell.

`scope` is the widest class of reader it can reach. `readers` are the channel
identities known to read it, a lower bound unless `complete`; `classes` name, in
words, the readers that cannot be listed. `external` says whether readers outside
the operator’s own accounts exist (`None`: unknown). `durability` is what a send
leaves behind (`DURABILITY`), `widening` how the readership can grow
(`WIDENING`). `evidence` records the calls made and why anything was assumed;
`defaulted` is true when an unknown resolved to `public`, which it always does.

Set-valued fields (`readers`, `classes`, `durability`, `widening`) are stored
deduplicated and sorted, so the same audience compares and hashes alike whatever
order a platform listed it in. A durability or widening flag this version does not
know is kept, never dropped. A public audience is never complete, retractable or free
of external readers, and a defaulted one is always public.

```pycon
>>> a = Audience(ref="github:example/app#12", scope="public", classes=["watchers and participants receive the body by email"],
...              durability=["indexed", "archived_by_others", "copies_pushed", "edit_history_visible"], as_of="2026-09-15T12:00:00Z")
>>> a.in_words()
'world-readable; emailed to watchers and participants; archived by others; edits keep a visible history; not retractable'
>>> Audience.from_dict(a.to_dict()) == a
True
```

#### *classmethod* from_dict(data)

The inverse of [`to_dict()`](#correspond.Audience.to_dict); unknown keys are ignored.

* **Return type:**
  [`Audience`](correspond.model.html.md#correspond.model.Audience)

#### *property* hash *: [str](https://docs.python.org/3/builtins/stdtypes.html#str)*

The SHA-256, in hex, of the canonical JSON of [`to_dict()`](#correspond.Audience.to_dict) without `AUDIENCE_UNHASHED`.

`data` is exactly what [`to_dict()`](#correspond.Audience.to_dict) returns, derived `readers[].address`
included. Canonical JSON is `json.dumps(data, sort_keys=True, separators=(",", ":"))`
(Python’s default ASCII escaping), encoded as UTF-8. Approvals bind to this value,
so the construction is a contract. A `to_dict` output can be hashed as it stands;
any other dict should go through `Audience.from_dict(d).hash`, which normalises.

#### in_words()

One line: the scope, the readers and reader classes, what a send leaves behind, whether it can be withdrawn.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

#### to_dict()

JSON-ready.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)

#### *classmethod* unknown(ref, \*reasons)

The audience nobody could compute: public, incomplete, every durability and widening flag, not retractable, defaulted, with `reasons` as evidence.

* **Return type:**
  [`Audience`](correspond.model.html.md#correspond.model.Audience)

### *class* correspond.AudienceReader(\*args, \*\*kwargs)

Bases: [`Protocol`](https://docs.python.org/3/library/typing.html#typing.Protocol)

Who can read a conversation, now and plausibly later, asked of the platform at the time of the call.

### *class* correspond.Authenticity(\*, grade, evidence=<factory>)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

A grade and its evidence, computed on correspond’s side of the hop and never read from a payload.

```pycon
>>> Authenticity(grade="bound", evidence={"method": "hmac-sha256"}).to_dict()
{'grade': 'bound', 'evidence': {'method': 'hmac-sha256'}}
```

#### *classmethod* from_dict(data)

The inverse of [`to_dict()`](#correspond.Authenticity.to_dict).

* **Return type:**
  [`Authenticity`](correspond.model.html.md#correspond.model.Authenticity)

#### to_dict()

JSON-ready.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)

### *class* correspond.Capabilities(, channel, read=Support.NONE, listen=Support.NONE, send=Support.NONE, edit=Support.NONE, react=Support.NONE, upload=Support.NONE, verify=Support.NONE, audience=Support.NONE, label=Support.NONE, unlabel=Support.NONE, initiate=Support.NONE, reply=Support.NONE, priority=Support.NONE, cc=Support.NONE, history_depth=HistoryDepth.NONE, listen_modes=(), grades=(), max_text_length=None, max_title_length=None, edit_max_age_s=None, reactions=(), reactions_per_message=None, max_upload_bytes=None, formats=('plain',), native_fields=None, rate_limits=(), notes=())

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

What a channel can do, graded, with its limits.

One `Support` per operation in `OPERATIONS` (`audience`: can the channel say
who reads a conversation; `label` / `unlabel`: can labels be added to or removed
from a conversation, apart from its opening send), plus four features of writing:
`initiate` (can a write start a conversation; Telegram bots cannot), `reply`
(can a draft answer a specific message), `priority`, and `cc` (can a draft copy
further recipients, `Draft.cc` and `Draft.bcc`). `history_depth` says how
far back `read` sees; `grades` are the authenticity grades the channel can attest;
`native_fields` are the keys its messages may carry in `native` (what a routing
condition can test), or `None` when the channel does not declare them.

#### *classmethod* from_dict(data)

The inverse of [`to_dict()`](#correspond.Capabilities.to_dict).

* **Return type:**
  [`Capabilities`](correspond.model.html.md#correspond.model.Capabilities)

#### *property* operations *: [tuple](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[str](https://docs.python.org/3/builtins/stdtypes.html#str), ...]*

The operations the channel has at all (full or partial).

#### supports(operation)

The support level for one of `OPERATIONS`.

* **Return type:**
  [`Support`](correspond.model.html.md#correspond.model.Support)

#### to_dict()

JSON-ready.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)

### *exception* correspond.ChannelError(message, , kind, retryable=False, retry_after=None)

Bases: [`CorrespondError`](correspond.errors.html.md#correspond.errors.CorrespondError)

A platform call that failed, classified so the caller can decide what to do.

### *class* correspond.ChannelIdentity(, channel, native_id, handle=None, display_name=None, is_bot=False, is_self=False, authority=None)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

Who a channel says sent something: its native id and what the platform attests about it. Not a person.

#### *property* address *: [str](https://docs.python.org/3/builtins/stdtypes.html#str)*

the form a people registry resolves.

```pycon
>>> ChannelIdentity(channel="github", native_id="583231", handle="octocat").address
'github:octocat'
```

* **Type:**
  `<channel>:<handle>`, or the native id when there is no handle

#### *classmethod* from_dict(data)

The inverse of [`to_dict()`](#correspond.ChannelIdentity.to_dict).

* **Return type:**
  [`ChannelIdentity`](correspond.model.html.md#correspond.model.ChannelIdentity)

#### label()

A short name for transcripts.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

#### to_dict()

JSON-ready.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)

### *class* correspond.ConversationRef(, channel, id='', kind='', parent=None)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

Where a conversation lives: a channel and that channel’s own id, encoded `<channel>:<id>`.

Two references are equal when channel and id are. `kind` (`issue`, `discussion`,
`chat`, `address`, …) and `parent` describe the conversation and are filled in by
the channel’s adapter, so a freshly parsed reference equals its normalised form.

```pycon
>>> ConversationRef.parse("telegram:-4001/7") == ConversationRef(channel="telegram", id="-4001/7", kind="topic")
True
>>> ConversationRef.parse("nonsense")
Traceback (most recent call last):
...
correspond.errors.InvalidRef: 'nonsense' is not a conversation reference: the form is <channel>:<id>, e.g. github:octocat/hello-world#1
```

#### *property* encoded *: [str](https://docs.python.org/3/builtins/stdtypes.html#str)*

The stable string form, `<channel>:<id>`.

#### *classmethod* from_dict(data)

The inverse of [`to_dict()`](#correspond.ConversationRef.to_dict); a bare encoded string also works.

* **Return type:**
  [`ConversationRef`](correspond.model.html.md#correspond.model.ConversationRef)

#### *classmethod* parse(text)

Split `<channel>:<id>` without consulting the channel (its adapter normalises the id).

* **Return type:**
  [`ConversationRef`](correspond.model.html.md#correspond.model.ConversationRef)

#### to_dict()

JSON-ready, with the encoded form under `ref`.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)

### *exception* correspond.CorrespondError

Bases: [`Exception`](https://docs.python.org/3/builtins/exceptions.html#Exception)

An expected failure, with a message meant for the person or agent that asked.

### *class* correspond.Draft(, text, title=None, reply_to=None, priority=None, cc=(), bcc=())

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

What to write: the text, and the few things channels share (a title, the message answered, a priority, copies).

`cc` and `bcc` are further recipients, on channels whose capabilities grade `cc`
(email). They count in the conversation’s audience.

```pycon
>>> Draft(text="hi", cc=["bob@example.org", " "]).cc
('bob@example.org',)
```

#### to_dict()

JSON-ready.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)

### *class* correspond.Editor(\*args, \*\*kwargs)

Bases: [`Protocol`](https://docs.python.org/3/library/typing.html#typing.Protocol)

Replace the text of a message correspond’s account wrote.

### *class* correspond.Event(\*, kind, channel, delivery_id, cursor=None, message=None, payload=<factory>)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

Something that happened on a channel, as a listener reports it: dedupe on `delivery_id`, resume from `cursor`.

#### *classmethod* from_dict(data)

The inverse of [`to_dict()`](#correspond.Event.to_dict).

* **Return type:**
  [`Event`](correspond.model.html.md#correspond.model.Event)

#### to_dict()

JSON-ready.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)

### *class* correspond.Grade(\*values)

Bases: [`StrEnum`](https://docs.python.org/3/library/enum.html#enum.StrEnum)

How sure the channel is about who sent a message: a vocabulary, not a ranking.

- `forged`: verification was attempted and failed.
- `claimed`: nothing verifiable (a typed name, an unauthenticated From header, anonymous input).
- `platform`: the platform authenticated the account (GitHub, Telegram).
- `domain`: email authenticated by *your own* receiving server’s Authentication-Results.
- `bound`: a host application signed an assertion about its logged-in user.
- `crypto`: a signed payload (a GitHub webhook’s `X-Hub-Signature-256`).

A policy names the grades it accepts for a permission. Comparing grades with `<`
raises, because `domain` is neither stronger nor weaker than `platform`.

```pycon
>>> Grade.CLAIMED < Grade.PLATFORM
Traceback (most recent call last):
...
TypeError: authenticity grades are not ranked; test membership in the grades a policy accepts
```

### *class* correspond.HistoryDepth(\*values)

Bases: [`StrEnum`](https://docs.python.org/3/library/enum.html#enum.StrEnum)

How far back `read` sees: all history, a 24-hour buffer, only what correspond has seen since it was linked or started listening, or nothing.

### *exception* correspond.InvalidRef

Bases: [`CorrespondError`](correspond.errors.html.md#correspond.errors.CorrespondError), [`ValueError`](https://docs.python.org/3/builtins/exceptions.html#ValueError)

A conversation reference that does not parse, or that its channel rejects.

### *class* correspond.Labeler(\*args, \*\*kwargs)

Bases: [`Protocol`](https://docs.python.org/3/library/typing.html#typing.Protocol)

Add labels to a conversation, apart from its opening send.

### *class* correspond.Listener(\*args, \*\*kwargs)

Bases: [`Protocol`](https://docs.python.org/3/library/typing.html#typing.Protocol)

Events since `cursor`, oldest first, each carrying the cursor to resume after it.

### *class* correspond.Message(\*, id, conversation, author, authenticity, sent_at, text, body=None, body_format='plain', attachments=(), reply_to=None, thread_root=None, edited_at=None, url=None, native=<factory>, raw=None)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

One message in a conversation, normalised, with the channel’s own fields kept in `native`.

#### *classmethod* from_dict(data)

The inverse of [`to_dict()`](#correspond.Message.to_dict).

* **Return type:**
  [`Message`](correspond.model.html.md#correspond.model.Message)

#### to_dict(, include_raw=False)

JSON-ready; `raw` (the untouched payload) only when asked for.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)

### *exception* correspond.MissingRequirement(channel, missing, , fix, kind='unavailable')

Bases: [`ChannelError`](correspond.errors.html.md#correspond.errors.ChannelError)

Something the channel needs is not here: a credential, a binary, an extra, an OS.

### *exception* correspond.NeedsApproval(reason, \*\*details)

Bases: [`Stopped`](correspond.errors.html.md#correspond.errors.Stopped)

Draft to operator: the write waits for the operator’s approval. Raised by a `before_send` check.

### *exception* correspond.NotSupported(operation, channel, , alternatives=())

Bases: [`CorrespondError`](correspond.errors.html.md#correspond.errors.CorrespondError)

The channel does not have this operation (or this feature of it). Never a silent no-op.

### *class* correspond.Reactor(\*args, \*\*kwargs)

Bases: [`Protocol`](https://docs.python.org/3/library/typing.html#typing.Protocol)

Add a reaction to a message.

### *class* correspond.Reader(\*args, \*\*kwargs)

Bases: [`Protocol`](https://docs.python.org/3/library/typing.html#typing.Protocol)

Messages of a conversation, oldest first; `limit` keeps the most recent.

### *exception* correspond.Refused(reason, \*\*details)

Bases: [`Stopped`](correspond.errors.html.md#correspond.errors.Stopped)

Block: this draft does not go to this conversation as written. Raised by a `before_send` check.

### *class* correspond.RouteDecision(, target, rule, reason)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

Where a message goes, which rule decided, and why.

#### to_dict()

JSON-ready.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)

### *class* correspond.Scope(\*values)

Bases: [`StrEnum`](https://docs.python.org/3/library/enum.html#enum.StrEnum)

The widest class of reader a conversation can reach.

- `operator`: only the operator’s own devices (a notification banner).
- `named`: exactly the explicit recipients (an email, a user’s private repository).
- `group`: a bounded membership (a Telegram group).
- `org`: an organisation or workspace (a private organisation repository).
- `public`: anyone. What an unknown audience resolves to.

### *class* correspond.SendResult(\*, ok, channel, conversation, operation='send', dry_run=False, message_id=None, url=None, account=None, plan=<factory>, error=None, error_kind=None, retryable=False, retry_after=None)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

What a write did, or would do (`dry_run`), or why it failed and whether a retry could help.

#### *classmethod* failure(error, , channel, conversation, operation='send', dry_run=False, plan=None)

A result carrying a [`ChannelError`](correspond.errors.html.md#correspond.errors.ChannelError)’s classification.

* **Return type:**
  [`SendResult`](correspond.model.html.md#correspond.model.SendResult)

#### *classmethod* from_dict(data)

The inverse of [`to_dict()`](#correspond.SendResult.to_dict).

* **Return type:**
  [`SendResult`](correspond.model.html.md#correspond.model.SendResult)

#### to_dict()

JSON-ready.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)

### *exception* correspond.Stopped(reason, \*\*details)

Bases: [`CorrespondError`](correspond.errors.html.md#correspond.errors.CorrespondError)

A write the `before_send` check did not let leave: `error_kind` says how, `reason` says why.

Keyword `details` (an approval id, a draft hash) travel with the result, in the
plan’s `before_send_details`.

### *class* correspond.Support(\*values)

Bases: [`StrEnum`](https://docs.python.org/3/library/enum.html#enum.StrEnum)

How well a channel does something: `full`, `partial` (with limits in `notes`), or `none`.

### *exception* correspond.UnknownChannel(channel, , known=(), hint='')

Bases: [`CorrespondError`](correspond.errors.html.md#correspond.errors.CorrespondError), [`LookupError`](https://docs.python.org/3/builtins/exceptions.html#LookupError)

A channel name that no registered adapter answers to.

### *class* correspond.Unlabeler(\*args, \*\*kwargs)

Bases: [`Protocol`](https://docs.python.org/3/library/typing.html#typing.Protocol)

Remove labels from a conversation.

### *class* correspond.Uploader(\*args, \*\*kwargs)

Bases: [`Protocol`](https://docs.python.org/3/library/typing.html#typing.Protocol)

Send a file to a conversation.

### *class* correspond.Verifier(\*args, \*\*kwargs)

Bases: [`Protocol`](https://docs.python.org/3/library/typing.html#typing.Protocol)

Grade an inbound delivery (a webhook, a posted report) from its headers and raw body.

### *class* correspond.Writer(\*args, \*\*kwargs)

Bases: [`Protocol`](https://docs.python.org/3/library/typing.html#typing.Protocol)

Send a draft to a conversation.

### correspond.audience(ref, draft=None, , registry=None)

Who can read a conversation, asked of its channel now. Never raises: unknown resolves to public.

A malformed reference, an unknown or planned channel, an adapter without an audience
reader, and any error while computing all give `scope="public"`, `complete=False`,
`defaulted=True`, with the reason in `evidence`. Nothing is cached: call it again
right before sending.

* **Return type:**
  [`Audience`](correspond.model.html.md#correspond.model.Audience)

### correspond.capabilities(channel, , registry=None)

What a channel can do, graded, with its limits.

* **Return type:**
  [`Capabilities`](correspond.model.html.md#correspond.model.Capabilities)

### correspond.channel_registry()

The process registry: built on first use, then shared.

* **Return type:**
  [`MutableMapping`](https://docs.python.org/3/library/collections.abc.html#collections.abc.MutableMapping)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`Any`](https://docs.python.org/3/library/typing.html#typing.Any)]

### correspond.check_binding(pattern, , registry=None)

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

### correspond.check_requirements(channel, \*, registry=None, config=None, run=<function run>)

What a channel needs and what is missing: the install command, binaries, platform, and every setting’s source. Never a secret’s value.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)

### correspond.edit(ref, message_id, text, , dry_run=False, registry=None, before_send=None)

Replace the text of a message correspond’s account wrote; the `before_send` check runs first, as for [`send()`](#correspond.send).

* **Return type:**
  [`SendResult`](correspond.model.html.md#correspond.model.SendResult)

### correspond.get_channel(name, , registry=None)

The adapter registered under `name`; [`UnknownChannel`](correspond.errors.html.md#correspond.errors.UnknownChannel) says what to do if there is none.

* **Return type:**
  [`Any`](https://docs.python.org/3/library/typing.html#typing.Any)

### correspond.label(ref, labels, , dry_run=False, registry=None, before_send=None)

Add `labels` to a conversation; the `before_send` check sees them, comma-joined, as the draft’s text.

* **Return type:**
  [`SendResult`](correspond.model.html.md#correspond.model.SendResult)

### correspond.listen(ref, , cursors=None, limit=None, commit=True, registry=None)

Events since the cursor stored for `ref`; each cursor is stored once the consumer moves past its event.

`cursors` defaults to files under the data root. Events can repeat after a crash or an
early `break`: deduplicate on `delivery_id`.

* **Return type:**
  [`Iterator`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Iterator)[[`Event`](correspond.model.html.md#correspond.model.Event)]

### correspond.metadata_rule(target, , name=None, \*\*conditions)

A rule sending messages to `target` when every `field=glob` condition holds.

* **Return type:**
  [`Callable`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Callable)[[[`Message`](correspond.model.html.md#correspond.model.Message)], [`str`](https://docs.python.org/3/builtins/stdtypes.html#str) | [`None`](https://docs.python.org/3/builtins/constants.html#None)]

```pycon
>>> from correspond.testing import demo_message
>>> rule = metadata_rule("urgent-queue", labels="priority:high")
>>> rule(demo_message(labels=["priority:high"])), rule(demo_message())
('urgent-queue', None)
```

### correspond.parse_ref(ref, , registry=None)

A reference normalised by its channel’s adapter (kind and parent filled in, the id validated).

* **Return type:**
  [`ConversationRef`](correspond.model.html.md#correspond.model.ConversationRef)

### correspond.react(ref, message_id, reaction, , dry_run=False, registry=None, before_send=None)

Add a reaction to a message (the channel’s capabilities list the reactions it accepts); the `before_send` check sees the reaction as the draft’s text.

* **Return type:**
  [`SendResult`](correspond.model.html.md#correspond.model.SendResult)

### correspond.read(ref, , since=None, limit=None, registry=None)

The messages of a conversation, oldest first (`limit` keeps the most recent ones).

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`Message`](correspond.model.html.md#correspond.model.Message)]

### correspond.register_channel(adapter, , name=None, replace=False, registry=None)

Add an adapter (tests, or a channel defined outside correspond). `replace` swaps out an existing one.

* **Return type:**
  [`Any`](https://docs.python.org/3/library/typing.html#typing.Any)

### correspond.route(message, , bindings=None, threads=None, rules=(), classifier=None)

Run the chain (bindings, thread continuity, metadata rules, classifier) and return the first decision, or `None`.

* **Return type:**
  [`RouteDecision`](correspond.routing.html.md#correspond.routing.RouteDecision) | [`None`](https://docs.python.org/3/builtins/constants.html#None)

### correspond.send(ref, text, , title=None, reply_to=None, priority=None, cc=(), bcc=(), dry_run=False, registry=None, before_send=None, idempotency_key=None, sends=None)

Send `text` (or a [`Draft`](correspond.model.html.md#correspond.model.Draft)) to a conversation; `dry_run` shows the plan and sends nothing.

`cc` and `bcc` copy further recipients, on channels that grade `cc` (email).
`before_send(ref, draft, audience)` runs first, on the dry run too; when `None`, the
config’s `before_send` reference, else [`correspond.outbound.notice()`](correspond.outbound.html.md#correspond.outbound.notice).

`idempotency_key` makes sending the same message again safe: a key that already sent
answers that send’s result and posts nothing, and one whose earlier attempt may have
gone out is confirmed by reading the conversation back, or refused as `unconfirmed`
([`correspond.idempotency`](correspond.idempotency.html.md#module-correspond.idempotency)). `sends` is where keys are kept: by default files
under the data root ([`correspond.stores.send_store()`](correspond.stores.html.md#correspond.stores.send_store)). A mapping with a
`claim(key, record) -> bool` (as that store has) is exclusive across processes; a
plain one is checked, then set, which covers one process.

* **Return type:**
  [`SendResult`](correspond.model.html.md#correspond.model.SendResult)

### correspond.unlabel(ref, labels, , dry_run=False, registry=None, before_send=None)

Remove `labels` from a conversation; the `before_send` check sees them, comma-joined, as the draft’s text.

* **Return type:**
  [`SendResult`](correspond.model.html.md#correspond.model.SendResult)

### correspond.unregister_channel(name, , registry=None)

Remove a channel from the registry.

* **Return type:**
  [`None`](https://docs.python.org/3/builtins/constants.html#None)

### correspond.upload(ref, name, data, , media_type='application/octet-stream', dry_run=False, registry=None, before_send=None)

Send a file to a conversation. The `before_send` check sees only the file name as the draft’s text (`operation="upload"`): a check that cannot vet the bytes should hold or refuse.

* **Return type:**
  [`SendResult`](correspond.model.html.md#correspond.model.SendResult)

### correspond.verify(channel, headers, body, , registry=None)

Grade an inbound delivery on `channel` from its headers and raw body.

* **Return type:**
  [`Authenticity`](correspond.model.html.md#correspond.model.Authenticity)

### Modules

| [`channels`](correspond.channels.html.md#module-correspond.channels)       | The built-in channel adapters, one module each.                                                                |
|--------------------------------------------------------------------------------------------|----------------------------------------------------------------------------------------------------------------|
| [`errors`](correspond.errors.html.md#module-correspond.errors)           | What correspond raises, and the vocabulary a failed write reports.                                             |
| [`idempotency`](correspond.idempotency.html.md#module-correspond.idempotency) | Idempotent sends: an `idempotency_key` keeps a message from going out twice.                                   |
| [`mcp`](correspond.mcp.html.md#module-correspond.mcp)                 | MCP over stdio: the same tools, for Claude Desktop and other local MCP clients.                                |
| [`model`](correspond.model.html.md#module-correspond.model)             | The data model every channel is described in.                                                                  |
| [`ops`](correspond.ops.html.md#module-correspond.ops)                 | The operations: small protocols an adapter implements a subset of, and the verbs that call them.               |
| [`outbound`](correspond.outbound.html.md#module-correspond.outbound)       | The `before_send` check: what every write runs, with the conversation's audience, before anything leaves.      |
| [`registry`](correspond.registry.html.md#module-correspond.registry)       | Which channels exist: the built-in channel table, the registry built from it, and what each channel needs.     |
| [`render`](correspond.render.html.md#module-correspond.render)           | Turning a tool's result into terminal output: `(stdout, stderr, exit code)`.                                   |
| [`routing`](correspond.routing.html.md#module-correspond.routing)         | Deciding what a message is about: a transparent rule chain.                                                    |
| [`settings`](correspond.settings.html.md#module-correspond.settings)       | Where correspond keeps state and reads configuration, and how it finds a secret.                               |
| [`stores`](correspond.stores.html.md#module-correspond.stores)           | The state stores: listen cursors, the web inbox's reports and blobs, the Telegram log.                         |
| [`testing`](correspond.testing.html.md#module-correspond.testing)         | An in-memory channel for tests and rehearsals, and `python -m correspond.testing`: the CLI with it registered. |
| [`tools`](correspond.tools.html.md#module-correspond.tools)             | The single source of truth for every surface: plain functions, flat arguments in, JSON-ready dicts out.        |
