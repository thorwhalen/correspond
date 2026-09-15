# correspond.errors

What correspond raises, and the vocabulary a failed write reports.

Two families, on purpose:

- **Mistakes a caller fixes in code or configuration raise**: an unknown channel
  ([`UnknownChannel`](#correspond.errors.UnknownChannel)), a malformed conversation reference ([`InvalidRef`](#correspond.errors.InvalidRef)), an
  operation the channel does not have ([`NotSupported`](#correspond.errors.NotSupported)). A retry changes nothing.
- **Failures of the platform or of the message** are [`ChannelError`](#correspond.errors.ChannelError) inside an
  adapter, carrying an `kind` from [`ERROR_KINDS`](#correspond.errors.ERROR_KINDS), `retryable` and
  `retry_after`. Reads let them propagate; the write verbs turn them into a
  `SendResult` with `ok=False`, so a notifier never crashes its caller.

```pycon
>>> str(NotSupported("react", "ntfy"))
'ntfy does not support react'
>>> error = ChannelError("slow down", kind="rate_limited", retryable=True, retry_after=30)
>>> error.kind, error.retryable, error.retry_after
('rate_limited', True, 30.0)
```

### Module Attributes

| [`ERROR_KINDS`](#correspond.errors.ERROR_KINDS)   | Why a platform call failed, for a caller deciding whether to retry, fix the draft, or ask a human.   |
|----------------------------------------------------------------|------------------------------------------------------------------------------------------------------|

### Exceptions

| [`ChannelError`](#correspond.errors.ChannelError)(message, \*, kind[, retryable, ...])   | A platform call that failed, classified so the caller can decide what to do.      |
|------------------------------------------------------------------------------------------------------|-----------------------------------------------------------------------------------|
| [`CorrespondError`](#correspond.errors.CorrespondError)                                     | An expected failure, with a message meant for the person or agent that asked.     |
| [`InvalidRef`](#correspond.errors.InvalidRef)                                          | A conversation reference that does not parse, or that its channel rejects.        |
| [`MissingRequirement`](#correspond.errors.MissingRequirement)(channel, missing, \*, fix)       | Something the channel needs is not here: a credential, a binary, an extra, an OS. |
| [`NotSupported`](#correspond.errors.NotSupported)(operation, channel, \*[, ...])         | The channel does not have this operation (or this feature of it).                 |
| [`UnknownChannel`](#correspond.errors.UnknownChannel)(channel, \*[, known, hint])          | A channel name that no registered adapter answers to.                             |

### *exception* correspond.errors.ChannelError(message, , kind, retryable=False, retry_after=None)

Bases: [`CorrespondError`](#correspond.errors.CorrespondError)

A platform call that failed, classified so the caller can decide what to do.

### *exception* correspond.errors.CorrespondError

Bases: [`Exception`](https://docs.python.org/3/builtins/exceptions.html#Exception)

An expected failure, with a message meant for the person or agent that asked.

### correspond.errors.ERROR_KINDS *= ('auth', 'permission', 'not_found', 'rate_limited', 'network', 'validation', 'unavailable')*

Why a platform call failed, for a caller deciding whether to retry, fix the draft, or ask a human.

### *exception* correspond.errors.InvalidRef

Bases: [`CorrespondError`](#correspond.errors.CorrespondError), [`ValueError`](https://docs.python.org/3/builtins/exceptions.html#ValueError)

A conversation reference that does not parse, or that its channel rejects.

### *exception* correspond.errors.MissingRequirement(channel, missing, , fix, kind='unavailable')

Bases: [`ChannelError`](#correspond.errors.ChannelError)

Something the channel needs is not here: a credential, a binary, an extra, an OS.

### *exception* correspond.errors.NotSupported(operation, channel, , alternatives=())

Bases: [`CorrespondError`](#correspond.errors.CorrespondError)

The channel does not have this operation (or this feature of it). Never a silent no-op.

### *exception* correspond.errors.UnknownChannel(channel, , known=(), hint='')

Bases: [`CorrespondError`](#correspond.errors.CorrespondError), [`LookupError`](https://docs.python.org/3/builtins/exceptions.html#LookupError)

A channel name that no registered adapter answers to.
