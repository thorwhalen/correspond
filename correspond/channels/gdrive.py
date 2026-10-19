"""Google Drive: a shared folder, read only, where files that a conversation announces arrive.

A message often says "uploaded, it's the one dated Tuesday" about a file that is not
attached to it: the folder is the payload and the message is the notification. This
channel lets one facade read both ends. It reads, and does nothing else: no listening, no
writing.

Reference: ``gdrive:<folder id>`` (a folder's URL, ``https://drive.google.com/drive/folders/<id>``,
is accepted and normalised to its id).

``read`` returns one :class:`~correspond.model.Message` per file in the folder and its
subfolders: the text is the file's path in the folder, ``sent_at`` its last modification,
the author Drive's last modifying user, and the file itself the message's one
:class:`~correspond.model.Attachment`, fetched only on ``content()``. So ``since`` and
``limit`` mean what they mean everywhere, and "the file Ada mentioned on Tuesday" lines up
with "the file modified on Tuesday". A Google Docs, Sheets or Slides file has no bytes of its
own; its attachment says so when fetched.

Credentials are the host's: a service-account key file named by ``service_account_file``,
used with the read-only Drive scope (share the folder with the account's ``client_email``),
or an authenticated PyDrive2 ``GoogleDrive`` passed as ``drive=``. Needs the ``gdrive``
extra (pydrivedol), imported on first use, not when the channel is listed.

>>> GoogleDrive().parse_ref("https://drive.google.com/drive/folders/0AbCdEfGhIjKlMnOp?usp=sharing").encoded
'gdrive:0AbCdEfGhIjKlMnOp'
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from typing import Any

from correspond.errors import (
    ChannelError,
    CorrespondError,
    InvalidRef,
    MissingRequirement,
)
from correspond.model import (
    Attachment,
    Audience,
    Authenticity,
    Capabilities,
    ChannelIdentity,
    ConversationRef,
    Grade,
    HistoryDepth,
    Message,
    Scope,
    Support,
    parse_time,
)
from correspond.ops import window
from correspond.registry import value

__all__ = ["GoogleDrive", "READONLY_SCOPE"]

NAME = "gdrive"
FOLDER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,128}$")
FOLDER_URL_RE = re.compile(
    r"^https://drive\.google\.com/(?:drive/(?:u/\d+/)?)?folders/(?P<id>[A-Za-z0-9_-]{10,128})(?:[/?#].*)?$"
)
#: The only scope correspond asks for: it never writes to Drive.
READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
#: Google-native files (Docs, Sheets, Slides...) have no bytes of their own to download.
NATIVE_MIME_PREFIX = "application/vnd.google-apps."
#: How deep ``read`` follows subfolders.
MAX_DEPTH = 5
#: The Drive API's HTTP codes, as correspond's error kinds.
_ERROR_KINDS = {401: "auth", 403: "permission", 404: "not_found", 429: "rate_limited"}
_PLATFORM = Authenticity(
    grade=Grade.PLATFORM,
    evidence={"attested_by": "the Google Drive API (the file's last modifying user)"},
)


def _error_kind(error: BaseException) -> str:
    """The kind of a Drive API failure, from its HTTP code (PyDrive2's ``ApiRequestError.error``, or an ``HttpError``'s status)."""
    detail = getattr(error, "error", None)
    code = detail.get("code") if isinstance(detail, dict) else None
    if code is None:
        code = getattr(getattr(error, "resp", None), "status", None)
    try:
        code = int(code)
    except (TypeError, ValueError):
        return "unavailable"
    return _ERROR_KINDS.get(code, "unavailable")


def _time(item: Any, *keys: str) -> datetime:
    """The first of ``keys`` that parses as a time, else the epoch: one odd date does not break a read."""
    for key in keys:
        try:
            moment = parse_time(item.get(key))
        except CorrespondError:
            continue
        if moment is not None:
            return moment
    return datetime.fromtimestamp(0, tz=timezone.utc)


def _folder_id(id: str) -> str:
    """A folder id from an id or a folder URL; :class:`InvalidRef` otherwise."""
    text = (id or "").strip()
    if FOLDER_ID_RE.match(text):
        return text
    match = FOLDER_URL_RE.match(text)
    if match:
        return match["id"]
    raise InvalidRef(
        f"gdrive references are gdrive:<folder id> (or a drive.google.com folder URL), not gdrive:{id}"
    )


class GoogleDrive:
    """A Drive folder, read only: its files as messages, each carrying the file as an attachment."""

    name = NAME

    def __init__(
        self,
        *,
        drive: Any = None,
        run: Callable[..., Any] = subprocess.run,
        max_depth: int = MAX_DEPTH,
    ):
        self.run = run
        self.max_depth = max_depth
        self._drive = drive

    @property
    def drive(self) -> Any:
        """The authenticated PyDrive2 ``GoogleDrive``: the one passed in, else one built from ``service_account_file`` with the read-only scope."""
        if self._drive is None:
            key_file = value(NAME, "service_account_file", run=self.run)
            if not key_file:
                raise MissingRequirement(
                    NAME,
                    "no service_account_file: the path of a Google service-account key (JSON)",
                    fix=(
                        "create a service account with the Drive API enabled, download its key, "
                        "set CORRESPOND_GDRIVE_SERVICE_ACCOUNT_FILE to its path, and share the "
                        "folder with the account's client_email (Viewer)"
                    ),
                )
            try:
                from pydrivedol import drive_from_service_account
            except ImportError:
                raise MissingRequirement(
                    NAME,
                    "pydrivedol is not installed",
                    fix="pip install 'correspond[gdrive]'",
                ) from None
            if not os.path.isfile(key_file):
                raise MissingRequirement(
                    NAME,
                    f"no service-account key at {key_file}",
                    fix="point CORRESPOND_GDRIVE_SERVICE_ACCOUNT_FILE at the key's JSON file",
                    kind="validation",
                )
            try:
                self._drive = drive_from_service_account(
                    key_file, scopes=(READONLY_SCOPE,)
                )
            except Exception as error:  # pydrive2 raises its own auth errors
                raise ChannelError(
                    f"Google refused the service-account key at {key_file}: {error}",
                    kind="auth",
                ) from None
        return self._drive

    @property
    def capabilities(self) -> Capabilities:
        """Read a folder's files, and nothing else."""
        return Capabilities(
            channel=NAME,
            read=Support.FULL,
            audience=Support.PARTIAL,
            history_depth=HistoryDepth.FULL,
            grades=(Grade.PLATFORM,),
            native_fields=(
                "path",
                "path_parts",
                "mime_type",
                "file_id",
                "md5",
                "google_native",
                "shortcut",
            ),
            notes=(
                f"read lists the folder and its subfolders (up to {self.max_depth} deep): one message per file, its text the path in the folder (native path_parts keeps a title holding /), sent_at its last modification, the file as its attachment; a file in several of these folders is listed once",
                "a shortcut is read as the file it points to (native shortcut: true); a shortcut to a folder is followed like a subfolder",
                "only what is in the folder now: a file deleted or moved out is gone, and a file's earlier versions are not read",
                "a Google Docs, Sheets or Slides file has no bytes of its own: its attachment refuses to download (export is not supported)",
                "read only: correspond asks for the drive.readonly scope and never writes to Drive",
                "audience is the folder's sharing: anyone (with the link) is public, a domain is org, otherwise the people it is shared with; never complete (a group's members and a shared drive's members are not listed, and Drive may cut the sharing list); an account that may not list the sharing gets public, defaulted",
            ),
        )

    def parse_ref(self, id: str) -> ConversationRef:
        """A folder id, or a folder URL normalised to its id."""
        return ConversationRef(channel=NAME, id=_folder_id(id), kind="folder")

    # ----------------------------------------------------------------- audience

    def audience(self, ref: ConversationRef, *, draft: Any = None) -> Audience:
        """Who can read the folder: its sharing permissions, asked of Drive now.

        ``anyone`` (with or without the link) makes it public; a domain, the organisation;
        otherwise the users it is shared with, by address, and each group as a class whose
        members are not listed. Anyone who can read a file can download and keep a copy.
        """
        try:
            permissions = self.drive.CreateFile({"id": ref.id}).GetPermissions()
        except Exception as error:
            return Audience.unknown(
                ref.encoded, f"Drive did not list the folder's sharing: {error}"
            )
        readers, classes, evidence = [], [], []
        scope = Scope.NAMED
        for permission in permissions or ():
            kind, role = permission.get("type"), permission.get("role")
            evidence.append(f"shared with {kind} as {role}")
            if kind == "anyone":
                scope = Scope.PUBLIC
                classes.append(
                    "anyone with the link"
                    if permission.get("withLink")
                    else "anyone (the folder is searchable)"
                )
            elif kind == "domain":
                if scope is not Scope.PUBLIC:
                    scope = Scope.ORG
                classes.append(f"everyone at {permission.get('domain')}")
            elif kind == "group":
                classes.append(
                    f"the members of the group {permission.get('emailAddress')}"
                )
            else:
                readers.append(
                    ChannelIdentity(
                        channel=NAME,
                        native_id=str(permission.get("id") or ""),
                        handle=permission.get("emailAddress") or None,
                        display_name=permission.get("name") or None,
                    )
                )
        return Audience(
            ref=ref.encoded,
            scope=scope,
            readers=readers,
            complete=False,
            classes=classes,
            external=True if scope is Scope.PUBLIC else None,
            durability=("archived_by_others",),
            widening=("visibility_flip", "forwarding"),
            evidence=evidence or ("Drive listed no sharing on the folder",),
        )

    # ------------------------------------------------------------------ reading

    def _list(self, folder_id: str, since: datetime | None) -> list[Any]:
        query = f"'{folder_id}' in parents and trashed=false"
        if since is not None:
            # Folders and shortcuts are listed whatever their date: what is inside them may be new.
            stamp = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
            query += (
                f" and (mimeType = '{FOLDER_MIME}' or mimeType = '{SHORTCUT_MIME}'"
                f" or modifiedDate >= '{stamp}')"
            )
        try:
            return list(self.drive.ListFile({"q": query}).GetList())
        except ChannelError:
            raise
        except Exception as error:  # pydrive2's ApiRequestError and friends
            raise ChannelError(
                f"Google Drive could not list folder {folder_id}: {error}",
                kind=_error_kind(error),
            ) from None

    def _items(
        self,
        folder_id: str,
        *,
        since: datetime | None = None,
        parts: tuple[str, ...] = (),
        depth: int = 0,
        seen: set[str] | None = None,
    ) -> Iterator[tuple[tuple[str, ...], Any]]:
        """``(path parts in the folder, Drive file resource)`` for every file, depth first, each file once."""
        seen = set() if seen is None else seen
        for item in self._list(folder_id, since):
            here = (*parts, str(item.get("title") or item["id"]))
            mime = item.get("mimeType")
            target = item.get("shortcutDetails") or {}
            if mime == SHORTCUT_MIME and target.get("targetMimeType") == FOLDER_MIME:
                mime, folder = FOLDER_MIME, target.get("targetId")
            else:
                folder = item["id"]
            if mime == FOLDER_MIME:
                if depth < self.max_depth and folder and folder not in seen:
                    seen.add(folder)
                    yield from self._items(
                        folder, since=since, parts=here, depth=depth + 1, seen=seen
                    )
                continue
            file_id = target.get("targetId") if mime == SHORTCUT_MIME else item["id"]
            if not file_id or file_id in seen:
                continue
            seen.add(file_id)
            yield here, item

    def _download(self, file_id: str) -> bytes:
        """The file's bytes, through the drive's own download (what pydrivedol's ``get_bytes`` does with a drive)."""
        try:
            buffer = self.drive.CreateFile({"id": file_id}).GetContentIOBuffer()
            return b"".join(chunk for chunk in buffer if chunk)
        except ChannelError:
            raise
        except Exception as error:
            raise ChannelError(
                f"Google Drive could not download {file_id}: {error}",
                kind=_error_kind(error),
            ) from None

    def _message(
        self, conversation: ConversationRef, parts: tuple[str, ...], item: Any
    ) -> Message:
        path = "/".join(parts)
        shortcut = item.get("mimeType") == SHORTCUT_MIME
        target = item.get("shortcutDetails") or {}
        file_id = str(target.get("targetId") if shortcut else item["id"])
        mime = str(
            (target.get("targetMimeType") if shortcut else item.get("mimeType"))
            or "application/octet-stream"
        )
        native = mime.startswith(NATIVE_MIME_PREFIX)
        size = None if shortcut else item.get("fileSize")

        def load(file_id=file_id, native=native, path=path) -> bytes:
            if native:
                raise ChannelError(
                    f"{path} is a Google {mime.rsplit('.', 1)[-1]} file, which has no bytes of its own; export it from Drive",
                    kind="validation",
                )
            return self._download(file_id)

        user = item.get("lastModifyingUser") or {}
        return Message(
            id=file_id,
            conversation=conversation,
            author=ChannelIdentity(
                channel=NAME,
                native_id=str(user.get("permissionId") or ""),
                handle=user.get("emailAddress") or None,
                display_name=user.get("displayName")
                or item.get("lastModifyingUserName")
                or None,
                is_self=bool(user.get("isAuthenticatedUser")),
            ),
            authenticity=_PLATFORM,
            sent_at=_time(item, "modifiedDate", "createdDate"),
            text=path,
            url=item.get("alternateLink"),
            attachments=(
                Attachment(
                    ref=f"gdrive-file:{file_id}",
                    media_type=mime,
                    name=item.get("title"),
                    size=int(size) if str(size or "").isdigit() else None,
                    loader=load,
                ),
            ),
            native={
                "path": path,
                "path_parts": list(parts),
                "mime_type": mime,
                "file_id": file_id,
                "md5": None if shortcut else item.get("md5Checksum"),
                "google_native": native,
                "shortcut": shortcut,
            },
        )

    def read(
        self,
        ref: ConversationRef,
        *,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[Message]:
        """The folder's files, oldest modification first, one message each; ``since`` is asked of Drive."""
        messages = [
            self._message(ref, parts, item)
            for parts, item in self._items(ref.id, since=since)
        ]
        return window(messages, since=since, limit=limit)
