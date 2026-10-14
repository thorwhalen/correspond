# correspond.notify

Make a draft’s `notify` list a property of sending, not a rule a sender can forget.

A [`Draft`](correspond.model.html.md#correspond.model.Draft) may name who must be notified (`Draft.notify`).
Each writer then guarantees it in its platform’s terms: a mention added to the text
when it is absent (GitHub, Telegram), an address in To or Cc (email). A channel whose
`capabilities.notify` is `none` refuses such a draft with `NotSupported("notify")`
before anything is planned, so a notification is never silently dropped.

These helpers are pure: the writers call them while building the plan, so the dry run
shows the exact text, or the exact Cc, that will go out.

```pycon
>>> with_mentions("Fixed in #12.", ["octocat", "@hubot"])
'Fixed in #12.\n\ncc @octocat @hubot'
>>> with_mentions("Thanks @Octocat, fixed.", ["octocat"])
'Thanks @Octocat, fixed.'
```

### Module Attributes

| [`DFLT_MENTION_LEAD`](#correspond.notify.DFLT_MENTION_LEAD)   | The line that carries the mentions a text lacked, before the handles.   |
|----------------------------------------------------------------------|-------------------------------------------------------------------------|

### Functions

| [`handle`](#correspond.notify.handle)(name)                           | A handle without its leading `@`.                                                              |
|-----------------------------------------------------------------------------------------|------------------------------------------------------------------------------------------------|
| [`is_mentioned`](#correspond.notify.is_mentioned)(text, name)               | Is `@name` in `text` as a whole mention that notifies? Case-insensitive.                       |
| [`missing_mentions`](#correspond.notify.missing_mentions)(text, names)          | The handles in `names` that `text` does not mention yet, in order, each once.                  |
| [`with_copies`](#correspond.notify.with_copies)(to, cc, names)             | `cc` plus every address in `names` that is neither `to` nor already copied (case-insensitive). |
| [`with_mentions`](#correspond.notify.with_mentions)(text, names, \*[, lead]) | `text` with a closing `cc @a @b` line naming every handle it did not mention.                  |

### correspond.notify.DFLT_MENTION_LEAD *= 'cc'*

The line that carries the mentions a text lacked, before the handles.

### correspond.notify.handle(name)

A handle without its leading `@`.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

```pycon
>>> handle("@octocat"), handle("octocat")
('octocat', 'octocat')
```

### correspond.notify.is_mentioned(text, name)

Is `@name` in `text` as a whole mention that notifies? Case-insensitive.

Not inside an address or a longer handle, and not inside code or an HTML comment,
where platforms render it without notifying anyone.

* **Return type:**
  [`bool`](https://docs.python.org/3/builtins/functions.html#bool)

```pycon
>>> is_mentioned("ping @octocat.", "octocat")
True
>>> is_mentioned("mail octocat@example.org", "octocat")
False
>>> is_mentioned("ping @octocat-two", "octocat")
False
>>> is_mentioned("see `@octocat` and <!-- @octocat -->", "octocat")
False
```

### correspond.notify.missing_mentions(text, names)

The handles in `names` that `text` does not mention yet, in order, each once.

* **Return type:**
  [`tuple`](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`...`](https://docs.python.org/3/builtins/constants.html#Ellipsis)]

### correspond.notify.with_copies(to, cc, names)

`cc` plus every address in `names` that is neither `to` nor already copied (case-insensitive).

* **Return type:**
  [`tuple`](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`...`](https://docs.python.org/3/builtins/constants.html#Ellipsis)]

```pycon
>>> with_copies("a@example.org", ["b@example.org"], ["A@example.org", "c@example.org"])
('b@example.org', 'c@example.org')
```

### correspond.notify.with_mentions(text, names, , lead='cc')

`text` with a closing `cc @a @b` line naming every handle it did not mention.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)
