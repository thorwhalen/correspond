# correspond.channels.telegram

Telegram: a bot over the Bot API, with the standard library.

### References

`telegram:` (the bot’s whole update stream, for listening),
`telegram:<chat id>` (a private chat, a group or a channel; `telegram:@name` for a
public channel), `telegram:<chat id>/<topic id>` (a forum topic).

Bots have no history API, and Telegram keeps undelivered updates for at most 24 hours, so
`poll` writes every update it receives to a local log under the data root
(`telegram/`) and `read` reads that log (a `telegram:@name` reference is first resolved
with `getChat`, over the network). `getUpdates` confirms updates for every chat
at once, which is why listening is account-wide: narrow to one chat when routing.

Senders are `platform`: Telegram authenticated the account, and correspond pulled the
update from the Bot API itself. The bot token never appears in an error or a plan.

```pycon
>>> Telegram().parse_ref("-4001/7").parent.encoded
'telegram:-4001'
```

### Classes

| [`Telegram`](#correspond.channels.telegram.Telegram)(\*[, http, log, run])   | A Telegram bot: listen, read the local log, send, edit, react.   |
|-----------------------------------------------------------------------------------|------------------------------------------------------------------|

### *class* correspond.channels.telegram.Telegram(\*, http=<function urllib_http>, log=None, run=<function run>)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

A Telegram bot: listen, read the local log, send, edit, react.

#### audience(ref, , draft=None)

Who reads a chat, from `getChat`; a topic has its chat’s readers.

- A private chat: `named`, the other account (by its id only, so a rename keeps
  the hash) as its reader.
- A chat with a public username: `public`.
- A group or supergroup without one: `group`; a channel without one: `group`
  of its subscribers. Members are never listed.
- A linked chat (a channel’s discussion group, or the channel a group discusses)
  is read too: posts are copied into it. A public linked chat makes the audience
  public; one that cannot be read makes it unknown.

Joiners read the history unless `getChat` says the history is hidden from them.
Forwarding widens every chat unless its content is protected. A failed `getChat`
raises, so the audience resolves to public, defaulted.

* **Return type:**
  [`Audience`](correspond.model.md#correspond.model.Audience)

#### *property* capabilities *: [Capabilities](correspond.model.md#correspond.model.Capabilities)*

A bot’s view of Telegram.

#### edit(ref, message_id, draft, , dry_run=False)

`editMessageText` on one of the bot’s messages.

* **Return type:**
  [`SendResult`](correspond.model.md#correspond.model.SendResult)

#### *property* log *: [MutableMapping](https://docs.python.org/3/library/collections.abc.html#collections.abc.MutableMapping)[[str](https://docs.python.org/3/builtins/stdtypes.html#str), [Any](https://docs.python.org/3/library/typing.html#typing.Any)]*

Every message this bot has received or sent, as JSON, keyed `<chat id>/<message id>.json`.

#### parse_ref(id)

`""` (the bot), a chat id or `@name`, or `<chat id>/<topic id>`.

* **Return type:**
  [`ConversationRef`](correspond.model.md#correspond.model.ConversationRef)

#### poll(ref, , cursor=None, limit=None)

Updates since `cursor` (an update offset), every one logged before it is handed over.

#### react(ref, message_id, reaction, , dry_run=False)

`setMessageReaction` with one emoji (a bot has one reaction per message).

* **Return type:**
  [`SendResult`](correspond.model.md#correspond.model.SendResult)

#### read(ref, , since=None, limit=None)

What the log holds for a chat or topic, oldest first.

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`Message`](correspond.model.md#correspond.model.Message)]

#### send(ref, draft, , dry_run=False)

`sendMessage`, the title (if any) prepended.

* **Return type:**
  [`SendResult`](correspond.model.md#correspond.model.SendResult)
