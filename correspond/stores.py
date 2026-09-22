"""The state stores: listen cursors, the web inbox's reports and blobs, the Telegram log.

Each is a ``MutableMapping`` over ``dol`` files under the data root, one folder per kind
(``~/.local/share/correspond/<kind>/``), never inside a repository. Anything with the same
interface replaces one: a ``dict`` in tests, an ``s3dol`` store on a server. ``dol`` is
imported when a store is first made, not when correspond is imported.

Keys are relative paths whose segments are plain names; nothing can address a file
outside the store's folder:

>>> safe = SafeKeys({})
>>> safe["site/20260911T120000Z-1.json"] = {"text": "hi"}
>>> list(safe)
['site/20260911T120000Z-1.json']
>>> safe["../elsewhere"] = {}
Traceback (most recent call last):
...
KeyError: "store key '../elsewhere' must be /-separated names of letters, digits, '.', '_', '-', '@', '+', '=' or '%' (no '.' or '..' segments)"
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterator, MutableMapping
from typing import Any
from urllib.parse import quote, unquote

from correspond import settings

__all__ = [
    "QuotedKeys",
    "SafeKeys",
    "SendStore",
    "bytes_store",
    "cursor_store",
    "json_store",
    "kind_dir",
    "send_store",
    "text_store",
]

_KIND_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._@+=%-]+$")


def kind_dir(kind: str, *, data_dir: str | os.PathLike | None = None):
    """The folder for one kind of state under the data root."""
    if not _KIND_RE.match(kind):
        raise ValueError(f"store kind {kind!r} must be a lowercase name")
    return settings.data_dir(data_dir) / kind


class SafeKeys(MutableMapping):
    """A mapping whose keys must be relative paths of plain names (no ``..``, no absolute paths)."""

    def __init__(self, store: MutableMapping):
        self.store = store

    @staticmethod
    def check(key: Any) -> str:
        """``key`` if it is a safe relative path, else ``KeyError``."""
        segments = key.split("/") if isinstance(key, str) else []
        if not segments or any(
            s in (".", "..") or not _SEGMENT_RE.match(s) for s in segments
        ):
            raise KeyError(
                f"store key {key!r} must be /-separated names of letters, digits, '.', '_', '-', '@', '+', '=' or '%' (no '.' or '..' segments)"
            )
        return key

    def __getitem__(self, key):
        return self.store[self.check(key)]

    def __setitem__(self, key, value):
        self.store[self.check(key)] = value

    def __delitem__(self, key):
        del self.store[self.check(key)]

    def __iter__(self) -> Iterator[str]:
        for key in self.store:
            yield key.replace(os.sep, "/") if isinstance(key, str) else key

    def __len__(self) -> int:
        return len(self.store)

    def __contains__(self, key) -> bool:
        try:
            return self.check(key) in self.store
        except KeyError:
            return False


class QuotedKeys(MutableMapping):
    """Any string key, kept under a percent-encoded file-safe name (plus ``suffix``) in a flat mapping.

    >>> cursors = QuotedKeys({}, suffix=".txt")
    >>> cursors["github:octocat/hello-world#1"] = "2026-09-11T12:00:00Z"
    >>> list(cursors), list(cursors.store)
    (['github:octocat/hello-world#1'], ['github%3Aoctocat%2Fhello-world%231.txt'])
    """

    def __init__(self, store: MutableMapping, *, suffix: str = ""):
        self.store = store
        self.suffix = suffix

    def _id(self, key: str) -> str:
        if not isinstance(key, str) or not key:
            raise KeyError(f"key must be a non-empty string, not {key!r}")
        return quote(key, safe="") + self.suffix

    def __getitem__(self, key):
        return self.store[self._id(key)]

    def __setitem__(self, key, value):
        self.store[self._id(key)] = value

    def __delitem__(self, key):
        del self.store[self._id(key)]

    def __iter__(self) -> Iterator[str]:
        for name in self.store:
            if isinstance(name, str) and name.endswith(self.suffix) and "/" not in name:
                yield unquote(
                    name[: len(name) - len(self.suffix)] if self.suffix else name
                )

    def __len__(self) -> int:
        return sum(1 for _ in self)

    def __contains__(self, key) -> bool:
        try:
            return self._id(key) in self.store
        except KeyError:
            return False


def _files(kind: str, cls_name: str, data_dir) -> MutableMapping:
    import dol

    root = str(kind_dir(kind, data_dir=data_dir)) + os.sep
    return SafeKeys(dol.mk_dirs_if_missing(getattr(dol, cls_name)(root)))


def text_store(
    kind: str, *, data_dir: str | os.PathLike | None = None
) -> MutableMapping[str, str]:
    """Text files under ``<data root>/<kind>/``."""
    return _files(kind, "TextFiles", data_dir)


def json_store(
    kind: str, *, data_dir: str | os.PathLike | None = None
) -> MutableMapping[str, Any]:
    """JSON files under ``<data root>/<kind>/`` (keys end in ``.json``)."""
    return _files(kind, "JsonFiles", data_dir)


def bytes_store(
    kind: str, *, data_dir: str | os.PathLike | None = None
) -> MutableMapping[str, bytes]:
    """Binary files under ``<data root>/<kind>/``."""
    return _files(kind, "Files", data_dir)


def cursor_store(
    *, data_dir: str | os.PathLike | None = None
) -> MutableMapping[str, str]:
    """Listen cursors, keyed by encoded conversation reference, under ``<data root>/cursors/``."""
    return QuotedKeys(text_store("cursors", data_dir=data_dir), suffix=".txt")


class SendStore(MutableMapping):
    """Idempotency records keyed by any key, each in ``<sha256 of the key>.json``, with an exclusive :meth:`claim`.

    Hashing keeps every file name short and safe whatever the key (the record keeps the key
    itself). :meth:`claim` creates a ``.claim`` marker with an exclusive create, so of two
    processes claiming one key at once exactly one wins; :meth:`release` removes it, so a
    key whose write the platform refused can be claimed again.
    """

    def __init__(self, files: MutableMapping[str, Any], folder: str | os.PathLike):
        self.files, self.folder = files, folder

    @staticmethod
    def name(key: str) -> str:
        """The file stem that holds ``key``'s record."""
        if not isinstance(key, str) or not key:
            raise KeyError(f"key must be a non-empty string, not {key!r}")
        return hashlib.sha256(key.encode("utf-8")).hexdigest()

    def __getitem__(self, key):
        return self.files[self.name(key) + ".json"]

    def __setitem__(self, key, value):
        self.files[self.name(key) + ".json"] = value

    def __delitem__(self, key):
        del self.files[self.name(key) + ".json"]
        self.release(key)

    def __iter__(self) -> Iterator[str]:
        for name in list(self.files):
            if name.endswith(".json"):
                yield self.files[name].get("key", name)

    def __len__(self) -> int:
        return sum(1 for name in self.files if name.endswith(".json"))

    def __contains__(self, key) -> bool:
        try:
            return self.name(key) + ".json" in self.files
        except KeyError:
            return False

    def _marker(self, key: str) -> str:
        return os.path.join(os.fspath(self.folder), self.name(key) + ".claim")

    def claim(self, key: str, record: Any) -> bool:
        """Store ``record`` for ``key`` if no one holds the key; False, storing nothing, if someone does."""
        os.makedirs(self.folder, exist_ok=True)
        try:
            os.close(os.open(self._marker(key), os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        except FileExistsError:
            return False
        self[key] = record
        return True

    def release(self, key: str) -> None:
        """Let ``key`` be claimed again."""
        try:
            os.remove(self._marker(key))
        except FileNotFoundError:
            pass


def send_store(*, data_dir: str | os.PathLike | None = None) -> SendStore:
    """What each idempotency key of :func:`correspond.send` did, under ``<data root>/sends/``."""
    return SendStore(
        json_store("sends", data_dir=data_dir), kind_dir("sends", data_dir=data_dir)
    )
