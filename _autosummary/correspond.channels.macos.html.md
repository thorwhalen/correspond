# correspond.channels.macos

macOS Notification Centre: a banner on this Mac (send only).

Reference: `macos:` (this machine; it takes no id). A send uses `terminal-notifier` when
it is installed, because it keeps bodies with quotes intact, and `osascript` otherwise.
Either way the text travels as program arguments, never inside a script, so nothing in a
message can be run.

```pycon
>>> MacOS().parse_ref("").encoded
'macos:'
```

### Classes

| [`MacOS`](#correspond.channels.macos.MacOS)(\*[, run])   | Notification Centre on the Mac correspond runs on.   |
|---------------------------------------------------------------------|------------------------------------------------------|

### *class* correspond.channels.macos.MacOS(\*, run=<function run>)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

Notification Centre on the Mac correspond runs on.

#### *property* capabilities *: [Capabilities](correspond.model.html.md#correspond.model.Capabilities)*

Send only.

#### parse_ref(id)

Only `macos:`.

* **Return type:**
  [`ConversationRef`](correspond.model.html.md#correspond.model.ConversationRef)

#### send(ref, draft, , dry_run=False)

Show a banner with the draft’s title and text.

* **Return type:**
  [`SendResult`](correspond.model.html.md#correspond.model.SendResult)
