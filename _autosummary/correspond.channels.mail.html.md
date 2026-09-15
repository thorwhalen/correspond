# correspond.channels.mail

Email: IMAP to read and listen, SMTP to send, with the standard library.

### References

- `email:`: the whole folder (`INBOX` unless configured), which is what `listen` watches.
- `email:<address>`: the correspondence with one address: its messages in the folder,
  and sends to it.

Message ids are `Message-ID` headers, angle brackets included. A draft’s `reply_to` is
one, and the send carries `In-Reply-To` and `References`. Reading never marks a message
seen.

Authenticity is `claimed` unless the **topmost** `Authentication-Results` header was
added by a server you trust (`trusted_authserv_ids`) and records `dmarc=pass` for the
From domain: then it is `domain`. Only the topmost header counts, even when the trusted
server added several: a lower one carrying the same authserv-id may have been written by the
sender (not every server strips those), so a result recorded lower down fails safe as
`claimed`.

```pycon
>>> Email().parse_ref("Ada@Example.org").encoded
'email:ada@example.org'
```

### Module Attributes

| [`LIST_LOCAL_PARTS`](#correspond.channels.mail.LIST_LOCAL_PARTS)   | Words that, as a part of an address's local part (`dev-team@`), mark it as a possible mailing list.   |
|---------------------------------------------------------------------|-------------------------------------------------------------------------------------------------------|

### Functions

| [`authenticity`](#correspond.channels.mail.authenticity)(message, trusted_authserv_ids)   | `domain` only when the topmost Authentication-Results comes from a trusted server and DMARC passed for the From domain.   |
|------------------------------------------------------------------------------------------------|---------------------------------------------------------------------------------------------------------------------------|

### Classes

| [`Email`](#correspond.channels.mail.Email)(\*[, imap, smtp, run])   | A mailbox over IMAP and SMTP.   |
|---------------------------------------------------------------------------------|---------------------------------|

### *class* correspond.channels.mail.Email(\*, imap=None, smtp=None, run=<function run>)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

A mailbox over IMAP and SMTP.

#### audience(ref, , draft=None)

Who reads an email: its address plus the draft’s `cc` and `bcc`, asked of nothing but the config.

Scope `named`, never `complete`: any address may be an alias, a shared mailbox or
an auto-forward, which nothing here can see. A recipient whose domain is not in
`own_domains` makes it `external`; a list-shaped address (under `lists` in the
config, or a local part naming a group, see [`LIST_LOCAL_PARTS`](#correspond.channels.mail.LIST_LOCAL_PARTS)) adds a class
and `list_expansion`, and, when every address is in an own domain, leaves
`external` unknown, since a list can have outside members. A Bcc address is a
class of its own, so moving it to Cc changes the hash. Every email is pushed to its
recipients, can be forwarded, and cannot be recalled.

* **Return type:**
  [`Audience`](correspond.model.html.md#correspond.model.Audience)

#### *property* capabilities *: [Capabilities](correspond.model.html.md#correspond.model.Capabilities)*

Read and listen to one folder; send with threading headers.

#### parse_ref(id)

`""` for the folder, or an address.

* **Return type:**
  [`ConversationRef`](correspond.model.html.md#correspond.model.ConversationRef)

#### poll(ref, , cursor=None, limit=None)

New messages since `cursor` (`<uidvalidity>:<uid>`); a first poll, or a changed UIDVALIDITY, looks back a day.

#### read(ref, , since=None, limit=None)

Messages in the folder (from `ref`’s address, if it has one), oldest first; never marked seen.

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`Message`](correspond.model.html.md#correspond.model.Message)]

#### send(ref, draft, , dry_run=False)

Send to `ref`’s address, with `In-Reply-To` and `References` when replying; `cc` in a header, `bcc` only on the envelope.

* **Return type:**
  [`SendResult`](correspond.model.html.md#correspond.model.SendResult)

### correspond.channels.mail.LIST_LOCAL_PARTS *= ('list', 'all', 'team', 'dev', 'announce', 'info')*

Words that, as a part of an address’s local part (`dev-team@`), mark it as a possible mailing list.

### correspond.channels.mail.authenticity(message, trusted_authserv_ids)

`domain` only when the topmost Authentication-Results comes from a trusted server and DMARC passed for the From domain.

* **Return type:**
  [`Authenticity`](correspond.model.html.md#correspond.model.Authenticity)
