# correspond.channels.gdrive

Google Drive: a shared folder, read only, where files that a conversation announces arrive.

A message often says “uploaded, it’s the one dated Tuesday” about a file that is not
attached to it: the folder is the payload and the message is the notification. This
channel lets one facade read both ends. It reads, and does nothing else: no listening, no
writing.

Reference: `gdrive:<folder id>` (a folder’s URL, `https://drive.google.com/drive/folders/<id>`,
is accepted and normalised to its id).

`read` returns one [`Message`](correspond.model.html.md#correspond.model.Message) per file in the folder and its
subfolders: the text is the file’s path in the folder, `sent_at` its last modification,
the author Drive’s last modifying user, and the file itself the message’s one
[`Attachment`](correspond.model.html.md#correspond.model.Attachment), fetched only on `content()`. So `since` and
`limit` mean what they mean everywhere, and “the file Ada mentioned on Tuesday” lines up
with “the file modified on Tuesday”. A Google Docs, Sheets or Slides file has no bytes of its
own; its attachment says so when fetched.

Credentials are the host’s: a service-account key file named by `service_account_file`,
used with the read-only Drive scope (share the folder with the account’s `client_email`),
or an authenticated PyDrive2 `GoogleDrive` passed as `drive=`. Needs the `gdrive`
extra (pydrivedol), imported on first use, not when the channel is listed.

```pycon
>>> GoogleDrive().parse_ref("https://drive.google.com/drive/folders/0AbCdEfGhIjKlMnOp?usp=sharing").encoded
'gdrive:0AbCdEfGhIjKlMnOp'
```

### Module Attributes

| [`READONLY_SCOPE`](#correspond.channels.gdrive.READONLY_SCOPE)   | it never writes to Drive.   |
|-------------------------------------------------------------------|-----------------------------|

### Classes

| [`GoogleDrive`](#correspond.channels.gdrive.GoogleDrive)(\*[, drive, run, max_depth])   | A Drive folder, read only: its files as messages, each carrying the file as an attachment.   |
|---------------------------------------------------------------------------------------------|----------------------------------------------------------------------------------------------|

### *class* correspond.channels.gdrive.GoogleDrive(\*, drive=None, run=<function run>, max_depth=5)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

A Drive folder, read only: its files as messages, each carrying the file as an attachment.

#### audience(ref, , draft=None)

Who can read the folder: its sharing permissions, asked of Drive now.

`anyone` (with or without the link) makes it public; a domain, the organisation;
otherwise the users it is shared with, by address, and each group as a class whose
members are not listed. Anyone who can read a file can download and keep a copy.

* **Return type:**
  [`Audience`](correspond.model.html.md#correspond.model.Audience)

#### *property* capabilities *: [Capabilities](correspond.model.html.md#correspond.model.Capabilities)*

Read a folder’s files, and nothing else.

#### *property* drive *: [Any](https://docs.python.org/3/library/typing.html#typing.Any)*

the one passed in, else one built from `service_account_file` with the read-only scope.

* **Type:**
  The authenticated PyDrive2 `GoogleDrive`

#### parse_ref(id)

A folder id, or a folder URL normalised to its id.

* **Return type:**
  [`ConversationRef`](correspond.model.html.md#correspond.model.ConversationRef)

#### read(ref, , since=None, limit=None)

The folder’s files, oldest modification first, one message each; `since` is asked of Drive.

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`Message`](correspond.model.html.md#correspond.model.Message)]

### correspond.channels.gdrive.READONLY_SCOPE *= 'https://www.googleapis.com/auth/drive.readonly'*

it never writes to Drive.

* **Type:**
  The only scope correspond asks for
