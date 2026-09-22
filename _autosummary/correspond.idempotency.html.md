# correspond.idempotency

Idempotent sends: an `idempotency_key` keeps a message from going out twice.

A channel can post a message and then fail: the platform accepted the post and the
connection dropped before its answer, or a call after the post raised. [`send()`](correspond.html.md#correspond.send)
then reports `ok=False`, and whoever sends it again (a retry, an operator releasing the
draft by hand) posts it a second time. With `idempotency_key=` [`send()`](correspond.html.md#correspond.send)
remembers, in a store of its own (`sends=`, by default files under the data root), what
each key did:

- right before the real write the key is claimed ([`ATTEMPTED`](#correspond.idempotency.ATTEMPTED)), and after it the key
  is [`SENT`](#correspond.idempotency.SENT), with the result; a failure the platform reported before accepting
  anything ([`NOTHING_POSTED_KINDS`](#correspond.idempotency.NOTHING_POSTED_KINDS)) marks it [`FAILED`](#correspond.idempotency.FAILED), which a new try may
  claim again;
- the same key after [`SENT`](#correspond.idempotency.SENT) posts nothing and answers the stored result, marked a
  replay;
- the same key after an attempt whose outcome is unknown (still [`ATTEMPTED`](#correspond.idempotency.ATTEMPTED): a
  network or platform failure, a crash) reads the conversation back, where the channel can
  be read, for a message by correspond’s own account with the same text since the attempt.
  Found, that message is the result, and nothing is posted. Not found, or not readable, the
  send is not made: it fails with `error_kind="unconfirmed"`, saying to check the
  conversation, since a reader may not show a message that did go out (an email’s inbox
  does not list what was sent, and Telegram’s posted text starts with the title). The
  caller who has checked sends with a new key;
- a second send with a key another one claimed meanwhile (a retry that overlapped the
  first) writes nothing: the default store claims a key with an exclusive create, so of
  two processes exactly one sends;
- a key used again for another conversation or another draft is refused (`validation`).

A dry run reads the store and writes nothing. Nothing is added to the text posted.

```pycon
>>> record = claimed("k1", "fake:example/demo", fingerprint="f", at=parse_time("2026-09-22T12:00:00Z"))
>>> record["state"], record["attempted_at"]
('attempted', '2026-09-22T12:00:00+00:00')
```

### Module Attributes

| [`ATTEMPTED`](#correspond.idempotency.ATTEMPTED)            | The key was claimed for a real write whose outcome is not known yet (or never became known).                                            |
|-----------------------------------------------------------------------|-----------------------------------------------------------------------------------------------------------------------------------------|
| [`SENT`](#correspond.idempotency.SENT)                 | The write went out; the record keeps its result.                                                                                        |
| [`FAILED`](#correspond.idempotency.FAILED)               | The platform refused the write before accepting anything; the key may be claimed again.                                                 |
| [`NOTHING_POSTED_KINDS`](#correspond.idempotency.NOTHING_POSTED_KINDS) | The `error_kind` of failures that mean the platform accepted nothing, so a new try is safe.                                             |
| [`READ_BACK_SKEW`](#correspond.idempotency.READ_BACK_SKEW)       | How far before an attempt a message read back may be dated and still be that attempt's: the platform's clock and this machine's differ. |
| [`MAX_KEY_LENGTH`](#correspond.idempotency.MAX_KEY_LENGTH)       | The longest key accepted (it names a file in the default store).                                                                        |

### Functions

| [`check_key`](#correspond.idempotency.check_key)(key)                                  | `key` stripped, or `ValueError` for one that is not a non-blank string of at most [`MAX_KEY_LENGTH`](#correspond.idempotency.MAX_KEY_LENGTH) characters.               |
|--------------------------------------------------------------------------------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| [`attempted_at`](#correspond.idempotency.attempted_at)(record)                            | When `record`'s key was claimed.                                                                                                                                            |
| [`claimed`](#correspond.idempotency.claimed)(key, conversation, \*, fingerprint, at) | The record of `key` claimed at `at` for a real write to `conversation`.                                                                                                     |
| [`fingerprint`](#correspond.idempotency.fingerprint)(conversation, draft)                | What a key is bound to: the conversation and everything the draft says, as SHA-256 hex.                                                                                     |
| [`matching_message`](#correspond.idempotency.matching_message)(messages, draft, \*, since)    | The latest of `messages` written by correspond's own account with `draft`'s text, at or after `since` less [`READ_BACK_SKEW`](#correspond.idempotency.READ_BACK_SKEW). |
| [`now`](#correspond.idempotency.now)()                                           | The moment a key is claimed (UTC).                                                                                                                                          |
| [`settled`](#correspond.idempotency.settled)(record, state, result)                  | `record` moved to `state` ([`SENT`](#correspond.idempotency.SENT) or [`FAILED`](#correspond.idempotency.FAILED)), with the write's `result`.    |

### correspond.idempotency.ATTEMPTED *= 'attempted'*

The key was claimed for a real write whose outcome is not known yet (or never became known).

### correspond.idempotency.FAILED *= 'failed'*

The platform refused the write before accepting anything; the key may be claimed again.

### correspond.idempotency.MAX_KEY_LENGTH *= 128*

The longest key accepted (it names a file in the default store).

### correspond.idempotency.NOTHING_POSTED_KINDS *= frozenset({'auth', 'not_found', 'permission', 'rate_limited'})*

The `error_kind` of failures that mean the platform accepted nothing, so a new try is safe.
A network failure and an unavailable platform are not among them: the post may have landed.
`validation` is not among them either: a platform can report an error with no type
after it committed the write (GitHub’s GraphQL mutations do on a timeout).

### correspond.idempotency.READ_BACK_SKEW *= datetime.timedelta(seconds=300)*

How far before an attempt a message read back may be dated and still be that attempt’s:
the platform’s clock and this machine’s differ.

### correspond.idempotency.SENT *= 'sent'*

The write went out; the record keeps its result.

### correspond.idempotency.attempted_at(record)

When `record`’s key was claimed.

* **Return type:**
  [`datetime`](https://docs.python.org/3/library/datetime.html#datetime.datetime)

### correspond.idempotency.check_key(key)

`key` stripped, or `ValueError` for one that is not a non-blank string of at most [`MAX_KEY_LENGTH`](#correspond.idempotency.MAX_KEY_LENGTH) characters.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

```pycon
>>> check_key(" case-12/reply-3 ")
'case-12/reply-3'
```

### correspond.idempotency.claimed(key, conversation, , fingerprint, at)

The record of `key` claimed at `at` for a real write to `conversation`.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`Any`](https://docs.python.org/3/library/typing.html#typing.Any)]

### correspond.idempotency.fingerprint(conversation, draft)

What a key is bound to: the conversation and everything the draft says, as SHA-256 hex.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

### correspond.idempotency.matching_message(messages, draft, , since)

The latest of `messages` written by correspond’s own account with `draft`’s text, at or after `since` less [`READ_BACK_SKEW`](#correspond.idempotency.READ_BACK_SKEW).

* **Return type:**
  [`Message`](correspond.model.html.md#correspond.model.Message) | [`None`](https://docs.python.org/3/builtins/constants.html#None)

### correspond.idempotency.now()

The moment a key is claimed (UTC).

* **Return type:**
  [`datetime`](https://docs.python.org/3/library/datetime.html#datetime.datetime)

### correspond.idempotency.settled(record, state, result)

`record` moved to `state` ([`SENT`](#correspond.idempotency.SENT) or [`FAILED`](#correspond.idempotency.FAILED)), with the write’s `result`.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`Any`](https://docs.python.org/3/library/typing.html#typing.Any)]
