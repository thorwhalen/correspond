"""A Google Drive folder over a fake PyDrive2 drive: files as messages with lazy attachments, subfolders, Google-native files, sharing as audience."""

from datetime import datetime, timezone

import pytest

import correspond
from correspond.channels.gdrive import READONLY_SCOPE, GoogleDrive
from correspond.errors import ChannelError, InvalidRef, MissingRequirement

FOLDER = "0AbCdEfGhIjKlMnOp"
SUB = "1SubFolderIdXYZ"


def _file(file_id, title, modified, *, mime="application/pdf", size="8", user="Ada"):
    return {
        "id": file_id,
        "title": title,
        "mimeType": mime,
        "fileSize": size,
        "modifiedDate": modified,
        "md5Checksum": f"md5-{file_id}",
        "alternateLink": f"https://drive.google.com/file/d/{file_id}/view",
        "lastModifyingUser": {
            "displayName": user,
            "emailAddress": f"{user.lower()}@example.org",
            "permissionId": f"p-{user.lower()}",
            "isAuthenticatedUser": False,
        },
    }


class FakeDrive:
    """The PyDrive2 calls the adapter makes: ListFile(...).GetList(), CreateFile(...).GetContentIOBuffer() / GetPermissions()."""

    def __init__(self, folders, contents=None, permissions=None):
        self.folders = folders
        self.contents = contents or {}
        self.permissions = permissions or []
        self.queries, self.downloads = [], []

    def ListFile(self, params):
        self.queries.append(params["q"])
        folder = params["q"].split("'")[1]
        drive = self

        class Listing:
            def GetList(self):
                if folder not in drive.folders:
                    raise RuntimeError("HttpError 404 notFound")
                return list(drive.folders[folder])

        return Listing()

    def CreateFile(self, metadata):
        drive, file_id = self, metadata["id"]

        class File(dict):
            def GetContentIOBuffer(self):
                drive.downloads.append(file_id)
                yield drive.contents[file_id]

            def GetPermissions(self):
                return drive.permissions

        return File(id=file_id)


def _drive():
    return FakeDrive(
        {
            FOLDER: [
                _file("1TuesdayExportFileId0001", "export-tuesday.pdf", "2026-09-15T10:00:00.000Z"),
                {
                    "id": SUB,
                    "title": "raw",
                    "mimeType": "application/vnd.google-apps.folder",
                },
                _file(
                    "1BudgetSheetFileId000003",
                    "Budget",
                    "2026-09-14T09:00:00.000Z",
                    mime="application/vnd.google-apps.spreadsheet",
                    size=None,
                    user="Grace",
                ),
            ],
            SUB: [
                _file("1RawCsvFileIdentifier002", "data.csv", "2026-09-16T08:00:00.000Z", mime="text/csv")
            ],
        },
        contents={"1TuesdayExportFileId0001": b"%PDF-1.7", "1RawCsvFileIdentifier002": b"a,b\n1,2\n"},
    )


def _read(drive, ref=f"gdrive:{FOLDER}", **kwargs):
    return correspond.read(ref, registry={"gdrive": GoogleDrive(drive=drive)}, **kwargs)


def test_a_folder_reads_as_one_message_per_file_with_the_file_attached():
    drive = _drive()
    messages = _read(drive)
    assert [m.text for m in messages] == ["Budget", "export-tuesday.pdf", "raw/data.csv"]
    tuesday = messages[1]
    assert tuesday.sent_at == datetime(2026, 9, 15, 10, tzinfo=timezone.utc)
    assert (
        tuesday.author.display_name == "Ada"
        and tuesday.authenticity.grade.value == "platform"
    )
    assert (
        tuesday.native["path"] == "export-tuesday.pdf"
        and tuesday.native["md5"] == "md5-1TuesdayExportFileId0001"
    )
    (attachment,) = tuesday.attachments
    assert (attachment.name, attachment.size, attachment.media_type) == (
        "export-tuesday.pdf",
        8,
        "application/pdf",
    )
    assert drive.downloads == [], "nothing is downloaded until asked"
    assert attachment.content() == b"%PDF-1.7" and drive.downloads == ["1TuesdayExportFileId0001"]
    assert set(tuesday.native) <= set(GoogleDrive().capabilities.native_fields)


def test_since_and_limit_mean_what_they_mean_everywhere():
    since = datetime(2026, 9, 15, tzinfo=timezone.utc)
    assert [m.text for m in _read(_drive(), since=since)] == [
        "export-tuesday.pdf",
        "raw/data.csv",
    ]
    assert [m.text for m in _read(_drive(), limit=1)] == ["raw/data.csv"]


def test_a_google_native_file_says_it_has_no_bytes_to_download():
    sheet = _read(_drive())[0]
    assert sheet.native["google_native"] and sheet.attachments[0].size is None
    with pytest.raises(ChannelError, match="no bytes of its own"):
        sheet.attachments[0].content()


def test_subfolders_are_followed_only_so_deep():
    drive = _drive()
    shallow = correspond.read(
        f"gdrive:{FOLDER}", registry={"gdrive": GoogleDrive(drive=drive, max_depth=0)}
    )
    assert "raw/data.csv" not in [m.text for m in shallow]


def test_references_take_an_id_or_a_folder_url_and_nothing_else():
    drive = GoogleDrive()
    for given in (
        FOLDER,
        f"https://drive.google.com/drive/folders/{FOLDER}",
        f"https://drive.google.com/drive/u/0/folders/{FOLDER}?usp=sharing",
    ):
        assert drive.parse_ref(given).encoded == f"gdrive:{FOLDER}"
    for bad in (
        "",
        "short",
        f"{FOLDER}' or '1'='1",
        "https://example.org/folders/0AbCdEfGhIjKlMnOp",
    ):
        with pytest.raises(InvalidRef):
            drive.parse_ref(bad)


def test_a_folder_drive_cannot_list_is_not_found():
    with pytest.raises(ChannelError) as caught:
        _read(_drive(), ref="gdrive:0NoSuchFolderAtAll")
    assert caught.value.kind == "not_found"


def test_it_only_reads():
    caps = GoogleDrive().capabilities
    assert caps.operations == ("read", "audience")
    with pytest.raises(correspond.NotSupported):
        correspond.send(
            f"gdrive:{FOLDER}", "hi", registry={"gdrive": GoogleDrive(drive=_drive())}
        )


def test_without_a_drive_or_a_key_file_it_says_how_to_get_one(monkeypatch):
    monkeypatch.delenv("CORRESPOND_GDRIVE_SERVICE_ACCOUNT_FILE", raising=False)
    monkeypatch.setenv("CORRESPOND_CONFIG", "/nonexistent/correspond.toml")
    with pytest.raises(MissingRequirement, match="client_email"):
        GoogleDrive().drive


def test_a_key_file_is_used_with_the_read_only_scope(monkeypatch):
    import pydrivedol

    calls = []
    monkeypatch.setattr(
        pydrivedol,
        "drive_from_service_account",
        lambda key, *, scopes: calls.append((key, scopes)) or "drive",
    )
    monkeypatch.setenv("CORRESPOND_GDRIVE_SERVICE_ACCOUNT_FILE", "/keys/example-sa.json")
    assert GoogleDrive().drive == "drive"
    assert calls == [("/keys/example-sa.json", (READONLY_SCOPE,))]


@pytest.mark.parametrize(
    "permissions, scope, complete",
    [
        (
            [
                {
                    "type": "user",
                    "role": "owner",
                    "emailAddress": "ada@example.org",
                    "id": "p1",
                }
            ],
            "named",
            True,
        ),
        (
            [
                {"type": "user", "role": "owner", "emailAddress": "ada@example.org"},
                {"type": "group", "role": "reader", "emailAddress": "team@example.org"},
            ],
            "named",
            False,
        ),
        ([{"type": "domain", "role": "reader", "domain": "example.org"}], "org", False),
        ([{"type": "anyone", "role": "reader", "withLink": True}], "public", False),
    ],
)
def test_the_audience_is_the_folders_sharing(permissions, scope, complete):
    drive = _drive()
    drive.permissions = permissions
    audience = correspond.audience(
        f"gdrive:{FOLDER}", registry={"gdrive": GoogleDrive(drive=drive)}
    )
    assert (audience.scope.value, audience.complete) == (scope, complete)
