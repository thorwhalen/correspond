"""Email over fake IMAP and SMTP: reading without marking seen, grades from trusted headers only, UID cursors, threaded sends."""

import imaplib
import smtplib
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses

import pytest

import correspond
from correspond.channels.mail import Email, authenticity
from correspond.errors import ChannelError, MissingRequirement

PNG = b"\x89PNG\r\n\x1a\nexample"


def _mail(
    *,
    sender="Ada Lovelace <ada@example.org>",
    to="me@example.org",
    subject="The export",
    body="The export drops the last row.",
    message_id="<m1@example.org>",
    auth=(),
    in_reply_to=None,
    references=None,
    html=None,
    attachment=None,
    date="Fri, 11 Sep 2026 09:00:00 +0000",
    headers=None,
):
    message = EmailMessage()
    for name, value in (headers or {}).items():
        message[name] = value
    for header in auth:
        message["Authentication-Results"] = header
    message["From"], message["To"], message["Subject"] = sender, to, subject
    message["Message-ID"], message["Date"] = message_id, date
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
    if references:
        message["References"] = references
    if html and not body:
        message.set_content(html, subtype="html")
    else:
        message.set_content(body)
        if html:
            message.add_alternative(html, subtype="html")
    if attachment:
        message.add_attachment(
            attachment, maintype="image", subtype="png", filename="screen.png"
        )
    return message.as_bytes()


class FakeImap:
    def __init__(self, messages, *, validity="42"):
        self.messages = dict(messages)
        self.validity = validity
        self.commands = []
        self.logged_out = False

    def select(self, mailbox, readonly=False):
        self.commands.append(("SELECT", mailbox, readonly))
        return "OK", [str(len(self.messages)).encode()]

    def response(self, code):
        if code == "UIDVALIDITY":
            return code, [self.validity.encode()]
        if code == "UIDNEXT":
            return code, [str(max(self.messages, default=0) + 1).encode()]
        return code, [None]

    def _sender(self, uid):
        parsed = BytesParser(policy=policy.default).parsebytes(self.messages[uid])
        return [a.lower() for _, a in getaddresses([str(parsed["From"])])]

    def uid(self, command, *args):
        self.commands.append((command, *args))
        if command == "SEARCH":
            uids, tokens = sorted(self.messages), list(args)
            for index, token in enumerate(tokens):
                if token == "FROM":
                    address = tokens[index + 1].strip('"')
                    uids = [u for u in uids if address in self._sender(u)]
                elif token == "UID":
                    start = int(tokens[index + 1].split(":")[0])
                    uids = [u for u in uids if u >= start] or (
                        [max(self.messages)] if self.messages else []
                    )
            return "OK", [" ".join(map(str, uids)).encode()]
        if command == "FETCH":
            data = []
            for uid in (int(x) for x in args[0].split(",")):
                if uid in self.messages:
                    data += [
                        (
                            f"{uid} (UID {uid} BODY[] {{{len(self.messages[uid])}}}".encode(),
                            self.messages[uid],
                        ),
                        b")",
                    ]
            return "OK", data
        raise AssertionError(command)

    def logout(self):
        self.logged_out = True


class FakeSmtp:
    def __init__(self, error=None):
        self.error = error
        self.sent = []
        self.quit_called = False

    def send_message(self, message):
        if self.error:
            raise self.error
        self.sent.append(message)

    def quit(self):
        self.quit_called = True


@pytest.fixture
def mailbox(monkeypatch):
    monkeypatch.setenv("CORRESPOND_EMAIL_USER", "me@example.org")
    return FakeImap(
        {
            1: _mail(attachment=PNG),
            2: _mail(
                sender="me@example.org",
                to="ada@example.org",
                message_id="<m2@example.org>",
                in_reply_to="<m1@example.org>",
                references="<m1@example.org>",
                date="Fri, 11 Sep 2026 09:30:00 +0000",
            ),
            3: _mail(
                sender="Grace <grace@example.org>",
                message_id="<m3@example.org>",
                body="",
                html="<p>Hello</p><script>x()</script><p>there</p>",
            ),
        }
    )


def _registry(imap=None, smtp=None):
    return {
        "email": Email(
            imap=(lambda: imap) if imap else None, smtp=(lambda: smtp) if smtp else None
        )
    }


def test_reading_an_address_parses_text_threads_and_attachments_without_marking_seen(
    mailbox,
):
    messages = correspond.read("email:Ada@Example.org", registry=_registry(mailbox))
    assert [m.id for m in messages] == ["<m1@example.org>"]
    first = messages[0]
    assert (
        first.author.handle == "ada@example.org"
        and first.author.display_name == "Ada Lovelace"
    )
    assert (
        first.conversation.encoded == "email:ada@example.org"
        and first.native["subject"] == "The export"
    )
    assert (
        first.text == "The export drops the last row."
        and first.authenticity.grade.value == "claimed"
    )
    [attachment] = first.attachments
    assert (
        attachment.media_type == "image/png"
        and attachment.name == "screen.png"
        and attachment.content() == PNG
    )
    assert ("SELECT", '"INBOX"', True) in mailbox.commands
    assert (
        any(c[0] == "FETCH" and "BODY.PEEK[]" in c[2] for c in mailbox.commands)
        and mailbox.logged_out
    )


def test_the_folder_holds_my_own_mail_in_the_conversation_with_its_recipient(mailbox):
    messages = correspond.read("email:", registry=_registry(mailbox))
    mine = next(m for m in messages if m.id == "<m2@example.org>")
    assert mine.author.is_self and mine.conversation.encoded == "email:ada@example.org"
    assert mine.reply_to == mine.thread_root == "<m1@example.org>"
    html = next(m for m in messages if m.id == "<m3@example.org>")
    assert (
        html.body_format == "html"
        and html.text == "Hello\nthere"
        and "script" not in html.text
    )


def _parsed(auth):
    return BytesParser(policy=policy.default).parsebytes(_mail(auth=auth))


def test_domain_only_from_the_topmost_header_of_a_trusted_server():
    passing = "mx.example.net; dkim=pass header.d=example.org; spf=pass; dmarc=pass (p=REJECT) header.from=example.org"
    trusted = {"mx.example.net"}
    assert authenticity(_parsed([passing]), trusted).grade.value == "domain"
    untrusted = authenticity(_parsed([passing]), {"mx.other.example"})
    assert (
        untrusted.grade.value == "claimed"
        and "not from a trusted server" in untrusted.evidence["reason"]
    )
    forged_below = authenticity(
        _parsed(["mx.example.net; dmarc=fail header.from=example.org", passing]), trusted
    )
    assert forged_below.grade.value == "claimed", (
        "a lower header may have been written by the sender"
    )
    other_domain = authenticity(
        _parsed(["mx.example.net; dmarc=pass header.from=example.net"]), trusted
    )
    assert other_domain.grade.value == "claimed"
    assert authenticity(_parsed([]), trusted).evidence == {
        "authentication_results": "none"
    }


def test_trusted_servers_come_from_settings(mailbox, monkeypatch):
    mailbox.messages[1] = _mail(
        auth=["mx.example.net; dmarc=pass header.from=example.org"]
    )
    monkeypatch.setenv("CORRESPOND_EMAIL_TRUSTED_AUTHSERV_IDS", "mx.example.net")
    assert (
        correspond.read("email:ada@example.org", registry=_registry(mailbox))[
            0
        ].authenticity.grade.value
        == "domain"
    )


def test_listening_by_uid_and_starting_over_when_uidvalidity_changes(mailbox):
    registry, cursors = _registry(mailbox), {}
    first = list(correspond.listen("email:", cursors=cursors, registry=registry))
    assert [e.payload["uid"] for e in first] == [1, 2, 3] and cursors == {
        "email:": "42:3"
    }
    assert any(c[:2] == ("SEARCH", "SINCE") for c in mailbox.commands)
    mailbox.messages[4] = _mail(message_id="<m4@example.org>")
    second = list(correspond.listen("email:", cursors=cursors, registry=registry))
    assert [e.message.id for e in second] == ["<m4@example.org>"] and cursors == {
        "email:": "42:4"
    }
    assert ("SEARCH", "UID", "5:*") not in mailbox.commands and (
        "SEARCH",
        "UID",
        "4:*",
    ) in mailbox.commands
    assert list(correspond.listen("email:", cursors=cursors, registry=registry)) == [], (
        "UID 5:* still returns UID 4"
    )
    mailbox.validity = "43"
    assert len(list(correspond.listen("email:", cursors=cursors, registry=registry))) == 4
    assert len({e.delivery_id for e in first + second}) == 4


def test_a_send_carries_threading_and_priority_headers(monkeypatch):
    monkeypatch.setenv("CORRESPOND_EMAIL_USER", "me@example.org")
    smtp = FakeSmtp()
    result = correspond.send(
        "email:ada@example.org",
        "It is fixed.",
        title="Re: The export",
        reply_to="<m1@example.org>",
        priority="high",
        registry=_registry(smtp=smtp),
    )
    [message] = smtp.sent
    assert (
        result.ok
        and result.message_id == message["Message-ID"]
        and message["Message-ID"].endswith("@example.org>")
    )
    assert (message["To"], message["From"], message["Subject"]) == (
        "ada@example.org",
        "me@example.org",
        "Re: The export",
    )
    assert message["In-Reply-To"] == message["References"] == "<m1@example.org>"
    assert (message["Importance"], message["X-Priority"]) == (
        "high",
        "1",
    ) and smtp.quit_called
    assert result.account.id == "me@example.org"


def test_sends_are_validated_and_dry_runs_connect_to_nothing(monkeypatch):
    def refuse():
        raise AssertionError("a dry run must not connect")

    registry = {"email": Email(smtp=refuse)}
    planned = correspond.send(
        "email:ada@example.org",
        "hi",
        title="Line one\nline two",
        dry_run=True,
        registry=registry,
    )
    assert planned.ok and planned.plan["to"] == "ada@example.org"
    assert correspond.send("email:", "hi", registry=registry).error_kind == "validation"
    assert (
        correspond.send(
            "email:ada@example.org", "hi", reply_to="m1", registry=registry
        ).error_kind
        == "validation"
    )


@pytest.mark.parametrize(
    "error, kind, retryable",
    [
        (
            smtplib.SMTPRecipientsRefused({"ada@example.org": (550, b"no such user")}),
            "validation",
            False,
        ),
        (smtplib.SMTPDataError(451, b"try again later"), "unavailable", True),
        (smtplib.SMTPDataError(554, b"rejected"), "validation", False),
        (smtplib.SMTPServerDisconnected("gone"), "network", True),
    ],
)
def test_smtp_failures_are_classified(monkeypatch, error, kind, retryable):
    monkeypatch.setenv("CORRESPOND_EMAIL_USER", "me@example.org")
    result = correspond.send(
        "email:ada@example.org", "hi", registry=_registry(smtp=FakeSmtp(error))
    )
    assert (result.ok, result.error_kind, result.retryable) == (False, kind, retryable)


def test_imap_failures_are_classified_and_missing_settings_explained(mailbox):
    class Dropping(FakeImap):
        def select(self, mailbox, readonly=False):
            raise imaplib.IMAP4.abort("connection reset")

    with pytest.raises(ChannelError) as caught:
        correspond.read("email:", registry=_registry(Dropping({})))
    assert (caught.value.kind, caught.value.retryable) == ("network", True)
    with pytest.raises(MissingRequirement, match="CORRESPOND_EMAIL_IMAP_HOST"):
        correspond.read("email:", registry={"email": Email()})
    with pytest.raises(correspond.InvalidRef):
        Email().parse_ref("not an address")


def test_the_native_fields_messages_carry_are_the_ones_capabilities_declare(mailbox):
    declared = set(Email().capabilities.native_fields)
    for message in correspond.read("email:", registry=_registry(mailbox)):
        assert set(message.native) <= declared


_NEWSLETTER_HTML = '<p>Issue 7</p><a href="https://example.org/item-1">First item</a>'
_LIST_HEADERS = {
    "List-Id": "Example Weekly <weekly.lists.example.org>",
    "List-Unsubscribe": "<https://example.org/unsub/abc>, <mailto:unsub@example.org>",
    "List-Post": "NO",
}


def _read_one(monkeypatch, raw):
    monkeypatch.setenv("CORRESPOND_EMAIL_USER", "me@example.org")
    (message,) = correspond.read("email:", registry=_registry(FakeImap({1: raw})))
    return message


def test_a_multipart_newsletter_keeps_the_html_part_and_list_headers(monkeypatch):
    message = _read_one(
        monkeypatch,
        _mail(
            body="Issue 7: see the web version",
            html=_NEWSLETTER_HTML,
            headers=_LIST_HEADERS,
        ),
    )
    assert message.body_format == "plain" and message.text.startswith("Issue 7")
    assert "https://example.org/item-1" in message.native["html"]
    assert message.native["list_id"] == "Example Weekly <weekly.lists.example.org>"
    assert message.native["list_unsubscribe"] == (
        "<https://example.org/unsub/abc>, <mailto:unsub@example.org>"
    )
    assert message.native["list_post"] == "NO"


def test_html_only_mail_keeps_html_in_body_not_duplicated_in_native(monkeypatch):
    message = _read_one(monkeypatch, _mail(body="", html=_NEWSLETTER_HTML))
    assert message.body_format == "html" and "item-1" in message.body
    assert "html" not in message.native


def test_repeated_and_dirty_list_headers_are_joined_and_cleaned(monkeypatch):
    raw = _mail(headers={"List-Unsubscribe": "<https://example.org/u>"})
    raw = raw.replace(
        b"List-Unsubscribe: <https://example.org/u>",
        b"List-Unsubscribe: <https://example.org/u>\nList-Unsubscribe: <mailto:unsub@example.org>\x00",
    )
    message = _read_one(monkeypatch, raw)
    assert message.native["list_unsubscribe"] == (
        "<https://example.org/u>, <mailto:unsub@example.org>"
    )


def test_plain_mail_without_list_headers_adds_no_keys(monkeypatch):
    message = _read_one(monkeypatch, _mail())
    assert not {"html", "list_id", "list_unsubscribe", "list_post"} & set(message.native)


def test_newsletter_native_keys_are_declared(monkeypatch):
    message = _read_one(monkeypatch, _mail(html=_NEWSLETTER_HTML, headers=_LIST_HEADERS))
    assert set(message.native) <= set(Email().capabilities.native_fields)


def test_folded_list_headers_are_unfolded(monkeypatch):
    raw = _mail(headers={"List-Unsubscribe": "<https://example.org/u>"})
    raw = raw.replace(
        b"List-Unsubscribe: <https://example.org/u>",
        b"List-Unsubscribe: <https://example.org/u>,\r\n <mailto:unsub@example.org>",
    )
    message = _read_one(monkeypatch, raw)
    assert message.native["list_unsubscribe"] == (
        "<https://example.org/u>, <mailto:unsub@example.org>"
    )


# ------------------------------------------------- per-call folder and several senders (#39)


class FolderImap(FakeImap):
    """A mailbox with several folders, searched the way a real server searches.

    ``FROM`` is a substring match, as in IMAP, and ``OR`` nests, so a test sees what an
    adapter that trusts the server's ``FROM`` would wrongly return.
    """

    def __init__(self, folders, *, validity="42"):
        super().__init__({}, validity=validity)
        self.folders = {name: dict(messages) for name, messages in folders.items()}
        self.selected = []

    def select(self, mailbox, readonly=False):
        self.commands.append(("SELECT", mailbox, readonly))
        name = mailbox[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        if name not in self.folders:
            return "NO", [b"no such folder"]
        self.selected.append(name)
        self.messages = self.folders[name]
        return "OK", [str(len(self.messages)).encode()]

    def _from(self, uid):
        parsed = BytesParser(policy=policy.default).parsebytes(self.messages[uid])
        return str(parsed["From"]).lower()

    def _criterion(self, tokens, index):
        token = tokens[index]
        if token == "OR":
            left, index = self._criterion(tokens, index + 1)
            right, index = self._criterion(tokens, index)
            return (lambda u: left(u) or right(u)), index
        if token == "FROM":
            needle = tokens[index + 1].strip('"').lower()
            return (lambda u: needle in self._from(u)), index + 2
        if token == "SINCE":
            return (lambda u: True), index + 2
        if token == "UID":
            start = int(tokens[index + 1].split(":")[0])
            return (lambda u: u >= start), index + 2
        if token == "ALL":
            return (lambda u: True), index + 1
        raise AssertionError(token)

    def uid(self, command, *args):
        if command != "SEARCH":
            return super().uid(command, *args)
        self.commands.append((command, *args))
        tokens, index, tests = list(args), 0, []
        while index < len(tokens):
            test, index = self._criterion(tokens, index)
            tests.append(test)
        uids = [u for u in sorted(self.messages) if all(t(u) for t in tests)]
        return "OK", [" ".join(map(str, uids)).encode()]


@pytest.fixture
def folders(monkeypatch):
    monkeypatch.setenv("CORRESPOND_EMAIL_USER", "me@example.org")
    return FolderImap(
        {
            "INBOX": {1: _mail()},
            "Topics/AI": {
                1: _mail(sender="Ada <ada@example.org>", message_id="<ai1@example.org>"),
                2: _mail(sender="news@example.net", message_id="<ai2@example.net>"),
                3: _mail(sender="nada@example.org", message_id="<ai3@example.org>"),
                4: _mail(sender="bot@example.org.example", message_id="<ai4@example>"),
            },
            "Topics/Space": {
                7: _mail(sender="grace@example.org", message_id="<sp7@example.org>"),
            },
            "Read Later": {
                9: _mail(sender="ada@example.org", message_id="<rl9@example.org>"),
            },
        }
    )


def test_two_reads_in_one_process_read_two_folders_with_since(folders):
    registry = _registry(folders)
    ai = correspond.read(
        "email:#Topics/AI", since="2026-09-01T00:00:00Z", registry=registry
    )
    space = correspond.read(
        "email:#Topics/Space", since="2026-09-01T00:00:00Z", registry=registry
    )
    assert [m.id for m in ai] == [
        "<ai1@example.org>",
        "<ai2@example.net>",
        "<ai3@example.org>",
        "<ai4@example>",
    ]
    assert [m.id for m in space] == ["<sp7@example.org>"]
    assert folders.selected == ["Topics/AI", "Topics/Space"]
    assert {m.native["folder"] for m in ai} == {"Topics/AI"}
    searches = [c for c in folders.commands if c[0] == "SEARCH"]
    assert all(c[1:] == ("ALL", "SINCE", "01-Sep-2026") for c in searches)
    assert correspond.read("email:", registry=registry)[0].native["folder"] == "INBOX", (
        "no folder in the reference is still the configured one"
    )


def test_a_message_read_from_a_named_folder_points_back_into_it(folders):
    registry = _registry(folders)
    [message] = correspond.read("email:ada@example.org#Topics/AI", registry=registry)
    assert message.conversation.encoded == "email:ada@example.org#Topics/AI"
    assert message.conversation.parent.encoded == "email:#Topics/AI"
    again = correspond.read(message.conversation, registry=registry)
    assert [m.id for m in again] == [message.id]


def test_several_senders_and_a_domain_match_exact_addresses_only(folders):
    registry = _registry(folders)
    several = correspond.read(
        "email:news@example.net,ada@example.org#Topics/AI", registry=registry
    )
    assert [m.id for m in several] == ["<ai1@example.org>", "<ai2@example.net>"], (
        "nada@ contains ada@ but is not ada@"
    )
    domain = correspond.read("email:@example.org#Topics/AI", registry=registry)
    assert [m.id for m in domain] == ["<ai1@example.org>", "<ai3@example.org>"], (
        "@example.org.example is another domain"
    )
    one = correspond.read("email:ada@example.org#Topics/AI", registry=registry)
    assert [m.id for m in one] == ["<ai1@example.org>"]
    search = [c for c in folders.commands if c[0] == "SEARCH"][0]
    assert search[1:] == (
        "OR",
        "FROM",
        '"ada@example.org"',
        "FROM",
        '"news@example.net"',
    )


def test_listening_to_a_named_folder_keeps_its_own_cursor(folders):
    registry, cursors = _registry(folders), {}
    events = list(
        correspond.listen(
            "email:@example.org#Topics/AI", cursors=cursors, registry=registry
        )
    )
    assert [e.message.id for e in events] == ["<ai1@example.org>", "<ai3@example.org>"]
    assert {e.delivery_id for e in events} == {
        "email:Topics/AI:42:1",
        "email:Topics/AI:42:3",
    }
    list(correspond.listen("email:", cursors=cursors, registry=registry))
    assert cursors == {"email:@example.org#Topics/AI": "42:4", "email:": "42:1"}


def test_folder_references_are_canonical_and_round_trip():
    email = Email()
    assert email.parse_ref("#inbox").encoded == "email:#INBOX"
    assert email.parse_ref("#Topics/AI").kind == "folder"
    several = email.parse_ref("Ada@Example.org,@EXAMPLE.net,ada@example.org#Topics/AI")
    assert several.encoded == "email:@example.net,ada@example.org#Topics/AI"
    assert several.kind == "senders" and several.parent.encoded == "email:#Topics/AI"
    spaced = email.parse_ref("#Read%20Later")
    assert spaced.encoded == "email:#Read%20Later"
    assert email.parse_ref(spaced.id) == spaced
    assert email.parse_ref("a#b@example.org").encoded == "email:a#b@example.org", (
        "an address whose local part holds # is still one address"
    )
    for bad in (
        "ada",
        "ada@example.org#",
        "@",
        "@example",
        "ada@example.org,,x@example.org",
    ):
        with pytest.raises(correspond.errors.InvalidRef):
            email.parse_ref(bad)


def test_a_folder_with_a_space_is_selected_by_its_real_name(folders):
    [message] = correspond.read("email:#Read%20Later", registry=_registry(folders))
    assert message.id == "<rl9@example.org>"
    assert ("SELECT", '"Read Later"', True) in folders.commands


def test_sends_need_one_address_and_ignore_the_folder(monkeypatch):
    monkeypatch.setenv("CORRESPOND_EMAIL_USER", "me@example.org")
    registry = _registry(smtp=FakeSmtp())
    plan = correspond.send(
        "email:ada@example.org#Topics/AI", "Thanks.", dry_run=True, registry=registry
    ).plan
    assert plan["to"] == "ada@example.org"
    for ref in ("email:ada@example.org,news@example.net", "email:@example.org"):
        result = correspond.send(ref, "Thanks.", dry_run=True, registry=registry)
        assert not result.ok and result.error_kind == "validation"
        assert "one address" in result.error
    audience = correspond.audience("email:@example.org", registry=registry)
    assert audience.defaulted and audience.scope.value == "public"


# ----------------------------------------------------------------- upload (#8)


def test_upload_emails_the_file_as_an_attachment(monkeypatch):
    monkeypatch.setenv("CORRESPOND_EMAIL_USER", "me@example.org")
    smtp = FakeSmtp()
    result = correspond.upload(
        "email:ada@example.org#Topics/AI",
        "screen.png",
        PNG,
        media_type="image/png",
        registry=_registry(smtp=smtp),
    )
    assert result.ok and result.operation == "upload"
    [message] = smtp.sent
    assert message["To"] == "ada@example.org" and message["Subject"] == "screen.png"
    assert result.message_id == message["Message-ID"]
    [attachment] = list(message.iter_attachments())
    assert attachment.get_filename() == "screen.png"
    assert attachment.get_content_type() == "image/png"
    assert attachment.get_payload(decode=True) == PNG
    assert result.plan["action"] == "email a file" and result.plan["bytes"] == len(PNG)
    assert "data" not in result.plan


def test_email_uploads_are_validated_and_dry_runs_connect_to_nothing(monkeypatch):
    monkeypatch.setenv("CORRESPOND_EMAIL_USER", "me@example.org")

    def refuse():
        raise AssertionError("a dry run must not connect")

    registry = {"email": Email(smtp=refuse)}
    plan = correspond.upload(
        "email:ada@example.org", "a.txt", b"x", dry_run=True, registry=registry
    )
    assert plan.ok and plan.dry_run and plan.plan["to"] == "ada@example.org"
    for ref, name, data in (
        ("email:@example.org", "a.txt", b"x"),
        ("email:", "a.txt", b"x"),
        ("email:ada@example.org", "", b"x"),
        ("email:ada@example.org", "a\nb.txt", b"x"),
        ("email:ada@example.org", "a.txt", b""),
        (
            "email:ada@example.org",
            "a.txt",
            b"x" * (Email().capabilities.max_upload_bytes + 1),
        ),
    ):
        refused = correspond.upload(ref, name, data, dry_run=True, registry=registry)
        assert not refused.ok and refused.error_kind == "validation", (ref, name)
    bad_type = correspond.upload(
        "email:ada@example.org",
        "a.txt",
        b"x",
        media_type="text",
        dry_run=True,
        registry=registry,
    )
    assert not bad_type.ok and bad_type.error_kind == "validation"
