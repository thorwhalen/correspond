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
import json
import os
import re
import stat
import warnings
from collections.abc import Iterator, MutableMapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

from correspond import settings

__all__ = [
    "ArrivalStamps",
    "QuotedKeys",
    "SafeKeys",
    "SendStore",
    "arrival_stamps",
    "bytes_store",
    "cursor_store",
    "json_store",
    "kind_dir",
    "private_dir",
    "send_store",
    "text_store",
]

_KIND_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._@+=%-]+$")


#: A state folder is the owner's only: it holds messages, tokens' cursors and reports.
PRIVATE_DIR_MODE = 0o700
#: The data-root folder that holds the files writers lock to take turns.
LOCKS_KIND = "locks"
#: Digits of a stored arrival stamp (nanoseconds since the epoch fit in 20).
STAMP_DIGITS = 20


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


def private_dir(kind: str, *, data_dir: str | os.PathLike | None = None) -> Path:
    """The folder for one kind of state, created owner-only, and tightened to owner-only if it was not.

    A folder made by an older correspond kept the mode the umask gave it; the first use
    since takes the group's and others' access away. A folder this user does not own is
    left as it is, with a warning. (Windows has no such modes: nothing changes there.)
    """
    folder = kind_dir(kind, data_dir=data_dir)
    folder.mkdir(mode=PRIVATE_DIR_MODE, parents=True, exist_ok=True)
    if os.name == "posix":
        mode = stat.S_IMODE(folder.stat().st_mode)
        if mode & ~PRIVATE_DIR_MODE:
            try:
                folder.chmod(mode & PRIVATE_DIR_MODE)
            except OSError as error:
                warnings.warn(
                    f"the state folder {folder} is open to others (mode {mode:o}) and could not be made owner-only: {error}",
                    stacklevel=2,
                )
    return folder


def _files(kind: str, cls_name: str, data_dir) -> MutableMapping:
    import dol

    folder = private_dir(kind, data_dir=data_dir)
    root = str(folder) + os.sep
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
    *, data_dir: str | os.PathLike | None = None, kind: str = "cursors"
) -> MutableMapping[str, str]:
    """Listen cursors, keyed by encoded conversation reference, under ``<data root>/<kind>/``.

    A consumer that must not take events from another (the Claude Code channel server, next
    to manual ``correspond listen`` calls) keeps its own ``kind``.
    """
    return QuotedKeys(text_store(kind, data_dir=data_dir), suffix=".txt")


class ArrivalStamps:
    """Strictly increasing arrival stamps (nanoseconds) shared by every process that opens one file.

    Writers that put events in one store, keyed by arrival so readers can keep a cursor
    (the Slack Events API receiver and Socket Mode listens), take their stamps here.
    :meth:`issue` holds an exclusive lock on the file for the whole block, so the writer
    that took a stamp also stores its event before any other writer takes the next one:
    a reader that saw a stamp has seen every event stamped below it, and no event lands
    below a cursor already handed out. The lock is the operating system's (``flock``, or
    ``msvcrt.locking`` on Windows): it orders the processes of one machine, not a store
    shared between machines.

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
    """

    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)

    @contextmanager
    def issue(self, now_ns: int, *, floor: int = 0) -> Iterator[int]:
        """Hold the lock and yield a stamp above ``now_ns - 1``, ``floor`` and every stamp issued before."""
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(fd, "r+b") as file:
            release = _lock(file)
            try:
                file.seek(0)
                text = file.read(STAMP_DIGITS).decode("ascii", "replace")
                last = int(text) if text.isdigit() else 0
                stamp = max(int(now_ns), last + 1, floor + 1)
                file.seek(0)
                file.write(f"{stamp:0{STAMP_DIGITS}d}".encode("ascii"))
                file.flush()
                yield stamp
            finally:
                release()


def _lock(file) -> Any:
    """Take an exclusive lock on ``file`` (waiting for it); return the function that releases it."""
    if os.name == "nt":
        import msvcrt

        file.seek(0)
        msvcrt.locking(file.fileno(), msvcrt.LK_LOCK, 1)

        def release():
            file.seek(0)
            msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)

        return release
    import fcntl

    fcntl.flock(file.fileno(), fcntl.LOCK_EX)
    return lambda: fcntl.flock(file.fileno(), fcntl.LOCK_UN)


def arrival_stamps(
    kind: str, *, data_dir: str | os.PathLike | None = None
) -> ArrivalStamps:
    """The arrival stamps of the ``kind`` store: ``<data root>/locks/<kind>.stamp``."""
    if not _KIND_RE.match(kind):
        raise ValueError(f"store kind {kind!r} must be a lowercase name")
    return ArrivalStamps(private_dir(LOCKS_KIND, data_dir=data_dir) / f"{kind}.stamp")


class SendStore(MutableMapping):
    """Idempotency records keyed by any key, each in ``<folder>/<sha256 of the key>.json``, with an exclusive :meth:`claim`.

    Hashing keeps every file name short and safe whatever the key (the record keeps the key
    itself). The record file is its own claim: :meth:`claim` creates it with an exclusive
    create and writes the record through the same handle, so of two processes claiming one
    key at once exactly one wins, and no crash can leave a claim without a record. A key
    whose record says ``failed`` may be claimed again. A record that cannot be read (a
    crash while it was written) reads as an attempt of unknown outcome, dated by its file.

    This is the contract :func:`correspond.send` relies on: a ``sends=`` mapping with a
    ``claim(key, record) -> bool`` is exclusive across processes; a plain mapping is
    checked, then set, which covers one process only.
    """

    def __init__(self, folder: str | os.PathLike):
        self.folder = os.fspath(folder)

    @staticmethod
    def name(key: str) -> str:
        """The file name that holds ``key``'s record."""
        if not isinstance(key, str) or not key:
            raise KeyError(f"key must be a non-empty string, not {key!r}")
        return hashlib.sha256(key.encode("utf-8")).hexdigest() + ".json"

    def _path(self, key: str) -> str:
        return os.path.join(self.folder, self.name(key))

    def _read(self, path: str, key: Any) -> dict:
        try:
            with open(path, encoding="utf-8") as file:
                return json.load(file)
        except FileNotFoundError:
            raise KeyError(key) from None
        except (OSError, ValueError):
            from datetime import datetime, timezone

            moment = datetime.fromtimestamp(os.path.getmtime(path), timezone.utc)
            return {"key": key, "state": "attempted", "attempted_at": moment.isoformat()}

    def __getitem__(self, key):
        return self._read(self._path(key), key)

    def __setitem__(self, key, value):
        os.makedirs(self.folder, mode=PRIVATE_DIR_MODE, exist_ok=True)
        path = self._path(key)
        temporary = f"{path}.{os.getpid()}.tmp"
        with open(temporary, "w", encoding="utf-8") as file:
            json.dump(value, file)
        os.replace(temporary, path)  # atomic: a reader never sees half a record

    def __delitem__(self, key):
        try:
            os.remove(self._path(key))
        except FileNotFoundError:
            raise KeyError(key) from None

    def __iter__(self) -> Iterator[str]:
        if not os.path.isdir(self.folder):
            return
        for name in sorted(os.listdir(self.folder)):
            if name.endswith(".json"):
                yield self._read(os.path.join(self.folder, name), name).get("key", name)

    def __len__(self) -> int:
        if not os.path.isdir(self.folder):
            return 0
        return sum(1 for name in os.listdir(self.folder) if name.endswith(".json"))

    def __contains__(self, key) -> bool:
        try:
            return os.path.exists(self._path(key))
        except KeyError:
            return False

    def claim(self, key: str, record: Any) -> bool:
        """Store ``record`` for ``key`` unless a record holds it (one saying ``failed`` does not); False, storing nothing, if one does."""
        os.makedirs(self.folder, mode=PRIVATE_DIR_MODE, exist_ok=True)
        path = self._path(key)
        try:
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if self._read(path, key).get("state") != "failed":
                return False
            self[key] = record
            return True
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            json.dump(record, file)
        return True


def send_store(*, data_dir: str | os.PathLike | None = None) -> SendStore:
    """What each idempotency key of :func:`correspond.send` did, under ``<data root>/sends/``."""
    return SendStore(kind_dir("sends", data_dir=data_dir))
