"""Files on a send, an upload's idempotency key, and Telegram's as_document (#60)."""

import json

import pytest

import correspond
from correspond import tools
from correspond.channels._http import Response
from correspond.channels.mail import Email
from correspond.channels.telegram import Telegram
from correspond.errors import NotSupported
from correspond.model import Attachment, Draft
from correspond.testing import FakeChannel

PNG = b"\x89PNG\r\n\x1a\nexample"
TOKEN = "123456:example-token-for-tests"


def _file(name, data, media_type="application/octet-stream"):
    return Attachment(
        ref=f"memory:{name}",
        name=name,
        media_type=media_type,
        size=len(data),
        loader=lambda: data,
    )


class FakeSmtp:
    def __init__(self):
        self.sent = []

    def send_message(self, message, to_addrs=None):
        self.sent.append(message)

    def quit(self):
        pass


class FakeTelegram:
    """Records the sending calls; the audience lookup (getChat) every write makes answers a group."""

    def __init__(self):
        self.calls = []

    def __call__(self, method, url, *, headers=None, body=None, timeout=None):
        name = url.rsplit("/", 1)[-1]
        if name == "getChat":
            return Response(200, {}, json.dumps({"ok": True, "result": {"id": -4001, "type": "group"}}).encode())
        self.calls.append((name, headers.get("Content-Type", ""), body))
        result = {
            "message_id": 77,
            "chat": {"id": -4001, "type": "group"},
            "date": 1_757_581_200,
            "from": {"id": 900, "is_bot": True, "username": "example_bot"},
        }
        return Response(200, {}, json.dumps({"ok": True, "result": result}).encode())


@pytest.fixture
def mail(monkeypatch):
    monkeypatch.setenv("CORRESPOND_EMAIL_USER", "me@example.org")
    smtp = FakeSmtp()
    return smtp, {"email": Email(smtp=lambda: smtp)}


@pytest.fixture
def bot(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    api = FakeTelegram()
    return api, {"telegram": Telegram(http=api, log={})}


def test_a_draft_without_attachments_keeps_its_fingerprint():
    from correspond.idempotency import fingerprint

    assert "attachments" not in Draft(text="hi").to_dict()
    assert fingerprint("x:y", Draft(text="hi")) == fingerprint(
        "x:y", Draft(text="hi", attachments=())
    )
    with_file = Draft(text="hi", attachments=[_file("a.txt", b"x")])
    assert with_file.to_dict()["attachments"][0]["name"] == "a.txt"
    with pytest.raises(TypeError):
        Draft(text="hi", attachments=["a.txt"])


def test_an_email_carries_every_attachment(mail):
    smtp, registry = mail
    result = correspond.send(
        "email:ada@example.org",
        "both files",
        title="Files",
        attachments=[
            _file("a.pdf", b"%PDF", "application/pdf"),
            _file("b.csv", b"a,b", "text/csv"),
        ],
        registry=registry,
    )
    assert result.ok and [f["name"] for f in result.plan["files"]] == ["a.pdf", "b.csv"]
    (message,) = smtp.sent
    parts = list(message.iter_attachments())
    assert [p.get_filename() for p in parts] == ["a.pdf", "b.csv"]
    assert (
        parts[0].get_payload(decode=True) == b"%PDF"
        and message.get_body(("plain",)).get_content().strip() == "both files"
    )


def test_a_channel_without_upload_refuses_attachments_by_name():
    fake = FakeChannel()
    with pytest.raises(NotSupported, match="attachments"):
        correspond.send(
            "fake:example/demo",
            "x",
            attachments=[_file("a.txt", b"x")],
            registry={"fake": fake},
        )


def test_an_attachment_over_the_channels_limit_is_refused(mail, monkeypatch):
    from correspond.channels import mail as module

    monkeypatch.setattr(module, "MAX_ATTACHMENT_BYTES", 3)
    smtp, registry = mail
    result = correspond.send(
        "email:ada@example.org",
        "x",
        attachments=[_file("a.bin", b"1234")],
        registry=registry,
    )
    assert (result.ok, result.error_kind) == (False, "validation") and smtp.sent == []


def test_before_send_sees_the_attachments_never_their_bytes(mail):
    _, registry = mail
    seen = []

    def check(ref, draft, audience, **context):
        seen.append(draft.to_dict())

    correspond.send(
        "email:ada@example.org",
        "x",
        attachments=[_file("a.txt", b"secret bytes")],
        dry_run=True,
        before_send=check,
        registry=registry,
    )
    ((draft,),) = [seen]
    assert draft["attachments"][0][
        "name"
    ] == "a.txt" and "secret bytes" not in json.dumps(draft)


def test_telegram_sends_one_file_with_the_text_as_its_caption(bot):
    api, registry = bot
    result = correspond.send(
        "telegram:-4001",
        "the screen",
        attachments=[_file("s.png", PNG, "image/png")],
        registry=registry,
    )
    assert result.ok and result.operation == "send"
    ((method, kind, body),) = api.calls
    assert (
        method == "sendPhoto"
        and kind.startswith("multipart/form-data")
        and b"the screen" in body
    )


def test_telegram_refuses_two_files_or_a_caption_too_long(bot):
    api, registry = bot
    two = correspond.send(
        "telegram:-4001",
        "x",
        attachments=[_file("a.txt", b"a"), _file("b.txt", b"b")],
        registry=registry,
    )
    long = correspond.send(
        "telegram:-4001",
        "x" * 1025,
        attachments=[_file("a.txt", b"a")],
        registry=registry,
    )
    assert (two.error_kind, long.error_kind) == (
        "validation",
        "validation",
    ) and api.calls == []


def test_as_document_sends_an_image_as_the_file_it_is(bot):
    api, registry = bot
    upload = correspond.upload(
        "telegram:-4001",
        "s.png",
        PNG,
        media_type="image/png",
        native={"as_document": True},
        registry=registry,
    )
    send = correspond.send(
        "telegram:-4001",
        "x",
        native={"as_document": True},
        attachments=[_file("s.png", PNG, "image/png")],
        registry=registry,
    )
    assert (
        upload.ok
        and send.ok
        and [c[0] for c in api.calls] == ["sendDocument", "sendDocument"]
    )
    alone = correspond.send(
        "telegram:-4001", "x", native={"as_document": True}, registry=registry
    )
    wrong = correspond.upload(
        "telegram:-4001",
        "s.png",
        PNG,
        media_type="image/png",
        native={"as_document": "yes"},
        registry=registry,
    )
    assert (alone.error_kind, wrong.error_kind) == ("validation", "validation")


def test_an_upload_native_field_a_channel_lacks_is_refused(mail):
    smtp, registry = mail
    result = correspond.upload(
        "email:ada@example.org",
        "a.txt",
        b"x",
        native={"as_document": True},
        registry=registry,
    )
    assert (result.ok, result.error_kind) == (False, "validation") and smtp.sent == []


def test_an_upload_key_never_posts_the_file_twice(bot):
    api, registry = bot
    sends = {}
    first = correspond.upload(
        "telegram:-4001",
        "a.txt",
        b"x",
        idempotency_key="k1",
        sends=sends,
        registry=registry,
    )
    again = correspond.upload(
        "telegram:-4001",
        "a.txt",
        b"x",
        idempotency_key="k1",
        sends=sends,
        registry=registry,
    )
    assert first.ok and again.ok and "already sent" in again.plan["idempotency"]
    assert [c[0] for c in api.calls] == ["sendDocument"]
    other = correspond.upload(
        "telegram:-4001",
        "b.txt",
        b"y",
        idempotency_key="k1",
        sends=sends,
        registry=registry,
    )
    assert (other.ok, other.error_kind) == (False, "validation")


def test_an_upload_whose_outcome_is_unknown_is_unconfirmed_not_retried(bot):
    from correspond.idempotency import claimed, fingerprint, now

    api, registry = bot
    probe = correspond.upload(
        "telegram:-4001", "a.txt", b"x", dry_run=True, registry=registry
    )
    assert probe.ok
    # Simulate a crash after the claim: the key is ATTEMPTED with this upload's fingerprint.
    import hashlib

    upload = {
        "name": "a.txt",
        "media_type": "application/octet-stream",
        "bytes": 1,
        "sha256": hashlib.sha256(b"x").hexdigest(),
    }
    draft = Draft(text="a.txt", native={"upload": upload})
    sends = {
        "k2": claimed(
            "k2",
            "telegram:-4001",
            fingerprint=fingerprint("telegram:-4001", draft),
            at=now(),
        )
    }
    result = correspond.upload(
        "telegram:-4001",
        "a.txt",
        b"x",
        idempotency_key="k2",
        sends=sends,
        registry=registry,
    )
    assert (result.ok, result.error_kind) == (
        False,
        "unconfirmed",
    ) and "not read back" in result.error
    assert api.calls == []


def test_the_send_tool_attaches_local_files_and_names_their_real_paths(
    tmp_path, monkeypatch
):
    import os

    monkeypatch.setenv("CORRESPOND_EMAIL_USER", "me@example.org")
    first, second = tmp_path / "a.txt", tmp_path / "b.pdf"
    first.write_bytes(b"x")
    second.write_bytes(b"%PDF")
    result = tools.send(
        "email:ada@example.org", "files", attach=f"{first},{second}", dry_run=True
    )
    assert result["ok"], result
    assert [f["ref"] for f in result["plan"]["files"]] == [
        f"file:{os.path.realpath(first)}",
        f"file:{os.path.realpath(second)}",
    ]
    assert result["plan"]["files"][1]["media_type"] == "application/pdf"
    hidden = tmp_path / ".private"
    hidden.mkdir()
    (hidden / "k").write_bytes(b"k")
    refused = tools.send(
        "email:ada@example.org", "x", attach=str(hidden / "k"), dry_run=True
    )
    assert (refused["ok"], refused["error_kind"]) == (False, "permission")
    nowhere = tools.send("github:example/demo#1", "x", attach=str(first), dry_run=True)
    assert (nowhere["ok"], nowhere["error_kind"]) == (False, "not_supported")
