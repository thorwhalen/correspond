# correspond.stores

The state stores: listen cursors, the web inbox’s reports and blobs, the Telegram log.

Each is a `MutableMapping` over `dol` files under the data root, one folder per kind
(`~/.local/share/correspond/<kind>/`), never inside a repository. Anything with the same
interface replaces one: a `dict` in tests, an `s3dol` store on a server. `dol` is
imported when a store is first made, not when correspond is imported.

Keys are relative paths whose segments are plain names; nothing can address a file
outside the store’s folder:

```pycon
>>> safe = SafeKeys({})
>>> safe["site/20260911T120000Z-1.json"] = {"text": "hi"}
>>> list(safe)
['site/20260911T120000Z-1.json']
>>> safe["../elsewhere"] = {}
Traceback (most recent call last):
...
KeyError: "store key '../elsewhere' must be /-separated names of letters, digits, '.', '_', '-', '@', '+', '=' or '%' (no '.' or '..' segments)"
```

### Functions

| [`arrival_stamps`](#correspond.stores.arrival_stamps)(kind, \*[, data_dir])   | The arrival stamps of the `kind` store: `<data root>/locks/<kind>.stamp`.                                                                        |
|-----------------------------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------|
| [`bytes_store`](#correspond.stores.bytes_store)(kind, \*[, data_dir])      | Binary files under `<data root>/<kind>/`.                                                                                                        |
| [`cursor_store`](#correspond.stores.cursor_store)(\*[, data_dir, kind])     | Listen cursors, keyed by encoded conversation reference, under `<data root>/<kind>/`.                                                            |
| [`json_store`](#correspond.stores.json_store)(kind, \*[, data_dir])       | JSON files under `<data root>/<kind>/` (keys end in `.json`).                                                                                    |
| [`kind_dir`](#correspond.stores.kind_dir)(kind, \*[, data_dir])         | The folder for one kind of state under the data root.                                                                                            |
| [`private_dir`](#correspond.stores.private_dir)(kind, \*[, data_dir])      | The folder for one kind of state, created owner-only, and tightened to owner-only if it was not.                                                 |
| [`send_store`](#correspond.stores.send_store)(\*[, data_dir])             | What each idempotency key of [`correspond.send()`](correspond.html.md#correspond.send) did, under `<data root>/sends/`. |
| [`text_store`](#correspond.stores.text_store)(kind, \*[, data_dir])       | Text files under `<data root>/<kind>/`.                                                                                                          |

### Classes

| [`ArrivalStamps`](#correspond.stores.ArrivalStamps)(path)             | Strictly increasing arrival stamps (nanoseconds) shared by every process that opens one file.                   |
|----------------------------------------------------------------------------------|-----------------------------------------------------------------------------------------------------------------|
| [`QuotedKeys`](#correspond.stores.QuotedKeys)(store, \*[, suffix]) | Any string key, kept under a percent-encoded file-safe name (plus `suffix`) in a flat mapping.                  |
| [`SafeKeys`](#correspond.stores.SafeKeys)(store)                 | A mapping whose keys must be relative paths of plain names (no `..`, no absolute paths).                        |
| [`SendStore`](#correspond.stores.SendStore)(folder)               | Idempotency records keyed by any key, each in `<folder>/<sha256 of the key>.json`, with an exclusive `claim()`. |

### *class* correspond.stores.ArrivalStamps(path)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

Strictly increasing arrival stamps (nanoseconds) shared by every process that opens one file.

Writers that put events in one store, keyed by arrival so readers can keep a cursor
(the Slack Events API receiver and Socket Mode listens), take their stamps here.
[`issue()`](#correspond.stores.ArrivalStamps.issue) holds an exclusive lock on the file for the whole block, so the writer
that took a stamp also stores its event before any other writer takes the next one,
and a reader lists the store under [`hold()`](#correspond.stores.ArrivalStamps.hold): what it lists is whole, it has seen
every event stamped below the last it saw, and no event lands below a cursor already
handed out. The lock is the operating system’s (`flock`, or
`msvcrt.locking` on Windows): it orders the processes of one machine, not a store
shared between machines.

```pycon
>>> import tempfile
>>> stamps = ArrivalStamps(Path(tempfile.mkdtemp()) / "demo.stamp")
>>> with stamps.issue(5) as first:  # store the event here, under the lock
...     pass
>>> with stamps.issue(3) as second:  # a clock behind the last stamp
...     pass
>>> with stamps.issue(4, floor=9) as third:
...     pass
>>> first, second, third
(5, 6, 10)
```

#### hold()

Hold the lock without taking a stamp: a reader lists the store here, so every event it lists is whole.

* **Return type:**
  [`Iterator`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Iterator)[[`None`](https://docs.python.org/3/builtins/constants.html#None)]

#### issue(now_ns, , floor=0)

Hold the lock and yield a stamp above `now_ns - 1`, `floor` and every stamp issued before.

* **Return type:**
  [`Iterator`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Iterator)[[`int`](https://docs.python.org/3/builtins/functions.html#int)]

### *class* correspond.stores.QuotedKeys(store, , suffix='')

Bases: [`MutableMapping`](https://docs.python.org/3/library/collections.abc.html#collections.abc.MutableMapping)

Any string key, kept under a percent-encoded file-safe name (plus `suffix`) in a flat mapping.

```pycon
>>> cursors = QuotedKeys({}, suffix=".txt")
>>> cursors["github:octocat/hello-world#1"] = "2026-09-11T12:00:00Z"
>>> list(cursors), list(cursors.store)
(['github:octocat/hello-world#1'], ['github%3Aoctocat%2Fhello-world%231.txt'])
```

### *class* correspond.stores.SafeKeys(store)

Bases: [`MutableMapping`](https://docs.python.org/3/library/collections.abc.html#collections.abc.MutableMapping)

A mapping whose keys must be relative paths of plain names (no `..`, no absolute paths).

#### *static* check(key)

`key` if it is a safe relative path, else `KeyError`.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

### *class* correspond.stores.SendStore(folder)

Bases: [`MutableMapping`](https://docs.python.org/3/library/collections.abc.html#collections.abc.MutableMapping)

Idempotency records keyed by any key, each in `<folder>/<sha256 of the key>.json`, with an exclusive [`claim()`](#correspond.stores.SendStore.claim).

Hashing keeps every file name short and safe whatever the key (the record keeps the key
itself). The record file is its own claim: [`claim()`](#correspond.stores.SendStore.claim) creates it with an exclusive
create and writes the record through the same handle, so of two processes claiming one
key at once exactly one wins, and no crash can leave a claim without a record. A key
whose record says `failed` may be claimed again. A record that cannot be read (a
crash while it was written) reads as an attempt of unknown outcome, dated by its file.

This is the contract [`correspond.send()`](correspond.html.md#correspond.send) relies on: a `sends=` mapping with a
`claim(key, record) -> bool` is exclusive across processes; a plain mapping is
checked, then set, which covers one process only.

#### claim(key, record)

Store `record` for `key` unless a record holds it (one saying `failed` does not); False, storing nothing, if one does.

* **Return type:**
  [`bool`](https://docs.python.org/3/builtins/functions.html#bool)

#### *static* name(key)

The file name that holds `key`’s record.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

### correspond.stores.arrival_stamps(kind, , data_dir=None)

The arrival stamps of the `kind` store: `<data root>/locks/<kind>.stamp`.

* **Return type:**
  [`ArrivalStamps`](#correspond.stores.ArrivalStamps)

### correspond.stores.bytes_store(kind, , data_dir=None)

Binary files under `<data root>/<kind>/`.

* **Return type:**
  [`MutableMapping`](https://docs.python.org/3/library/collections.abc.html#collections.abc.MutableMapping)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`bytes`](https://docs.python.org/3/builtins/stdtypes.html#bytes)]

### correspond.stores.cursor_store(, data_dir=None, kind='cursors')

Listen cursors, keyed by encoded conversation reference, under `<data root>/<kind>/`.

A consumer that must not take events from another (the Claude Code channel server, next
to manual `correspond listen` calls) keeps its own `kind`.

* **Return type:**
  [`MutableMapping`](https://docs.python.org/3/library/collections.abc.html#collections.abc.MutableMapping)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]

### correspond.stores.json_store(kind, , data_dir=None)

JSON files under `<data root>/<kind>/` (keys end in `.json`).

* **Return type:**
  [`MutableMapping`](https://docs.python.org/3/library/collections.abc.html#collections.abc.MutableMapping)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`Any`](https://docs.python.org/3/library/typing.html#typing.Any)]

### correspond.stores.kind_dir(kind, , data_dir=None)

The folder for one kind of state under the data root.

### correspond.stores.private_dir(kind, , data_dir=None)

The folder for one kind of state, created owner-only, and tightened to owner-only if it was not.

A folder made by an older correspond kept the mode the umask gave it; the first use
since takes the group’s and others’ access away. A folder another user owns is left
as it is, with a warning, and so is a symbolic link (and what it points to). Only the
kind’s folder is tightened, not the data root above it. (Windows has no such modes:
nothing changes there.)

* **Return type:**
  [`Path`](https://docs.python.org/3/library/pathlib.html#pathlib.Path)

### correspond.stores.send_store(, data_dir=None)

What each idempotency key of [`correspond.send()`](correspond.html.md#correspond.send) did, under `<data root>/sends/`.

* **Return type:**
  [`SendStore`](#correspond.stores.SendStore)

### correspond.stores.text_store(kind, , data_dir=None)

Text files under `<data root>/<kind>/`.

* **Return type:**
  [`MutableMapping`](https://docs.python.org/3/library/collections.abc.html#collections.abc.MutableMapping)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]
