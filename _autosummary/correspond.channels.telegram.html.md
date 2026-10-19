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

**Webhook delivery**, for a server: [`Telegram.set_webhook()`](#correspond.channels.telegram.Telegram.set_webhook) asks Telegram to POST
every update, one at a time, to an HTTPS address, carrying the configured
`webhook_secret` in the `X-Telegram-Bot-Api-Secret-Token` header.
[`mk_webhook_app()`](#correspond.channels.telegram.mk_webhook_app) is the receiver, an ASGI app that stores each update carrying that
secret under the data root (`telegram-webhook/`), in the order it arrives; the secret is
the gate, and nothing without it is stored. [`serve_webhook()`](#correspond.channels.telegram.serve_webhook) runs it (with uvicorn,
the `server` extra) on `127.0.0.1` behind your own HTTPS reverse proxy, and refuses
any other address unless `webhook_allow_remote` is set. With `delivery = "webhook"` in
the config, `listen` reads what the receiver stored instead of calling `getUpdates`:
Telegram serves one or the other, never both, so while a webhook is set `getUpdates` is
refused (409), and [`Telegram.delete_webhook()`](#correspond.channels.telegram.Telegram.delete_webhook) returns the bot to polling.
[`Telegram.verify()`](#correspond.channels.telegram.Telegram.verify) grades one delivery by the same header.

```pycon
>>> Telegram().parse_ref("-4001/7").parent.encoded
'telegram:-4001'
```

### Module Attributes

| [`SECRET_HEADER`](#correspond.channels.telegram.SECRET_HEADER)   | The header Telegram puts the webhook's secret token in.   |
|------------------------------------------------------------------|-----------------------------------------------------------|

### Functions

| [`mk_webhook_app`](#correspond.channels.telegram.mk_webhook_app)(\*, secret[, updates, ...])         | The webhook receiver as an ASGI app: Telegram POSTs updates, the app stores them in `updates`.                           |
|-----------------------------------------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------------|
| [`serve_webhook`](#correspond.channels.telegram.serve_webhook)(\*[, host, port, allow_remote, ...]) | Run the receiver (with uvicorn) on `host`, this machine unless `allow_remote`.                                           |
| [`webhook_app_from_env`](#correspond.channels.telegram.webhook_app_from_env)()                             | [`mk_webhook_app()`](#correspond.channels.telegram.mk_webhook_app) configured from the settings, for `uvicorn --factory`. |

### Classes

| [`Telegram`](#correspond.channels.telegram.Telegram)(\*[, http, log, updates, run, clock])   | A Telegram bot: listen, read the local log, send, edit, react.   |
|---------------------------------------------------------------------------------------------------|------------------------------------------------------------------|

### correspond.channels.telegram.SECRET_HEADER *= 'x-telegram-bot-api-secret-token'*

The header Telegram puts the webhook’s secret token in.

### *class* correspond.channels.telegram.Telegram(\*, http=<function urllib_http>, log=None, updates=None, run=<function run>, clock=<built-in function time_ns>)

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
  [`Audience`](correspond.model.html.md#correspond.model.Audience)

#### *property* capabilities *: [Capabilities](correspond.model.html.md#correspond.model.Capabilities)*

A bot’s view of Telegram.

#### delete_webhook(, drop_pending_updates=False, dry_run=False)

Stop webhook delivery, so `getUpdates` works again; pending updates are kept unless `drop_pending_updates`.

* **Return type:**
  [`SendResult`](correspond.model.html.md#correspond.model.SendResult)

#### edit(ref, message_id, draft, , dry_run=False)

`editMessageText` on one of the bot’s messages.

* **Return type:**
  [`SendResult`](correspond.model.html.md#correspond.model.SendResult)

#### *property* log *: [MutableMapping](https://docs.python.org/3/library/collections.abc.html#collections.abc.MutableMapping)[[str](https://docs.python.org/3/builtins/stdtypes.html#str), [Any](https://docs.python.org/3/library/typing.html#typing.Any)]*

Every message this bot has received or sent, as JSON, keyed `<chat id>/<message id>.json`.

#### parse_ref(id)

`""` (the bot), a chat id or `@name`, or `<chat id>/<topic id>`.

* **Return type:**
  [`ConversationRef`](correspond.model.html.md#correspond.model.ConversationRef)

#### poll(ref, , cursor=None, limit=None)

Updates since `cursor`, every one logged before it is handed over.

They come from `getUpdates` (the cursor is an update offset), or, with `delivery
= "webhook"` in the config, from what the webhook receiver stored
(`_poll_received()`).

#### prepare(ref, draft)

The draft as it will post: `notify` usernames checked, the missing mentions added. Pure.

* **Return type:**
  [`Draft`](correspond.model.html.md#correspond.model.Draft)

#### react(ref, message_id, reaction, , dry_run=False)

`setMessageReaction` with one emoji (a bot has one reaction per message).

* **Return type:**
  [`SendResult`](correspond.model.html.md#correspond.model.SendResult)

#### read(ref, , since=None, limit=None)

What the log holds for a chat or topic, oldest first.

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`Message`](correspond.model.html.md#correspond.model.Message)]

#### send(ref, draft, , dry_run=False)

`sendMessage`, the title (if any) prepended; `draft.notify` usernames the text does not @mention yet are mentioned on a closing `cc` line.

* **Return type:**
  [`SendResult`](correspond.model.html.md#correspond.model.SendResult)

#### set_webhook(url, , dry_run=False)

Have Telegram POST this bot’s updates to `url` (HTTPS), with the configured `webhook_secret`.

`getUpdates` stops working until [`delete_webhook()`](#correspond.channels.telegram.Telegram.delete_webhook); set `delivery =
"webhook"` so `listen` reads what the receiver stores. The plan reads the
webhook back with `getWebhookInfo`. A dry run calls nothing and reads no secret.

* **Return type:**
  [`SendResult`](correspond.model.html.md#correspond.model.SendResult)

#### *property* updates *: [MutableMapping](https://docs.python.org/3/library/collections.abc.html#collections.abc.MutableMapping)[[str](https://docs.python.org/3/builtins/stdtypes.html#str), [Any](https://docs.python.org/3/library/typing.html#typing.Any)]*

What the webhook receiver stored, keyed by zero-padded update id (`listen` reads it when `delivery` is `webhook`).

#### verify(headers, body)

`platform` when the delivery carries the configured `webhook_secret`, `forged` otherwise.

The secret token proves Telegram sent the request (only Telegram and this machine
know it); it signs nothing, so the grade is Telegram’s own, not `crypto`.

* **Return type:**
  [`Authenticity`](correspond.model.html.md#correspond.model.Authenticity)

#### webhook_info()

Where Telegram delivers this bot’s updates now (`getWebhookInfo`): an empty `url` means polling.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`Any`](https://docs.python.org/3/library/typing.html#typing.Any)]

### correspond.channels.telegram.mk_webhook_app(\*, secret, updates=None, max_body_bytes=1000000, clock=<built-in function time_ns>)

The webhook receiver as an ASGI app: Telegram POSTs updates, the app stores them in `updates`.

A request without `secret` in `X-Telegram-Bot-Api-Secret-Token` is refused (401)
and nothing is stored: the secret is the only gate, since behind a reverse proxy every
connection comes from this machine. Each update is stored under
`<arrival time in ns>-<update id>.json`, so `listen` takes them in the order they
came, whatever their ids (Telegram restarts them at random after a week without
updates). One receiver process per bot keeps that order: [`Telegram.set_webhook()`](#correspond.channels.telegram.Telegram.set_webhook)
asks Telegram for one connection at a time. `updates` defaults to files under the
data root (`telegram-webhook/`), which `listen` reads when `delivery` is
`webhook`.

* **Return type:**
  [`Callable`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Callable)[[`...`](https://docs.python.org/3/builtins/constants.html#Ellipsis), [`Awaitable`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Awaitable)[[`None`](https://docs.python.org/3/builtins/constants.html#None)]]

### correspond.channels.telegram.serve_webhook(, host='127.0.0.1', port=8081, allow_remote=None, app=None, run=None)

Run the receiver (with uvicorn) on `host`, this machine unless `allow_remote`.

`allow_remote` defaults to the `webhook_allow_remote` setting. Put your HTTPS
reverse proxy in front: Telegram reaches only public HTTPS addresses. Proxy headers are
ignored, since the receiver needs no client address, and one worker keeps the order.

* **Return type:**
  [`None`](https://docs.python.org/3/builtins/constants.html#None)

### correspond.channels.telegram.webhook_app_from_env()

[`mk_webhook_app()`](#correspond.channels.telegram.mk_webhook_app) configured from the settings, for `uvicorn --factory`.

* **Return type:**
  [`Callable`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Callable)[[`...`](https://docs.python.org/3/builtins/constants.html#Ellipsis), [`Awaitable`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Awaitable)[[`None`](https://docs.python.org/3/builtins/constants.html#None)]]
