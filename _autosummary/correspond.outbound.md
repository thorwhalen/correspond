# correspond.outbound

The `before_send` check: what every write runs, with the conversation’s audience, before anything leaves.

A check is a callable:

```default
before_send(ref, draft, audience, *, operation, dry_run, message_id, **context) -> None
```

`operation` is `send`, `edit`, `react` or `upload`; `message_id` is the message
an edit or a reaction targets (`None` otherwise); a reaction’s draft text is the reaction,
an upload’s is the file name. Accept `**context`: later versions may pass more. Returning
lets the write go ahead; raising [`Refused`](correspond.errors.md#correspond.errors.Refused) blocks it; raising
[`NeedsApproval`](correspond.errors.md#correspond.errors.NeedsApproval) holds it for the operator; either may carry keyword
details, which go into the plan. The write verbs of [`correspond.ops`](correspond.ops.md#module-correspond.ops) compute the
audience, run the check once on the real path or on the dry run, and put both into the plan,
so a dry run shows the verdict a send would get.

Which check runs:

1. the `before_send=` argument, when a Python caller passes one;
2. else `before_send = "module:attr"` at the top of the config file, imported when a write
   first needs it. A value that does not import, or is not callable, stops every write
   (`before_send_unavailable`): a configured check that cannot run is never skipped. So
   does a `before_send` key inside a table, where TOML puts a line written after one;
3. else [`notice()`](#correspond.outbound.notice), which lets the write go ahead: the audience line it adds to the plan
   is the whole of it.

Anything else the check does (raise another exception, exit, return a value, stop with an
`error_kind` outside [`CHECK_KINDS`](correspond.errors.md#correspond.errors.CHECK_KINDS)) also stops the write
(`before_send_failed`). liaise supplies a real check (`liaise.vet.before_send`).

The command line and the MCP tools take no `before_send` argument. The check guards
drafts, not the process running correspond: whoever sets its environment or edits the
config file (`$CORRESPOND_CONFIG` can name another file) chooses the check, and a write
made without correspond never meets it. liaise’s hook covers those.

```pycon
>>> resolve(None, config={})[1]
'correspond.outbound:notice'
>>> resolve(None, config={"before_send": "no.such.module:check"})
Traceback (most recent call last):
    ...
correspond.errors.BeforeSendUnavailable: before_send = 'no.such.module:check' in the config does not import (ModuleNotFoundError: No module named 'no'), so nothing is sent
```

### Module Attributes

| [`BeforeSend`](#correspond.outbound.BeforeSend)   | returns `None` to let the write go ahead, raises `Refused` or `NeedsApproval` to stop it.   |
|---------------------------------------------------------------|---------------------------------------------------------------------------------------------|
| [`CONFIG_KEY`](#correspond.outbound.CONFIG_KEY)   | The top-level key of the config file naming the check, as `"module:attr"`.                  |
| [`DEFAULT`](#correspond.outbound.DEFAULT)      | The check that runs when neither the argument nor the config names one.                     |

### Functions

| [`check`](#correspond.outbound.check)(ref, draft, \*[, operation, dry_run, ...])   | Compute the audience, run the check, and return the plan lines with what stopped the write (`None`: it may go ahead).                             |
|-----------------------------------------------------------------------------------------------------|---------------------------------------------------------------------------------------------------------------------------------------------------|
| [`notice`](#correspond.outbound.notice)(ref, draft, audience, \*\*context)          | The default check: every write goes ahead, and its plan and summary say who can read it.                                                          |
| [`resolve`](#correspond.outbound.resolve)([before_send, config])                     | The check to run and the name it is reported by: the argument, else the config's, else [`notice()`](#correspond.outbound.notice). |

### correspond.outbound.BeforeSend

returns `None` to let the write go ahead, raises `Refused` or `NeedsApproval` to stop it.

* **Type:**
  A `before_send` check, called `(ref, draft, audience, *, operation, dry_run, message_id)`

alias of [`Callable`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Callable)[[…], [`None`](https://docs.python.org/3/builtins/constants.html#None)]

### correspond.outbound.CONFIG_KEY *= 'before_send'*

The top-level key of the config file naming the check, as `"module:attr"`.

### correspond.outbound.DEFAULT *= 'correspond.outbound:notice'*

The check that runs when neither the argument nor the config names one.

### correspond.outbound.check(ref, draft, , operation='send', dry_run=False, message_id=None, before_send=None, registry=None)

Compute the audience, run the check, and return the plan lines with what stopped the write (`None`: it may go ahead).

The audience is computed first, so a write that cannot be checked still shows who it
would have reached. Never raises for anything the check does (`KeyboardInterrupt`
aside, which stops everything).

* **Return type:**
  [`tuple`](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict), [`Stopped`](correspond.errors.md#correspond.errors.Stopped) | [`None`](https://docs.python.org/3/builtins/constants.html#None)]

### correspond.outbound.notice(ref, draft, audience, \*\*context)

The default check: every write goes ahead, and its plan and summary say who can read it.

* **Return type:**
  [`None`](https://docs.python.org/3/builtins/constants.html#None)

### correspond.outbound.resolve(before_send=None, , config=None)

The check to run and the name it is reported by: the argument, else the config’s, else [`notice()`](#correspond.outbound.notice).

Raises [`BeforeSendUnavailable`](correspond.errors.md#correspond.errors.BeforeSendUnavailable) when the config names a check
that cannot be loaded, or cannot itself be read.

* **Return type:**
  [`tuple`](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[`Callable`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Callable)[[`...`](https://docs.python.org/3/builtins/constants.html#Ellipsis), [`None`](https://docs.python.org/3/builtins/constants.html#None)], [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]
