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

| [`bytes_store`](#correspond.stores.bytes_store)(kind, \*[, data_dir])   | Binary files under `<data root>/<kind>/`.                                              |
|--------------------------------------------------------------------------------------|----------------------------------------------------------------------------------------|
| [`cursor_store`](#correspond.stores.cursor_store)(\*[, data_dir])        | Listen cursors, keyed by encoded conversation reference, under `<data root>/cursors/`. |
| [`json_store`](#correspond.stores.json_store)(kind, \*[, data_dir])    | JSON files under `<data root>/<kind>/` (keys end in `.json`).                          |
| [`kind_dir`](#correspond.stores.kind_dir)(kind, \*[, data_dir])      | The folder for one kind of state under the data root.                                  |
| [`text_store`](#correspond.stores.text_store)(kind, \*[, data_dir])    | Text files under `<data root>/<kind>/`.                                                |

### Classes

| [`QuotedKeys`](#correspond.stores.QuotedKeys)(store, \*[, suffix])   | Any string key, kept under a percent-encoded file-safe name (plus `suffix`) in a flat mapping.   |
|------------------------------------------------------------------------------------|--------------------------------------------------------------------------------------------------|
| [`SafeKeys`](#correspond.stores.SafeKeys)(store)                   | A mapping whose keys must be relative paths of plain names (no `..`, no absolute paths).         |

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

### correspond.stores.bytes_store(kind, , data_dir=None)

Binary files under `<data root>/<kind>/`.

* **Return type:**
  [`MutableMapping`](https://docs.python.org/3/library/collections.abc.html#collections.abc.MutableMapping)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`bytes`](https://docs.python.org/3/builtins/stdtypes.html#bytes)]

### correspond.stores.cursor_store(, data_dir=None)

Listen cursors, keyed by encoded conversation reference, under `<data root>/cursors/`.

* **Return type:**
  [`MutableMapping`](https://docs.python.org/3/library/collections.abc.html#collections.abc.MutableMapping)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]

### correspond.stores.json_store(kind, , data_dir=None)

JSON files under `<data root>/<kind>/` (keys end in `.json`).

* **Return type:**
  [`MutableMapping`](https://docs.python.org/3/library/collections.abc.html#collections.abc.MutableMapping)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`Any`](https://docs.python.org/3/library/typing.html#typing.Any)]

### correspond.stores.kind_dir(kind, , data_dir=None)

The folder for one kind of state under the data root.

### correspond.stores.text_store(kind, , data_dir=None)

Text files under `<data root>/<kind>/`.

* **Return type:**
  [`MutableMapping`](https://docs.python.org/3/library/collections.abc.html#collections.abc.MutableMapping)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]
