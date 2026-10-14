"""The notification guarantee on drafts (issue #7): ``Draft.notify`` is a property of sending.

Every write here is a dry run against an adapter (which contacts nothing) or goes to
``FakeChannel``; no real address, handle or topic appears.
"""

import pytest

import correspond
from correspond import idempotency, tools
from correspond.channels.github import GitHub
from correspond.channels.mail import Email
from correspond.channels.ntfy import Ntfy
from correspond.channels.telegram import Telegram
from correspond.errors import ChannelError, NotSupported
from correspond.model import Draft, Support
from correspond.notify import missing_mentions, with_copies, with_mentions
from correspond.testing import FakeChannel

REPO = "example/demo"


def _ref(adapter, id_):
    return adapter.parse_ref(id_)


# --- the pure helpers ---------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("hello @octocat", ()),
        ("hello @OCTOCAT!", ()),
        ("mail octocat@example.org", ("octocat",)),  # an address is not a mention
        ("hello @octocat-two", ("octocat",)),  # a longer handle is not a mention
        ("ping @example/octocat", ("octocat",)),  # a team mention is not the user
    ],
)
def test_missing_mentions(text, expected):
    assert missing_mentions(text, ["@octocat"]) == expected


def test_with_mentions_adds_one_line_once_per_handle():
    assert with_mentions("done", ["a-user", "@a-user", "b-user"]) == "done\n\ncc @a-user @b-user"
    assert with_mentions("done @a-user", ["a-user"]) == "done @a-user"
    assert with_mentions("done", []) == "done"


def test_with_copies_skips_the_to_address_and_existing_copies():
    assert with_copies(
        "ada@example.org", ["bob@example.org"], ["ADA@example.org", "Bob@example.org", "cy@example.org"]
    ) == ("bob@example.org", "cy@example.org")


# --- the model ------------------------------------------------------------------


def test_draft_notify_is_normalised_and_absent_from_the_dict_unless_set():
    assert Draft(text="hi", notify=[" a ", "", "a", "b"]).notify == ("a", "b")
    assert "notify" not in Draft(text="hi").to_dict()  # idempotency fingerprints unchanged
    assert Draft(text="hi", notify=["a"]).to_dict()["notify"] == ["a"]
    with pytest.raises(TypeError, match="notify"):
        Draft(text="hi", notify="a")


def test_capabilities_grade_notify():
    assert GitHub().capabilities.notify is Support.PARTIAL  # not past repository visibility
    assert Email().capabilities.notify is Support.FULL
    assert Telegram().capabilities.notify is Support.PARTIAL
    assert Ntfy().capabilities.notify is Support.NONE
    assert "notify" in Ntfy().capabilities.to_dict()


# --- ops.send -------------------------------------------------------------------


def test_a_channel_that_cannot_guarantee_it_refuses_with_not_supported():
    """Raised before anything is planned, like an unsupported ``cc``."""
    with pytest.raises(NotSupported, match="notify"):
        correspond.send(
            "ntfy:example-topic", "hi", notify=["octocat"], dry_run=True, registry={"ntfy": Ntfy()}
        )


def test_the_fake_channel_posts_the_mention_and_the_plan_says_so():
    fake = FakeChannel()
    registry = {"fake": fake}
    planned = correspond.send(
        "fake:example/demo", "fixed", notify=["octocat"], dry_run=True, registry=registry
    )
    assert planned.ok and planned.plan["text"] == "fixed\n\ncc @octocat"
    assert planned.plan["mentions_added"] == ["octocat"]
    sent = correspond.send("fake:example/demo", "fixed", notify=["octocat"], registry=registry)
    assert sent.ok
    assert fake.conversations["example/demo"][-1].text == "fixed\n\ncc @octocat"


def test_a_draft_and_loose_notify_are_not_both_accepted():
    with pytest.raises(ValueError, match="notify"):
        correspond.send(
            "fake:example/demo",
            Draft(text="hi"),
            notify=["octocat"],
            registry={"fake": FakeChannel()},
        )


def test_every_surface_takes_notify():
    """tools.py is the SSOT: the CLI and MCP are generated from its flat signature."""
    import inspect

    from correspond.__main__ import HELP

    assert "notify" in inspect.signature(tools.send).parameters
    assert "notify" in HELP["send"]


# --- the writers ------------------------------------------------------------------


def test_github_mentions_who_is_missing_on_a_comment_and_an_issue():
    gh = GitHub(run=lambda *a, **k: pytest.fail("a dry run runs no gh command"))
    comment = gh.send(
        _ref(gh, f"{REPO}#1"), Draft(text="thanks @a-user", notify=["a-user", "b-user"]), dry_run=True
    )
    assert comment.plan["body"] == "thanks @a-user\n\ncc @b-user"
    assert comment.plan["mentions_added"] == ["b-user"]
    issue = gh.send(
        _ref(gh, REPO), Draft(text="body", title="T", notify=["example/team"]), dry_run=True
    )
    assert issue.plan["body"] == "body\n\ncc @example/team"


def test_github_refuses_what_is_not_a_handle():
    gh = GitHub(run=lambda *a, **k: pytest.fail("no gh"))
    with pytest.raises(ChannelError, match="handle"):
        gh.send(_ref(gh, f"{REPO}#1"), Draft(text="x", notify=["not a handle"]), dry_run=True)


def test_email_puts_notify_in_cc_unless_already_to_or_cc():
    mail = Email(smtp=lambda: pytest.fail("a dry run connects to nothing"))
    result = mail.send(
        _ref(mail, "ada@example.org"),
        Draft(
            text="hi",
            cc=["bob@example.org"],
            bcc=["cy@example.org"],
            notify=["Ada@example.org", "bob@example.org", "cy@example.org", "dee@example.org"],
        ),
        dry_run=True,
    )
    assert result.plan["cc"] == "bob@example.org, cy@example.org, dee@example.org"
    assert result.plan["bcc"] is None  # moved to Cc: Bcc would hide it
    assert result.plan["cc_added"] == ["cy@example.org", "dee@example.org"]


def test_email_notify_takes_addresses():
    mail = Email(smtp=lambda: pytest.fail("no smtp"))
    with pytest.raises(ChannelError, match="addresses"):
        mail.send(_ref(mail, "ada@example.org"), Draft(text="hi", notify=["octocat"]), dry_run=True)


def test_telegram_mentions_the_username(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:example-token-for-tests")
    bot = Telegram(http=lambda *a, **k: pytest.fail("a dry run calls nothing"), log={})
    result = bot.send(
        bot.parse_ref("-1001"), Draft(text="ready", notify=["@example_user"]), dry_run=True
    )
    assert result.plan["text"] == "ready\n\ncc @example_user"
    with pytest.raises(ChannelError, match="username"):
        bot.send(bot.parse_ref("-1001"), Draft(text="x", notify=["no"]), dry_run=True)


# --- idempotency ------------------------------------------------------------------


def test_read_back_matches_the_text_as_posted_with_its_mentions():
    fake = FakeChannel()
    registry = {"fake": fake}
    draft = Draft(text="fixed", notify=["octocat"])
    correspond.send("fake:example/demo", draft, registry=registry)
    messages = fake.conversations["example/demo"]
    since = messages[-1].sent_at
    assert idempotency.matching_message(messages, draft, since=since) is messages[-1]
    assert idempotency.matching_message(messages, Draft(text="fixed"), since=since) is None


def test_email_audience_counts_the_notify_addresses_a_send_copies():
    """The before_send check must see the Cc the send will really carry."""
    mail = Email()
    ref = _ref(mail, "ada@example.org")
    plain = mail.audience(ref, draft=Draft(text="hi"))
    notified = mail.audience(ref, draft=Draft(text="hi", notify=["dee@example.org"]))
    assert notified.hash != plain.hash
    assert notified == mail.audience(ref, draft=Draft(text="hi", cc=["dee@example.org"]))


# --- review fixes: the effective draft is what gets checked ------------------------


def test_the_length_limit_counts_the_mention_line():
    gh = GitHub(run=lambda *a, **k: pytest.fail("refused before any gh call"))
    limit = gh.capabilities.max_text_length
    result = correspond.send(
        f"github:{REPO}#1", "x" * limit, notify=["octocat"], dry_run=True, registry={"github": gh}
    )
    assert not result.ok and result.error_kind == "validation"


def test_before_send_sees_the_draft_as_it_will_go_out():
    seen = []

    def check(ref, draft, audience, **kw):
        seen.append(draft)

    mail = Email(smtp=lambda: pytest.fail("dry run"))
    correspond.send(
        "email:ada@example.org", "hi", notify=["dee@example.org"], dry_run=True,
        registry={"email": mail}, before_send=check,
    )
    correspond.send(
        "fake:example/demo", "hi", notify=["octocat"], dry_run=True,
        registry={"fake": FakeChannel()}, before_send=check,
    )
    assert seen[0].cc == ("dee@example.org",)
    assert seen[1].text == "hi\n\ncc @octocat"


def test_a_bad_handle_is_a_validation_result_from_send():
    result = correspond.send(
        "fake:example/demo", "hi", notify=["octocat"], dry_run=True, registry={"fake": FakeChannel()}
    )
    assert result.ok
    gh = GitHub(run=lambda *a, **k: pytest.fail("no gh"))
    bad = correspond.send(
        f"github:{REPO}#1", "hi", notify=["not a handle"], dry_run=True, registry={"github": gh}
    )
    assert not bad.ok and bad.error_kind == "validation"


def test_mentions_in_code_or_comments_do_not_count():
    text = "log:\n```\nby @octocat\n```\nand `@octocat` <!-- @octocat -->"
    assert missing_mentions(text, ["octocat"]) == ("octocat",)
    assert with_mentions(text, ["octocat"]).endswith("cc @octocat")
