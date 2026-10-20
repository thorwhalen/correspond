# correspond.channels.slack_events

Slack Events API receiver: Slack pushes a channel’s messages, edits and deletions, so listen need not poll.

[`mk_events_app()`](#correspond.channels.slack_events.mk_events_app) is the receiver, an ASGI app that checks every request with
[`verify()`](correspond.channels.slack.html.md#correspond.channels.slack.Slack.verify) (`X-Slack-Signature` under the signing
secret, over a timestamp at most five minutes old: nothing unverified is read), answers
Slack’s `url_verification` challenge, and keeps the `message` events under the data
root (`slack-events/`). With `delivery = "events"` in the `slack` config, `listen`
reads what the receiver stored instead of polling `conversations.history`, and reports
edits (`message.updated`) and deletions (`message.deleted`) too.

[`serve_events()`](#correspond.channels.slack_events.serve_events) runs the receiver (with uvicorn, the `server` extra) on
127.0.0.1:8083 unless `webhook_allow_remote` is set; put your HTTPS reverse proxy in
front, and give its URL as the app’s Event Subscriptions Request URL, subscribed to the
bot events `message.channels` (and `message.groups` for private channels).

### Functions

| [`events_app_from_env`](#correspond.channels.slack_events.events_app_from_env)()                             | [`mk_events_app()`](#correspond.channels.slack_events.mk_events_app) configured from the settings, for `uvicorn --factory`.   |
|----------------------------------------------------------------------------------------------------|---------------------------------------------------------------------------------------------------------------------------|
| [`events_store`](#correspond.channels.slack_events.events_store)()                                    | Where the receiver keeps events: the `slack` `webhook_store` setting's `module:attr`, else files under the data root.     |
| [`mk_events_app`](#correspond.channels.slack_events.mk_events_app)(\*[, verify, events, ...])          | The receiver as an ASGI app: Slack POSTs signed requests, the app stores the `message` events in `events`.                |
| [`prune_events`](#correspond.channels.slack_events.prune_events)(events, \*, now_ns[, retention_ns])  | Delete the stored events that arrived more than `retention_ns` before `now_ns`; return how many.                          |
| [`serve_events`](#correspond.channels.slack_events.serve_events)(\*[, host, port, allow_remote, ...]) | Run the receiver (with uvicorn) on `host`, this machine unless `allow_remote`.                                            |
| [`serve_main`](#correspond.channels.slack_events.serve_main)([argv])                                | `correspond-slack-events`: run the Events API receiver ([`serve_events()`](#correspond.channels.slack_events.serve_events)). |

### correspond.channels.slack_events.events_app_from_env()

[`mk_events_app()`](#correspond.channels.slack_events.mk_events_app) configured from the settings, for `uvicorn --factory`.

The signing secret is required here, so a receiver without one refuses to start
rather than refusing every request.

* **Return type:**
  [`Callable`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Callable)[[`...`](https://docs.python.org/3/builtins/constants.html#Ellipsis), [`Awaitable`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Awaitable)[[`None`](https://docs.python.org/3/builtins/constants.html#None)]]

### correspond.channels.slack_events.events_store()

Where the receiver keeps events: the `slack` `webhook_store` setting’s `module:attr`, else files under the data root.

* **Return type:**
  [`MutableMapping`](https://docs.python.org/3/library/collections.abc.html#collections.abc.MutableMapping)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`Any`](https://docs.python.org/3/library/typing.html#typing.Any)]

### correspond.channels.slack_events.mk_events_app(\*, verify=None, events=None, max_body_bytes=1000000, clock=<built-in function time_ns>, prune_every_ns=3600000000000, keep=None, stamps=None)

The receiver as an ASGI app: Slack POSTs signed requests, the app stores the `message` events in `events`.

`verify` grades a request from its headers and raw body; only `crypto` passes,
anything else gets 401 and nothing is read from it. It defaults to
[`Slack.verify`](correspond.channels.slack.html.md#correspond.channels.slack.Slack.verify) (the signing secret,
five-minute window). A verified `url_verification` gets its challenge back; any
other verified request that is not a `message` event is acknowledged (200) and
dropped. A kept event is stored under `<arrival ns>-<event id>.json` as
`{event_id, team_id, channel, event, received_ns}`; a retry Slack sends for an event
already stored (a retry, `X-Slack-Retry-Num`, or a replay within the signature’s
window) is acknowledged and not stored twice.
`events` defaults to files under the data root (`slack-events/`).

`keep` says which channel ids’ events are stored (the others are acknowledged and
dropped); it defaults to the `keep_channels` setting as the receiver starts (restart
it after changing the setting), and to every channel when that is unset. `stamps` (an [`ArrivalStamps`](correspond.stores.html.md#correspond.stores.ArrivalStamps), by default the one
Socket Mode listens share) stamps each event and holds its lock while the event is
stored, so writers on one machine take turns and none stores an event below a cursor
a listen already passed.

The receiver deletes events older than the retention itself ([`prune_events()`](#correspond.channels.slack_events.prune_events)),
on a write, at most once per `prune_every_ns`.

* **Return type:**
  [`Callable`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Callable)[[`...`](https://docs.python.org/3/builtins/constants.html#Ellipsis), [`Awaitable`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Awaitable)[[`None`](https://docs.python.org/3/builtins/constants.html#None)]]

### correspond.channels.slack_events.prune_events(events, , now_ns, retention_ns=604800000000000)

Delete the stored events that arrived more than `retention_ns` before `now_ns`; return how many.

Only keys the receiver writes are considered: anything else in the store is left alone.

```pycon
>>> store = {"00000000000000000001-Ev1.json": {}, "00000000000000000009-Ev2.json": {}, "x": 0}
>>> prune_events(store, now_ns=10, retention_ns=5)
1
>>> sorted(store)
['00000000000000000009-Ev2.json', 'x']
```

* **Return type:**
  [`int`](https://docs.python.org/3/builtins/functions.html#int)

### correspond.channels.slack_events.serve_events(, host='127.0.0.1', port=8083, allow_remote=None, app=None, run=None)

Run the receiver (with uvicorn) on `host`, this machine unless `allow_remote`.

`allow_remote` defaults to the `slack` `webhook_allow_remote` setting. Put your
HTTPS reverse proxy in front.

* **Return type:**
  [`None`](https://docs.python.org/3/builtins/constants.html#None)

### correspond.channels.slack_events.serve_main(argv=None)

`correspond-slack-events`: run the Events API receiver ([`serve_events()`](#correspond.channels.slack_events.serve_events)).

* **Return type:**
  [`None`](https://docs.python.org/3/builtins/constants.html#None)
