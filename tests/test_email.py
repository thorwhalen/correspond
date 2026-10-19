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


# ------------------------------------------ a folder and several senders per reference (#39)


class FakeFolders(FakeImap):
    """Several folders, each its own UIDs; ``FROM`` matches a substring, as IMAP servers do."""

    def __init__(self, folders, **kwargs):
        super().__init__({}, **kwargs)
        self.folders = {name: dict(messages) for name, messages in folders.items()}

    def select(self, mailbox, readonly=False):
        self.commands.append(("SELECT", mailbox, readonly))
        name = mailbox.strip('"')
        if name not in self.folders:
            return "NO", [b"no such mailbox"]
        self.messages = self.folders[name]
        return "OK", [str(len(self.messages)).encode()]

    def uid(self, command, *args):
        if command != "SEARCH":
            return super().uid(command, *args)
        self.commands.append((command, *args))
        uids, tokens = sorted(self.messages), list(args)
        for index, token in enumerate(tokens):
            if token == "FROM":
                needle = tokens[index + 1].strip('"')
                uids = [u for u in uids if any(needle in s for s in self._sender(u))]
            elif token == "SINCE":
                day = tokens[index + 1]
                uids = [u for u in uids if self._day(u) >= day]
            elif token == "UID":
                start = int(tokens[index + 1].split(":")[0])
                uids = [u for u in uids if u >= start] or (
                    [max(self.messages)] if self.messages else []
                )
        return "OK", [" ".join(map(str, uids)).encode()]

    def _day(self, uid):
        from datetime import datetime

        parsed = BytesParser(policy=policy.default).parsebytes(self.messages[uid])
        moment = datetime.strptime(str(parsed["Date"]), "%a, %d %b %Y %H:%M:%S %z")
        return moment.strftime("%Y%m%d")


def _news(uid, sender, day, *, folder="news"):
    return _mail(
        sender=sender,
        message_id=f"<{folder}-{uid}@example.org>",
        subject=f"{folder} {uid}",
        date=f"{day} Sep 2026 09:00:00 +0000",
    )


@pytest.fixture
def topics(monkeypatch):
    monkeypatch.setenv("CORRESPOND_EMAIL_USER", "me@example.org")
    monkeypatch.delenv("CORRESPOND_EMAIL_FOLDER", raising=False)
    return FakeFolders(
        {
            "INBOX": {1: _news(1, "ada@example.org", "Fri, 11", folder="inbox")},
            "Newsletters/AI": {
                1: _news(1, "digest@example.org", "Tue, 01", folder="ai"),
                2: _news(2, "digest@example.org", "Mon, 14", folder="ai"),
            },
            "&AMk-nergie": {  # Énergie, as the server names it
                7: _news(7, "letters@example.net", "Tue, 15", folder="energy"),
                8: _news(8, "eve@example.org.example.net", "Wed, 16", folder="energy"),
                9: _news(9, "bada@example.org", "Wed, 16", folder="energy"),
                10: _news(10, "ada@example.org", "Thu, 17", folder="energy"),
            },
        }
    )


def test_two_reads_in_one_process_read_two_folders_with_since(topics):
    """The acceptance line of #39: no environment change between the two calls."""
    from datetime import datetime, timezone

    since = datetime(2026, 9, 10, tzinfo=timezone.utc)
    registry = {"email": Email(imap=lambda: topics)}
    ai = correspond.read("email:?folder=Newsletters/AI", since=since, registry=registry)
    energy = correspond.read("email:?folder=%C3%89nergie", since=since, registry=registry)
    assert [m.id for m in ai] == ["<ai-2@example.org>"], (
        "the 1 September issue predates since"
    )
    assert [m.id for m in energy] == [
        "<energy-7@example.org>",
        "<energy-8@example.org>",
        "<energy-9@example.org>",
        "<energy-10@example.org>",
    ]
    assert {m.native["folder"] for m in ai} == {"Newsletters/AI"}
    assert {m.native["folder"] for m in energy} == {"Énergie"}
    selected = [c[1] for c in topics.commands if c[0] == "SELECT"]
    assert selected == ['"Newsletters/AI"', '"&AMk-nergie"'], "modified UTF-7 on the wire"
    assert any(c[0] == "SEARCH" and "SINCE" in c for c in topics.commands)


def test_a_reference_without_a_folder_still_reads_the_configured_one(topics, monkeypatch):
    registry = {"email": Email(imap=lambda: topics)}
    assert [m.id for m in correspond.read("email:", registry=registry)] == [
        "<inbox-1@example.org>"
    ]
    monkeypatch.setenv("CORRESPOND_EMAIL_FOLDER", "Newsletters/AI")
    assert len(correspond.read("email:", registry=registry)) == 2


def test_several_senders_and_a_domain_keep_exact_matches_only(topics):
    registry = {"email": Email(imap=lambda: topics)}
    ref = "email:?folder=%C3%89nergie&from=ada@example.org,@example.net"
    messages = correspond.read(ref, registry=registry)
    assert [m.author.handle for m in messages] == [
        "letters@example.net",
        "ada@example.org",
    ], "bada@ only contains ada@; @example.net is that domain, not its subdomains"
    searches = [c for c in topics.commands if c[0] == "SEARCH"]
    assert [c[-1] for c in searches] == ['"@example.net"', '"ada@example.org"']


def test_an_address_reference_with_a_folder_reads_only_that_address(topics):
    registry = {"email": Email(imap=lambda: topics)}
    messages = correspond.read(
        "email:ada@example.org?folder=%C3%89nergie", registry=registry
    )
    assert [m.id for m in messages] == ["<energy-10@example.org>"]
    assert messages[0].conversation.encoded == "email:ada@example.org"


def test_listening_to_a_filtered_folder_never_stalls_on_mail_it_filters_out(topics):
    ref = "email:?folder=%C3%89nergie&from=ada@example.org"
    registry = {"email": Email(imap=lambda: topics)}
    cursors = {}
    key = Email().parse_ref(ref.removeprefix("email:")).encoded
    cursors[key] = "42:7"
    first = list(correspond.listen(ref, cursors=cursors, limit=1, registry=registry))
    assert first == [], "9 (bada@) matches FROM as a substring only"
    assert cursors[key] == "42:9", "the cursor still moves past what was looked at"
    second = list(correspond.listen(ref, cursors=cursors, registry=registry))
    assert [e.message.id for e in second] == ["<energy-10@example.org>"]
    assert second[0].delivery_id == "email:Énergie:42:10"


def test_reference_grammar_is_canonical_and_refuses_what_it_cannot_mean():
    email = Email()
    assert (
        email.parse_ref("Ada@Example.org?folder=inbox").encoded
        == "email:ada@example.org?folder=INBOX"
    )
    assert (
        email.parse_ref("?folder=%5BGmail%5D/All%20Mail").encoded
        == "email:?folder=[Gmail]/All%20Mail"
    )
    folder = email.parse_ref("?from=b@example.org,A@example.org&folder=News")
    assert (folder.kind, folder.id) == (
        "folder",
        "?folder=News&from=a@example.org,b@example.org",
    )
    address = email.parse_ref("ada@example.org?folder=News")
    assert (address.kind, address.parent.encoded) == ("address", "email:?folder=News")
    assert email.parse_ref("").encoded == "email:"
    for bad in (
        "?folder=",
        "?folder=News&folder=Other",
        "?label=News",
        "?folder",
        "ada@example.org?from=b@example.org",
        "?from=example.org",
        "?from=ada@example.org,",
        "?folder=a%0Ab",
        "not-an-address?folder=News",
    ):
        with pytest.raises(correspond.InvalidRef):
            email.parse_ref(bad)


def test_a_send_to_an_address_with_a_folder_goes_to_the_address(monkeypatch):
    monkeypatch.setenv("CORRESPOND_EMAIL_USER", "me@example.org")
    smtp = FakeSmtp()
    result = correspond.send(
        "email:ada@example.org?folder=News", "hello", registry=_registry(smtp=smtp)
    )
    assert result.ok and smtp.sent[0]["To"] == "ada@example.org"
    audience = correspond.audience(
        "email:?folder=News&from=ada@example.org", registry=_registry()
    )
    assert "name an address" in audience.evidence[0], (
        "a filtered folder is still no correspondence"
    )


def test_a_folder_named_either_way_is_one_reference_and_one_wire_name():
    from correspond.channels.mail import _mailbox

    email = Email()
    assert (
        email.parse_ref("?folder=%26AMk-nergie").encoded
        == email.parse_ref("?folder=%C3%89nergie").encoded
        == "email:?folder=%C3%89nergie"
    )
    literal = email.parse_ref("?folder=Q%26A-2024")
    assert literal.encoded == "email:?folder=Q%26A-2024"
    assert _mailbox("Q&A-2024") == "Q&-A-2024", "not mistaken for an encoded run"


def test_a_limit_counts_mail_that_passes_the_exact_sender_check(monkeypatch):
    monkeypatch.setenv("CORRESPOND_EMAIL_USER", "me@example.org")
    imap = FakeFolders(
        {
            "INBOX": {
                1: _news(1, "ada@example.org", "Tue, 01", folder="in"),
                2: _news(2, "bada@example.org", "Wed, 02", folder="in"),
                3: _news(3, "bada@example.org", "Thu, 03", folder="in"),
                5: _news(5, "ADA@example.org", "Sat, 05", folder="in"),
            }
        }
    )
    messages = correspond.read(
        "email:ada@example.org", limit=2, registry={"email": Email(imap=lambda: imap)}
    )
    assert [m.id for m in messages] == ["<in-1@example.org>", "<in-5@example.org>"]


def test_any_from_address_in_scope_keeps_the_message(monkeypatch):
    monkeypatch.setenv("CORRESPOND_EMAIL_USER", "me@example.org")
    imap = FakeFolders(
        {
            "INBOX": {
                1: _news(1, "Bob <bob@example.net>, Ada <ada@example.org>", "Tue, 01")
            }
        }
    )
    messages = correspond.read(
        "email:ada@example.org", registry={"email": Email(imap=lambda: imap)}
    )
    assert [m.id for m in messages] == ["<news-1@example.org>"]


def test_an_address_with_reserved_characters_is_written_escaped():
    email = Email()
    ref = email.parse_ref("a%3Fb@example.org?folder=News")
    assert ref.encoded == "email:a%3Fb@example.org?folder=News"
    assert email.parse_ref(ref.id).encoded == ref.encoded
    for bad in ("?folder=%E9", "a%E9@example.org"):
        with pytest.raises(correspond.InvalidRef):
            email.parse_ref(bad)
