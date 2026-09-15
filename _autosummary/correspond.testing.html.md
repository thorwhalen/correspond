# correspond.testing

An in-memory channel for tests and rehearsals, and `python -m correspond.testing`: the CLI with it registered.

[`FakeChannel`](#correspond.testing.FakeChannel) reads the conversations it was seeded with and records what it is
asked to send. It implements `Reader` and `Writer` only, so every other operation
raises `NotSupported`, exactly as a real channel without that operation does. Nothing it
does leaves the process, which makes it the channel to rehearse a workflow on:

```default
python -m correspond.testing read fake:example/demo
python -m correspond.testing send fake:example/demo "hello" --dry-run
```

```pycon
>>> fake = demo_channel()
>>> [m.text for m in fake.read(fake.parse_ref("example/demo"))]
['The export drops the last row.', 'Seen it too, on the CSV export.']
```

### Functions

| [`demo_channel`](#correspond.testing.demo_channel)([name])                        | A [`FakeChannel`](#correspond.testing.FakeChannel) seeded with one short conversation, `example/demo`.   |
|----------------------------------------------------------------------------------------------|----------------------------------------------------------------------------------------------------------------------|
| [`demo_message`](#correspond.testing.demo_message)(\*[, conversation, text, ...]) | A fictional message, for examples and tests.                                                                         |
| [`main`](#correspond.testing.main)([argv])                                | The ordinary `correspond` CLI, with the demo fake channel registered as `fake`.                                      |

### Classes

| [`FakeChannel`](#correspond.testing.FakeChannel)([name, conversations])   | A channel held in memory: seeded conversations to read, and a record of sends (`sent`).   |
|---------------------------------------------------------------------------------------|-------------------------------------------------------------------------------------------|

### *class* correspond.testing.FakeChannel(name='fake', , conversations=None)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

A channel held in memory: seeded conversations to read, and a record of sends (`sent`).

#### *property* capabilities *: [Capabilities](correspond.model.html.md#correspond.model.Capabilities)*

Read and send, with a small text limit; nothing else.

#### parse_ref(id)

Any non-empty id of letters, digits and `._/#-`.

* **Return type:**
  [`ConversationRef`](correspond.model.html.md#correspond.model.ConversationRef)

#### read(ref, , since=None, limit=None)

The seeded (and sent) messages of a conversation.

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`Message`](correspond.model.html.md#correspond.model.Message)]

#### send(ref, draft, , dry_run=False)

Append to the conversation (or, with `dry_run`, say that it would).

* **Return type:**
  [`SendResult`](correspond.model.html.md#correspond.model.SendResult)

### correspond.testing.demo_channel(name='fake')

A [`FakeChannel`](#correspond.testing.FakeChannel) seeded with one short conversation, `example/demo`.

* **Return type:**
  [`FakeChannel`](#correspond.testing.FakeChannel)

### correspond.testing.demo_message(, conversation='fake:example/demo', text='The export drops the last row.', message_id='m1', handle='ada', grade=Grade.PLATFORM, labels=(), is_self=False, reply_to=None, sent_at=datetime.datetime(2026, 9, 11, 9, 0, tzinfo=datetime.timezone.utc))

A fictional message, for examples and tests.

* **Return type:**
  [`Message`](correspond.model.html.md#correspond.model.Message)

### correspond.testing.main(argv=None)

The ordinary `correspond` CLI, with the demo fake channel registered as `fake`.

* **Return type:**
  [`None`](https://docs.python.org/3/builtins/constants.html#None)
