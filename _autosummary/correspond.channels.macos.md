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

#### audience(ref, , draft=None)

Only the operator: a banner on this Mac, which can be cleared.

* **Return type:**
  [`Audience`](correspond.model.md#correspond.model.Audience)

#### *property* capabilities *: [Capabilities](correspond.model.md#correspond.model.Capabilities)*

Send only.

#### parse_ref(id)

Only `macos:`.

* **Return type:**
  [`ConversationRef`](correspond.model.md#correspond.model.ConversationRef)

#### send(ref, draft, , dry_run=False)

Show a banner with the draft’s title and text.

* **Return type:**
  [`SendResult`](correspond.model.md#correspond.model.SendResult)
