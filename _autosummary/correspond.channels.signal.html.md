# correspond.channels.signal

Signal through `signal-cli-rest-api` (JSON-RPC mode): listen, read what was logged, send.

### References

`signal:` (the linked account’s receive stream, for `listen`),
`signal:+<number>` (a person, by international phone number or Signal uuid) and
`signal:group.<id>` (a group, by the REST API’s group id).

Signal has no bot API. `signal-cli` runs as a *linked device* (or a registered number) of
an account you control, and the REST wrapper exposes it: `POST /v2/send` to send,
`GET /v1/receive/<number>` (a WebSocket in JSON-RPC mode) to receive. The WebSocket client
is the optional `websocket-client` (extra `signal`); tests and other transports pass
`ws=`.

Three facts shape the adapter:

- There is no history before linking: `read` returns only what `listen` and `send`
  logged on this machine (`history_depth = since_link`).
- `signal-cli` releases older than about three months stop working against Signal’s
  servers, so a silent channel may just be an old release (`requirements signal` says so).
- Whether to trust a changed safety number is an authenticity policy of the server
  (`--trust-new-identities`). `listen` compares the account’s known identities on every
  poll and reports a changed safety number as an `identity.changed` event.

```pycon
>>> Signal().parse_ref("group.ZXhhbXBsZQ==").kind
'group'
```

### Functions

| [`requirements_problems`](#correspond.channels.signal.requirements_problems)()            | What stops Signal working from this configuration; the signal-cli age caveat is a note, not a problem.   |
|-------------------------------------------------------------------------------------|----------------------------------------------------------------------------------------------------------|
| [`websocket_frames`](#correspond.channels.signal.websocket_frames)(url, \*, timeout) | Text frames of the WebSocket at `url` until it has been quiet for `timeout` seconds (the default `ws=`). |

### Classes

| [`Signal`](#correspond.channels.signal.Signal)(\*[, http, ws, log])   | A Signal account linked to a `signal-cli-rest-api` server.   |
|--------------------------------------------------------------------------------|--------------------------------------------------------------|

### *class* correspond.channels.signal.Signal(\*, http=<function urllib_http>, ws=<function websocket_frames>, log=None)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

A Signal account linked to a `signal-cli-rest-api` server.

#### audience(ref, , draft=None)

Who reads a conversation: a person’s is `named`; a group’s members are listed by the REST API.

* **Return type:**
  [`Audience`](correspond.model.html.md#correspond.model.Audience)

#### *property* capabilities *: [Capabilities](correspond.model.html.md#correspond.model.Capabilities)*

A linked device’s view of Signal.

#### *property* log *: [MutableMapping](https://docs.python.org/3/library/collections.abc.html#collections.abc.MutableMapping)[[str](https://docs.python.org/3/builtins/stdtypes.html#str), [Any](https://docs.python.org/3/library/typing.html#typing.Any)]*

Every message received or sent, as JSON, keyed `<conversation id>/<message id>.json`; identities seen sit under `identities/`.

#### parse_ref(id)

`""` (the account’s stream), a phone number or uuid, or `group.<id>`.

* **Return type:**
  [`ConversationRef`](correspond.model.html.md#correspond.model.ConversationRef)

#### poll(ref, , cursor=None, limit=None)

What the WebSocket delivered within `listen_wait_s`, then any safety-number changes.

Signal hands each message to the receiver once, so the cursor (the last envelope
timestamp) only marks progress; `delivery_id` is what dedupes.

#### read(ref, , since=None, limit=None)

What the log holds for a conversation, oldest first.

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`Message`](correspond.model.html.md#correspond.model.Message)]

#### send(ref, draft, , dry_run=False)

`POST /v2/send` to a person or group, the title (if any) prepended; a reply quotes a logged message.

* **Return type:**
  [`SendResult`](correspond.model.html.md#correspond.model.SendResult)

### correspond.channels.signal.requirements_problems()

What stops Signal working from this configuration; the signal-cli age caveat is a note, not a problem.

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]

### correspond.channels.signal.websocket_frames(url, , timeout)

Text frames of the WebSocket at `url` until it has been quiet for `timeout` seconds (the default `ws=`).

* **Return type:**
  [`Iterable`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Iterable)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]
