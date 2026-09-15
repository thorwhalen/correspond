# correspond.model

The data model every channel is described in.

Accounts, conversation references, channel identities, authenticity, messages,
attachments, events, capabilities, audiences, drafts and send results: frozen dataclasses
that round-trip through JSON-ready dicts (`to_dict` / `from_dict`), because every
surface (the CLI, MCP, a ledger on disk) moves them as JSON.

There is no person here. A [`ChannelIdentity`](#correspond.model.ChannelIdentity) is who the platform says sent a
message and [`Authenticity`](#correspond.model.Authenticity) is how sure the channel is. Linking an identity to a
person is a job for a people registry, with its own evidence. An [`Audience`](#correspond.model.Audience) lists
readers the same way: as channel identities, never as people.

```pycon
>>> ref = ConversationRef.parse("github:octocat/hello-world#1")
>>> ref.channel, ref.id, str(ref)
('github', 'octocat/hello-world#1', 'github:octocat/hello-world#1')
>>> ConversationRef.from_dict(ref.to_dict()) == ref
True
>>> Grade("bound") in {Grade.PLATFORM, Grade.BOUND, Grade.CRYPTO}
True
```

### Module Attributes

| [`OPERATIONS`](#correspond.model.OPERATIONS)        | The operations, in the order capabilities and surfaces list them.                                                                                            |
|--------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------|
| [`ACTS_AS`](#correspond.model.ACTS_AS)           | Whose name a write goes out under.                                                                                                                           |
| [`EVENT_KINDS`](#correspond.model.EVENT_KINDS)       | What an [`Event`](#correspond.model.Event) reports.                                                                                      |
| [`PRIORITIES`](#correspond.model.PRIORITIES)        | The priorities a draft may ask for; channels without priorities refuse anything but `None`.                                                                  |
| [`DURABILITY`](#correspond.model.DURABILITY)        | searchable, archived by third parties, copies delivered to readers (email notifications), an edit history anyone who reads can see.                          |
| [`WIDENING`](#correspond.model.WIDENING)          | How a readership can grow after a send.                                                                                                                      |
| [`CLASS_WORDS`](#correspond.model.CLASS_WORDS)       | Short phrases for reader classes, used by [`Audience.in_words()`](#correspond.model.Audience.in_words); a class not listed here is shown as written. |
| [`AUDIENCE_UNHASHED`](#correspond.model.AUDIENCE_UNHASHED) | they differ between two computations of the same audience.                                                                                                   |

### Functions

| [`format_time`](#correspond.model.format_time)(value)   | ISO 8601 in UTC with a `Z` suffix; a naive datetime is taken to be UTC.                                            |
|-----------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------|
| [`parse_time`](#correspond.model.parse_time)(value)    | The inverse of [`format_time()`](#correspond.model.format_time); also accepts offsets and datetimes. |

### Classes

| [`Account`](#correspond.model.Account)(\*, channel, id[, acts_as])                | The credentialed endpoint correspond acts through: whose name a write goes out under.                                                        |
|-----------------------------------------------------------------------------------------------------|----------------------------------------------------------------------------------------------------------------------------------------------|
| [`Attachment`](#correspond.model.Attachment)(\*, ref[, media_type, name, size, ...]) | A file that came with a message: referenced by `ref`, fetched only on `content()`, never inlined.                                            |
| [`Audience`](#correspond.model.Audience)(\*, ref, scope[, readers, complete, ...]) | Who can read a conversation, now and plausibly later, as far as its channel can tell.                                                        |
| [`Authenticity`](#correspond.model.Authenticity)(\*, grade[, evidence])                | A grade and its evidence, computed on correspond's side of the hop and never read from a payload.                                            |
| [`Capabilities`](#correspond.model.Capabilities)(\*, channel[, read, listen, ...])     | What a channel can do, graded, with its limits.                                                                                              |
| [`ChannelIdentity`](#correspond.model.ChannelIdentity)(\*, channel, native_id[, ...])     | Who a channel says sent something: its native id and what the platform attests about it.                                                     |
| [`ConversationRef`](#correspond.model.ConversationRef)(\*, channel[, id, kind, parent])   | Where a conversation lives: a channel and that channel's own id, encoded `<channel>:<id>`.                                                   |
| [`Draft`](#correspond.model.Draft)(\*, text[, title, reply_to, priority])       | What to write: the text, and the few things channels share (a title, the message answered, a priority).                                      |
| [`Event`](#correspond.model.Event)(\*, kind, channel, delivery_id[, ...])       | Something that happened on a channel, as a listener reports it: dedupe on `delivery_id`, resume from `cursor`.                               |
| [`Grade`](#correspond.model.Grade)(\*values)                                    | How sure the channel is about who sent a message: a vocabulary, not a ranking.                                                               |
| [`HistoryDepth`](#correspond.model.HistoryDepth)(\*values)                             | How far back `read` sees: all history, a 24-hour buffer, only what correspond has seen since it was linked or started listening, or nothing. |
| [`Message`](#correspond.model.Message)(\*, id, conversation, author, ...[, ...])  | One message in a conversation, normalised, with the channel's own fields kept in `native`.                                                   |
| [`Scope`](#correspond.model.Scope)(\*values)                                    | The widest class of reader a conversation can reach.                                                                                         |
| [`SendResult`](#correspond.model.SendResult)(\*, ok, channel, conversation[, ...])   | What a write did, or would do (`dry_run`), or why it failed and whether a retry could help.                                                  |
| [`Support`](#correspond.model.Support)(\*values)                                  | How well a channel does something: `full`, `partial` (with limits in `notes`), or `none`.                                                    |

### correspond.model.ACTS_AS *= ('bot', 'user', 'app', 'service')*

Whose name a write goes out under.

### correspond.model.AUDIENCE_UNHASHED *= ('as_of', 'evidence')*

they differ between two computations of
the same audience.

* **Type:**
  The fields [`Audience.hash`](#correspond.model.Audience.hash) leaves out

### *class* correspond.model.Account(, channel, id, acts_as='user')

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

The credentialed endpoint correspond acts through: whose name a write goes out under.

#### *classmethod* from_dict(data)

The inverse of [`to_dict()`](#correspond.model.Account.to_dict).

* **Return type:**
  [`Account`](#correspond.model.Account)

#### to_dict()

JSON-ready.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)

### *class* correspond.model.Attachment(, ref, media_type='application/octet-stream', name=None, size=None, sha256=None, loader=None)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

A file that came with a message: referenced by `ref`, fetched only on [`content()`](#correspond.model.Attachment.content), never inlined.

#### content()

The bytes, fetched now through the channel that produced the attachment.

* **Return type:**
  [`bytes`](https://docs.python.org/3/builtins/stdtypes.html#bytes)

#### *classmethod* from_dict(data)

The inverse of [`to_dict()`](#correspond.model.Attachment.to_dict) (the result has no loader).

* **Return type:**
  [`Attachment`](#correspond.model.Attachment)

#### to_dict()

JSON-ready (without the loader).

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)

### *class* correspond.model.Audience(\*, ref, scope, readers=(), complete=False, classes=(), external=None, retractable=False, durability=(), widening=(), as_of=<factory>, evidence=(), defaulted=False)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

Who can read a conversation, now and plausibly later, as far as its channel can tell.

`scope` is the widest class of reader it can reach. `readers` are the channel
identities known to read it, a lower bound unless `complete`; `classes` name, in
words, the readers that cannot be listed. `external` says whether readers outside
the operator’s own accounts exist (`None`: unknown). `durability` is what a send
leaves behind ([`DURABILITY`](#correspond.model.DURABILITY)), `widening` how the readership can grow
([`WIDENING`](#correspond.model.WIDENING)). `evidence` records the calls made and why anything was assumed;
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

The inverse of [`to_dict()`](#correspond.model.Audience.to_dict); unknown keys are ignored.

* **Return type:**
  [`Audience`](#correspond.model.Audience)

#### *property* hash *: [str](https://docs.python.org/3/builtins/stdtypes.html#str)*

The SHA-256, in hex, of the canonical JSON of [`to_dict()`](#correspond.model.Audience.to_dict) without [`AUDIENCE_UNHASHED`](#correspond.model.AUDIENCE_UNHASHED).

`data` is exactly what [`to_dict()`](#correspond.model.Audience.to_dict) returns, derived `readers[].address`
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
  [`Audience`](#correspond.model.Audience)

### *class* correspond.model.Authenticity(\*, grade, evidence=<factory>)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

A grade and its evidence, computed on correspond’s side of the hop and never read from a payload.

```pycon
>>> Authenticity(grade="bound", evidence={"method": "hmac-sha256"}).to_dict()
{'grade': 'bound', 'evidence': {'method': 'hmac-sha256'}}
```

#### *classmethod* from_dict(data)

The inverse of [`to_dict()`](#correspond.model.Authenticity.to_dict).

* **Return type:**
  [`Authenticity`](#correspond.model.Authenticity)

#### to_dict()

JSON-ready.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)

### correspond.model.CLASS_WORDS *= {'watchers and participants receive the body by email': 'emailed to watchers and participants'}*

Short phrases for reader classes, used by [`Audience.in_words()`](#correspond.model.Audience.in_words); a class not listed
here is shown as written.

### *class* correspond.model.Capabilities(, channel, read=Support.NONE, listen=Support.NONE, send=Support.NONE, edit=Support.NONE, react=Support.NONE, upload=Support.NONE, verify=Support.NONE, audience=Support.NONE, initiate=Support.NONE, reply=Support.NONE, priority=Support.NONE, history_depth=HistoryDepth.NONE, listen_modes=(), grades=(), max_text_length=None, max_title_length=None, edit_max_age_s=None, reactions=(), reactions_per_message=None, max_upload_bytes=None, formats=('plain',), native_fields=None, rate_limits=(), notes=())

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

What a channel can do, graded, with its limits.

One `Support` per operation in [`OPERATIONS`](#correspond.model.OPERATIONS) (`audience`: can the channel say
who reads a conversation), plus three features of writing:
`initiate` (can a write start a conversation; Telegram bots cannot), `reply`
(can a draft answer a specific message) and `priority`. `history_depth` says how
far back `read` sees; `grades` are the authenticity grades the channel can attest;
`native_fields` are the keys its messages may carry in `native` (what a routing
condition can test), or `None` when the channel does not declare them.

#### *classmethod* from_dict(data)

The inverse of [`to_dict()`](#correspond.model.Capabilities.to_dict).

* **Return type:**
  [`Capabilities`](#correspond.model.Capabilities)

#### *property* operations *: [tuple](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[str](https://docs.python.org/3/builtins/stdtypes.html#str), ...]*

The operations the channel has at all (full or partial).

#### supports(operation)

The support level for one of [`OPERATIONS`](#correspond.model.OPERATIONS).

* **Return type:**
  [`Support`](#correspond.model.Support)

#### to_dict()

JSON-ready.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)

### *class* correspond.model.ChannelIdentity(, channel, native_id, handle=None, display_name=None, is_bot=False, is_self=False, authority=None)

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

The inverse of [`to_dict()`](#correspond.model.ChannelIdentity.to_dict).

* **Return type:**
  [`ChannelIdentity`](#correspond.model.ChannelIdentity)

#### label()

A short name for transcripts.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

#### to_dict()

JSON-ready.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)

### *class* correspond.model.ConversationRef(, channel, id='', kind='', parent=None)

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

The inverse of [`to_dict()`](#correspond.model.ConversationRef.to_dict); a bare encoded string also works.

* **Return type:**
  [`ConversationRef`](#correspond.model.ConversationRef)

#### *classmethod* parse(text)

Split `<channel>:<id>` without consulting the channel (its adapter normalises the id).

* **Return type:**
  [`ConversationRef`](#correspond.model.ConversationRef)

#### to_dict()

JSON-ready, with the encoded form under `ref`.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)

### correspond.model.DURABILITY *= ('indexed', 'archived_by_others', 'copies_pushed', 'edit_history_visible')*

searchable, archived by third parties, copies delivered to
readers (email notifications), an edit history anyone who reads can see.

* **Type:**
  What a send leaves behind

### *class* correspond.model.Draft(, text, title=None, reply_to=None, priority=None)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

What to write: the text, and the few things channels share (a title, the message answered, a priority).

#### to_dict()

JSON-ready.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)

### correspond.model.EVENT_KINDS *= ('message.created', 'message.updated', 'reaction.added')*

What an [`Event`](#correspond.model.Event) reports.

### *class* correspond.model.Event(\*, kind, channel, delivery_id, cursor=None, message=None, payload=<factory>)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

Something that happened on a channel, as a listener reports it: dedupe on `delivery_id`, resume from `cursor`.

#### *classmethod* from_dict(data)

The inverse of [`to_dict()`](#correspond.model.Event.to_dict).

* **Return type:**
  [`Event`](#correspond.model.Event)

#### to_dict()

JSON-ready.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)

### *class* correspond.model.Grade(\*values)

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

### *class* correspond.model.HistoryDepth(\*values)

Bases: [`StrEnum`](https://docs.python.org/3/library/enum.html#enum.StrEnum)

How far back `read` sees: all history, a 24-hour buffer, only what correspond has seen since it was linked or started listening, or nothing.

### *class* correspond.model.Message(\*, id, conversation, author, authenticity, sent_at, text, body=None, body_format='plain', attachments=(), reply_to=None, thread_root=None, edited_at=None, url=None, native=<factory>, raw=None)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

One message in a conversation, normalised, with the channel’s own fields kept in `native`.

#### *classmethod* from_dict(data)

The inverse of [`to_dict()`](#correspond.model.Message.to_dict).

* **Return type:**
  [`Message`](#correspond.model.Message)

#### to_dict(, include_raw=False)

JSON-ready; `raw` (the untouched payload) only when asked for.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)

### correspond.model.OPERATIONS *= ('read', 'listen', 'send', 'edit', 'react', 'upload', 'verify', 'audience')*

The operations, in the order capabilities and surfaces list them.

### correspond.model.PRIORITIES *= ('low', 'normal', 'high', 'urgent')*

The priorities a draft may ask for; channels without priorities refuse anything but `None`.

### *class* correspond.model.Scope(\*values)

Bases: [`StrEnum`](https://docs.python.org/3/library/enum.html#enum.StrEnum)

The widest class of reader a conversation can reach.

- `operator`: only the operator’s own devices (a notification banner).
- `named`: exactly the explicit recipients (an email, a user’s private repository).
- `group`: a bounded membership (a Telegram group).
- `org`: an organisation or workspace (a private organisation repository).
- `public`: anyone. What an unknown audience resolves to.

### *class* correspond.model.SendResult(\*, ok, channel, conversation, operation='send', dry_run=False, message_id=None, url=None, account=None, plan=<factory>, error=None, error_kind=None, retryable=False, retry_after=None)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

What a write did, or would do (`dry_run`), or why it failed and whether a retry could help.

#### *classmethod* failure(error, , channel, conversation, operation='send', dry_run=False, plan=None)

A result carrying a [`ChannelError`](correspond.errors.html.md#correspond.errors.ChannelError)’s classification.

* **Return type:**
  [`SendResult`](#correspond.model.SendResult)

#### *classmethod* from_dict(data)

The inverse of [`to_dict()`](#correspond.model.SendResult.to_dict).

* **Return type:**
  [`SendResult`](#correspond.model.SendResult)

#### to_dict()

JSON-ready.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)

### *class* correspond.model.Support(\*values)

Bases: [`StrEnum`](https://docs.python.org/3/library/enum.html#enum.StrEnum)

How well a channel does something: `full`, `partial` (with limits in `notes`), or `none`.

### correspond.model.WIDENING *= ('visibility_flip', 'joiners_read_history', 'forwarding', 'forks', 'list_expansion')*

How a readership can grow after a send.

### correspond.model.format_time(value)

ISO 8601 in UTC with a `Z` suffix; a naive datetime is taken to be UTC.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str) | [`None`](https://docs.python.org/3/builtins/constants.html#None)

```pycon
>>> format_time(datetime(2026, 9, 11, 12, 30, tzinfo=timezone.utc))
'2026-09-11T12:30:00Z'
```

### correspond.model.parse_time(value)

The inverse of [`format_time()`](#correspond.model.format_time); also accepts offsets and datetimes. Naive means UTC.

* **Return type:**
  [`datetime`](https://docs.python.org/3/library/datetime.html#datetime.datetime) | [`None`](https://docs.python.org/3/builtins/constants.html#None)

```pycon
>>> parse_time("2026-09-11T12:30:00Z") == datetime(2026, 9, 11, 12, 30, tzinfo=timezone.utc)
True
```
