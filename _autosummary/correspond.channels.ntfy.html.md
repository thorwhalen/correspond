# correspond.channels.ntfy

ntfy: push notifications to a phone or a desktop through an ntfy server (send only).

### References

`ntfy:<topic>`, or `ntfy:` for the default topic, which is resolved in this
order: `$NTFY_TOPIC`; the macOS Keychain (service `topic_keychain_service` in the
`[ntfy]` config table, default `correspond-ntfy-topic`); an ssh host named by
`topic_remote`, asked for the `NTFY_TOPIC=...` line of `topic_remote_file`. A dry run
reads only the environment and the config file (no Keychain, no remote host: those are looked
up when sending), and a plan shows a topic masked.

A topic is a bearer secret: anyone who knows an unauthenticated one can publish to it and
read it. Prefer `ntfy:` to writing a topic into a reference that ends up in logs.

```pycon
>>> Ntfy().parse_ref("").kind
'topic'
```

### Module Attributes

| [`PRIORITY`](#correspond.channels.ntfy.PRIORITY)   | Draft priorities as ntfy's 1-5 scale.   |
|-------------------------------------------------------------|-----------------------------------------|

### Classes

| [`Ntfy`](#correspond.channels.ntfy.Ntfy)(\*[, http, run])   | An ntfy server: publish to a topic.   |
|--------------------------------------------------------------------------|---------------------------------------|

### *class* correspond.channels.ntfy.Ntfy(\*, http=<function urllib_http>, run=<function run>)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

An ntfy server: publish to a topic.

#### audience(ref, , draft=None)

Who reads a topic: anyone who knows its name, unless the config says the server denies anonymous reads.

Nothing is asked of the server. Its cache keeps each message for subscribers who
connect later (`cache_duration`, 12h by default), and every subscriber’s device
gets a copy.

* **Return type:**
  [`Audience`](correspond.model.html.md#correspond.model.Audience)

#### *property* capabilities *: [Capabilities](correspond.model.html.md#correspond.model.Capabilities)*

Send only, with priorities.

#### parse_ref(id)

A topic, or nothing for the default topic.

* **Return type:**
  [`ConversationRef`](correspond.model.html.md#correspond.model.ConversationRef)

#### send(ref, draft, , dry_run=False)

Publish `draft.text` (with its title and priority) to the topic.

* **Return type:**
  [`SendResult`](correspond.model.html.md#correspond.model.SendResult)

### correspond.channels.ntfy.PRIORITY *= {'high': '4', 'low': '2', 'normal': '3', 'urgent': '5'}*

Draft priorities as ntfy’s 1-5 scale.
