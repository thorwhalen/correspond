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

import re
import subprocess
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from typing import Any

from correspond.errors import ChannelError, InvalidRef, MissingRequirement
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
#: Google-native files (Docs, Sheets, Slides...) have no bytes of their own to download.
NATIVE_MIME_PREFIX = "application/vnd.google-apps."
#: How deep ``read`` follows subfolders.
MAX_DEPTH = 5
_PLATFORM = Authenticity(
    grade=Grade.PLATFORM,
    evidence={"attested_by": "the Google Drive API (the file's last modifying user)"},
)


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
            history_depth=HistoryDepth.NONE,
            grades=(Grade.PLATFORM,),
            native_fields=("path", "mime_type", "file_id", "md5", "google_native"),
            notes=(
                "read lists the folder and its subfolders (up to 5 deep): one message per file, its text the path in the folder, sent_at its last modification, the file as its attachment",
                "only what is in the folder now: a file deleted or moved out is gone, and a file's earlier versions are not read",
                "a Google Docs, Sheets or Slides file has no bytes of its own: its attachment refuses to download (export is not supported)",
                "read only: correspond asks for the drive.readonly scope and never writes to Drive",
                "audience is the folder's sharing: anyone (with the link) is public, a domain is org, otherwise the people it is shared with; a group's members are not listed",
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
            complete=not classes,
            classes=classes,
            external=True if scope is Scope.PUBLIC else None,
            durability=("archived_by_others",),
            widening=("visibility_flip", "forwarding"),
            evidence=evidence or ("Drive listed no sharing on the folder",),
        )

    # ------------------------------------------------------------------ reading

    def _items(
        self, folder_id: str, prefix: str = "", depth: int = 0
    ) -> Iterator[tuple[str, Any]]:
        """``(path in the folder, Drive file resource)`` for every file, depth first."""
        query = f"'{folder_id}' in parents and trashed=false"
        try:
            items = self.drive.ListFile({"q": query}).GetList()
        except ChannelError:
            raise
        except Exception as error:  # pydrive2's ApiRequestError and friends
            text = str(error)
            kind = (
                "not_found"
                if "404" in text or "notFound" in text
                else "permission"
                if "403" in text
                else "unavailable"
            )
            raise ChannelError(
                f"Google Drive could not list folder {folder_id}: {text}", kind=kind
            ) from None
        for item in items:
            path = f"{prefix}{item['title']}"
            if item.get("mimeType") == FOLDER_MIME:
                if depth < self.max_depth:
                    yield from self._items(item["id"], f"{path}/", depth + 1)
                continue
            yield path, item

    def _download(self, file_id: str) -> bytes:
        from pydrivedol import get_bytes

        try:
            return get_bytes(file_id, drive=self.drive)
        except Exception as error:
            raise ChannelError(
                f"Google Drive could not download {file_id}: {error}", kind="unavailable"
            ) from None

    def _message(self, conversation: ConversationRef, path: str, item: Any) -> Message:
        file_id = str(item["id"])
        mime = str(item.get("mimeType") or "application/octet-stream")
        native = mime.startswith(NATIVE_MIME_PREFIX)
        size = item.get("fileSize")

        def load(file_id=file_id, native=native, path=path) -> bytes:
            if native:
                raise ChannelError(
                    f"{path} is a Google {mime.rsplit('.', 1)[-1]} file, which has no bytes of its own; export it from Drive",
                    kind="validation",
                )
            return self._download(file_id)

        user = item.get("lastModifyingUser") or {}
        modified = parse_time(item.get("modifiedDate")) or parse_time(
            item.get("createdDate")
        )
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
            sent_at=modified or datetime.fromtimestamp(0, tz=timezone.utc),
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
                "mime_type": mime,
                "file_id": file_id,
                "md5": item.get("md5Checksum"),
                "google_native": native,
            },
        )

    def read(
        self,
        ref: ConversationRef,
        *,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[Message]:
        """The folder's files, oldest modification first, one message each."""
        messages = [self._message(ref, path, item) for path, item in self._items(ref.id)]
        return window(messages, since=since, limit=limit)
