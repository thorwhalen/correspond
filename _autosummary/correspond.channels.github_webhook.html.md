# correspond.channels.github_webhook

GitHub webhook receiver: `discussion_comment` deliveries, so listen sees every discussion reply.

GitHub has no `since` filter for discussions, so a poll walks them by latest activity
and can miss a reply that moves neither the discussion’s `updatedAt` nor its place in
that order (when the discussion is outside the `recent_discussions` window). A
`discussion_comment` webhook names every such discussion as it happens.

[`mk_webhook_app()`](#correspond.channels.github_webhook.mk_webhook_app) is the receiver, an ASGI app that checks `X-Hub-Signature-256`
against the `webhook_secret` (the HMAC over the body: nothing unsigned is stored) and
keeps, under the data root (`github-webhook/`), only which discussion of which
repository had a comment, and when it arrived. With `delivery = "webhook"` in the
`github` config, `listen` still polls, and also reads whole every discussion the
receiver saw since the cursor, finding the reply by its own creation time: the webhook
is a hint, the poll stays the source of the messages and of the cursor.

[`serve_webhook()`](#correspond.channels.github_webhook.serve_webhook) runs the receiver (with uvicorn, the `server` extra) on
127.0.0.1:8082 unless `webhook_allow_remote` is set; put your HTTPS reverse proxy in
front, and point a repository or organization webhook (content type
`application/json`, the “Discussion comments” event) at it.

### Module Attributes

| [`EVENTS`](#correspond.channels.github_webhook.EVENTS)       | The events the receiver keeps; any other signed event is acknowledged and dropped.   |
|---------------------------------------------------------------|--------------------------------------------------------------------------------------|
| [`WEBHOOK_KIND`](#correspond.channels.github_webhook.WEBHOOK_KIND) | The data-root folder the receiver stores deliveries in.                              |

### Functions

| [`mk_webhook_app`](#correspond.channels.github_webhook.mk_webhook_app)(\*, secret[, deliveries, ...])      | The receiver as an ASGI app: GitHub POSTs signed deliveries, the app stores the `events` ones in `deliveries`.             |
|-----------------------------------------------------------------------------------------------------|----------------------------------------------------------------------------------------------------------------------------|
| [`serve_main`](#correspond.channels.github_webhook.serve_main)([argv])                                 | `correspond-github-webhook`: run the webhook receiver ([`serve_webhook()`](#correspond.channels.github_webhook.serve_webhook)).  |
| [`serve_webhook`](#correspond.channels.github_webhook.serve_webhook)(\*[, host, port, allow_remote, ...]) | Run the receiver (with uvicorn) on `host`, this machine unless `allow_remote`.                                             |
| [`webhook_app_from_env`](#correspond.channels.github_webhook.webhook_app_from_env)()                             | [`mk_webhook_app()`](#correspond.channels.github_webhook.mk_webhook_app) configured from the settings, for `uvicorn --factory`.   |
| [`webhook_store`](#correspond.channels.github_webhook.webhook_store)()                                    | Where the receiver keeps deliveries: the `github` `webhook_store` setting's `module:attr`, else files under the data root. |

### correspond.channels.github_webhook.EVENTS *= ('discussion_comment',)*

The events the receiver keeps; any other signed event is acknowledged and dropped.

### correspond.channels.github_webhook.WEBHOOK_KIND *= 'github-webhook'*

The data-root folder the receiver stores deliveries in.

### correspond.channels.github_webhook.mk_webhook_app(\*, secret, deliveries=None, events=('discussion_comment', ), max_body_bytes=1000000, clock=<built-in function time_ns>)

The receiver as an ASGI app: GitHub POSTs signed deliveries, the app stores the `events` ones in `deliveries`.

A request whose `X-Hub-Signature-256` is not the HMAC of its body under `secret` is
refused (401) and nothing is read from it: the signature is the only gate, since behind
a reverse proxy every connection comes from this machine. A signed `ping` (sent when
the webhook is created) and any event not in `events` get 202 and are not stored. A
kept delivery is stored under `<delivery id>.json` as `{event, action,
repository, discussion, comment, received_ns}`: no comment text. Keyed by
GitHub’s delivery id, a replayed request (GitHub signatures carry no timestamp)
overwrites its first copy instead of growing the store. `deliveries`
defaults to files under the data root (`github-webhook/`).

* **Return type:**
  [`Callable`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Callable)[[`...`](https://docs.python.org/3/builtins/constants.html#Ellipsis), [`Awaitable`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Awaitable)[[`None`](https://docs.python.org/3/builtins/constants.html#None)]]

### correspond.channels.github_webhook.serve_main(argv=None)

`correspond-github-webhook`: run the webhook receiver ([`serve_webhook()`](#correspond.channels.github_webhook.serve_webhook)).

* **Return type:**
  [`None`](https://docs.python.org/3/builtins/constants.html#None)

### correspond.channels.github_webhook.serve_webhook(, host='127.0.0.1', port=8082, allow_remote=None, app=None, run=None)

Run the receiver (with uvicorn) on `host`, this machine unless `allow_remote`.

`allow_remote` defaults to the `github` `webhook_allow_remote` setting. Put your
HTTPS reverse proxy in front.

* **Return type:**
  [`None`](https://docs.python.org/3/builtins/constants.html#None)

### correspond.channels.github_webhook.webhook_app_from_env()

[`mk_webhook_app()`](#correspond.channels.github_webhook.mk_webhook_app) configured from the settings, for `uvicorn --factory`.

* **Return type:**
  [`Callable`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Callable)[[`...`](https://docs.python.org/3/builtins/constants.html#Ellipsis), [`Awaitable`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Awaitable)[[`None`](https://docs.python.org/3/builtins/constants.html#None)]]

### correspond.channels.github_webhook.webhook_store()

Where the receiver keeps deliveries: the `github` `webhook_store` setting’s `module:attr`, else files under the data root.

* **Return type:**
  [`MutableMapping`](https://docs.python.org/3/library/collections.abc.html#collections.abc.MutableMapping)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`Any`](https://docs.python.org/3/library/typing.html#typing.Any)]
