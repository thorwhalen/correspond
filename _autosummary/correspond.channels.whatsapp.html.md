# correspond.channels.whatsapp

WhatsApp, from the chat exports you make on the phone: read only, no API, nothing sent.

WhatsApp has no API for a personal account’s chats. What it does have is *Export chat*,
which writes the conversation as `_chat.txt` (zipped, with or without the media). This
channel reads those exports into a local archive, one per chat, and serves the archive as
an ordinary conversation, so `read` with `since` works here as it does everywhere and
answers “what is new since I last processed this chat?”.

Reference: `whatsapp:<chat>`, where `<chat>` is a name you choose when importing
(lowercase letters, digits, `-`, `_` and `.`), for example `whatsapp:ada-lovelace`.

Two steps:

1. [`WhatsAppExport.import_export()`](#correspond.channels.whatsapp.WhatsAppExport.import_export) parses an export (the `.zip` or the `.txt`) and
   merges it into the chat’s archive. Exports overlap (each one repeats the whole chat), so
   a message already archived is recognised by its id and not added twice. The result says
   how many messages were new and the time span they cover.
2. [`WhatsAppExport.read()`](#correspond.channels.whatsapp.WhatsAppExport.read) returns the archived messages, oldest first, with `since`
   and `limit`.

The archive lives under the data root (`<data root>/whatsapp/`), never in a repository:
`<chat>.jsonl` holds one message per line, `<chat>.imports.jsonl` one line per import,
and `<chat>.txt` a transcript with one `<ISO time> <sender>: <text>` line per message,
for grep and for anchors (a time is a stable anchor across imports; a line number is not).

Both export layouts are parsed: iOS (`[2/11/26, 12:51:08] Ada: hi`) and Android
(`2/11/26, 12:51 - Ada: hi`), with 12- or 24-hour times, and `/`, `.` or `-` in
dates. Whether a date is day-first is decided from the export itself (a field above 12
settles it; otherwise the reading that keeps the times in order wins), or set with
`dayfirst`. Times carry no zone in an export: they are read in the zone named by `tz`
(an IANA name), else this machine’s.

```pycon
>>> rows = parse_export("[2/11/26, 12:51:08] Ada: hi\nsecond line\n[2/11/26, 12:52:00] Bram: \u200eimage omitted", tz="UTC")
>>> [(r["sent_at"], r["sender"], r["text"], r["native"].get("media")) for r in rows]
[('2026-02-11T12:51:08+00:00', 'Ada', 'hi\nsecond line', None), ('2026-02-11T12:52:00+00:00', 'Bram', '', 'image')]
```

### Module Attributes

| [`ARCHIVE_KIND`](#correspond.channels.whatsapp.ARCHIVE_KIND)   | The data-root folder that holds the archives.   |
|-----------------------------------------------------------------|-------------------------------------------------|

### Functions

| [`export_text`](#correspond.channels.whatsapp.export_text)(source)                      | The text of an export: a `.txt` file, a `.zip` holding `_chat.txt` (or one `.txt`), or the raw bytes of either.       |
|-------------------------------------------------------------------------------------------|-----------------------------------------------------------------------------------------------------------------------|
| [`message_id`](#correspond.channels.whatsapp.message_id)(row[, occurrence])            | A stable id from the time, sender, text and media kind: the same message in two overlapping exports gets the same id. |
| [`parse_export`](#correspond.channels.whatsapp.parse_export)(text, \*[, dayfirst, tz])   | The messages of an export's text, in file order, as JSON-ready rows.                                                  |
| [`redact_credentials`](#correspond.channels.whatsapp.redact_credentials)(rows, \*[, lookback]) | The rows with credential-looking text replaced by `[redacted: credential]`, and `native.redacted` set.                |

### Classes

| [`WhatsAppExport`](#correspond.channels.whatsapp.WhatsAppExport)(\*[, store, self_names])   | WhatsApp chats as archived from their exports: `import_export` merges one in, `read` serves it.   |
|--------------------------------------------------------------------------------------------|---------------------------------------------------------------------------------------------------|

### correspond.channels.whatsapp.ARCHIVE_KIND *= 'whatsapp'*

The data-root folder that holds the archives.

### *class* correspond.channels.whatsapp.WhatsAppExport(, store=None, self_names=None)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

WhatsApp chats as archived from their exports: `import_export` merges one in, `read` serves it.

`store` is where archives are kept, a `MutableMapping` of file names to text (default:
text files under `<data root>/whatsapp/`; a `dict` in tests). `self_names` are the
names under which the exports show the operator’s own messages (default: the
`self_name` setting, comma-separated), so those messages are marked `is_self`.

#### archived(ref)

The archived rows of one chat, oldest first.

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)]

#### audience(ref, , draft=None)

Who reads the chat, as far as the archive shows: everyone who wrote in it.

* **Return type:**
  [`Audience`](correspond.model.html.md#correspond.model.Audience)

#### *property* capabilities *: [Capabilities](correspond.model.html.md#correspond.model.Capabilities)*

Read what was imported, and nothing else.

#### import_export(ref, source, \*, dayfirst=None, tz=None, now=None, redact=<function redact_credentials>)

Merge an export into the chat’s archive; say what was new.

`redact` runs on the parsed messages before anything is stored (default
[`redact_credentials()`](#correspond.channels.whatsapp.redact_credentials); `None` keeps the text as exported). The result’s
`redacted` counts the messages it changed.

Returns `parsed` (messages in the export), `added`, `total` (archived now),
`previous_last` (the newest archived message before this import, `None` on the
first), `first_added` and `last_added` (the span of what was new), and
`export_span` (first and last time in the export). An export whose messages are
all archived adds nothing and changes nothing but the import log.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)

#### imports(ref)

One record per import of this chat, oldest first.

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)]

#### parse_ref(id)

A chat name: lowercase letters, digits, `-`, `_`, `.`.

* **Return type:**
  [`ConversationRef`](correspond.model.html.md#correspond.model.ConversationRef)

#### read(ref, , since=None, limit=None)

The archived messages, oldest first; `since` and `limit` as everywhere.

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`Message`](correspond.model.html.md#correspond.model.Message)]

#### *property* self_names *: [tuple](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[str](https://docs.python.org/3/builtins/stdtypes.html#str), ...]*

given, else the `self_name` setting.

* **Type:**
  The operator’s own sender names

#### *property* store *: [MutableMapping](https://docs.python.org/3/library/collections.abc.html#collections.abc.MutableMapping)[[str](https://docs.python.org/3/builtins/stdtypes.html#str), [str](https://docs.python.org/3/builtins/stdtypes.html#str)]*

The archive store, built on first use.

#### transcript_path(ref)

The transcript’s path on disk, when the archive is the default one under the data root.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str) | [`None`](https://docs.python.org/3/builtins/constants.html#None)

### correspond.channels.whatsapp.export_text(source)

The text of an export: a `.txt` file, a `.zip` holding `_chat.txt` (or one `.txt`), or the raw bytes of either.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

### correspond.channels.whatsapp.message_id(row, occurrence=0)

A stable id from the time, sender, text and media kind: the same message in two overlapping exports gets the same id.

`occurrence` tells apart identical messages sent in the same second (the second is
`<hash>-1`).

```pycon
>>> message_id({"sent_at": "2026-02-11T12:51:08+00:00", "sender": "Ada", "text": "hi"})
'514e092c59e260a1'
```

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

### correspond.channels.whatsapp.parse_export(text, , dayfirst=None, tz=None)

The messages of an export’s text, in file order, as JSON-ready rows.

Each row: `sent_at` (ISO 8601 with offset), `sender` (the name the export gives,
`""` for a system line), `text` (continuation lines joined, WhatsApp’s direction
marks removed, media and edit markers taken out) and `native`: `media` (`image`,
`video`, `audio`, `sticker`, `gif`, `document`, `contact`, `file`,
`media`), `attached` (the file name, when the export carried the file), `edited`,
`deleted`, `call`, `system`, and `line` (the line number in this export).
Text before the first dated line is ignored.

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)]

### correspond.channels.whatsapp.redact_credentials(rows, , lookback=5)

The rows with credential-looking text replaced by `[redacted: credential]`, and `native.redacted` set.

Redacted: the value after a label (`password: …`, `pwd=…`, `api key is …`); a
known key shape anywhere (`sk-…`, `ghp_…`, `xoxb-…`, `AKIA…`, `AIza…`); and a
message that is one secret-looking token (8 to 128 characters, letters and digits or
three character classes, not a URL or address) when it, or one of the `lookback` messages before it,
mentions a password, a login, a key or a code. Redaction happens before anything is
stored, so the archive never holds what it removed.

```pycon
>>> rows = [{"text": "the password for the account", "native": {}}, {"text": "Tr0ub4dor&3x", "native": {}},
...         {"text": "pwd: s3cret!Value", "native": {}}, {"text": "see https://example.org/Page_1?x=Y2", "native": {}}]
>>> [r["text"] for r in redact_credentials(rows)]
['the password for the account', '[redacted: credential]', 'pwd: [redacted: credential]', 'see https://example.org/Page_1?x=Y2']
```

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)]
