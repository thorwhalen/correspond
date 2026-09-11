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
):
    message = EmailMessage()
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
