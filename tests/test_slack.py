"""Slack: Markdown rendered as mrkdwn, a dry run that is the draft to paste, a post through an incoming webhook, a webhook URL that never leaks."""

import json

import pytest

import correspond
from correspond.channels._http import Response
from correspond.channels.slack import MAX_TEXT_CHARS, Slack, to_mrkdwn
from correspond.errors import InvalidRef, NotSupported

WEBHOOK = "https://hooks.slack.com/services/T000/B000/example-secret-part"


class FakeWebhook:
    def __init__(self, status=200, body=b"ok"):
        self.status, self.body = status, body
        self.posts = []

    def __call__(self, method, url, *, headers=None, body=None, timeout=None):
        self.posts.append((method, url, json.loads(body)))
        return Response(self.status, {}, self.body)


def _registry(http=None, keychain=lambda service: ""):
    return {"slack": Slack(http=http or FakeWebhook(), keychain=keychain)}


@pytest.mark.parametrize(
    "markdown, mrkdwn",
    [
        ("**bold** and __bold__", "*bold* and *bold*"),
        ("*italic* and _italic_", "_italic_ and _italic_"),
        ("~~gone~~", "~gone~"),
        ("[docs](https://example.org/d)", "<https://example.org/d|docs>"),
        ("<https://example.org/x>", "<https://example.org/x>"),
        ("![chart](https://example.org/c.png)", "<https://example.org/c.png|chart>"),
        ("## Summary ##", "*Summary*"),
        ("- one\n  * two\n+ three", "• one\n  • two\n• three"),
        ("1. first\n2. second", "1. first\n2. second"),
        ("> a quote\nand 1 > 0", "> a quote\nand 1 &gt; 0"),
        ("Tom & Jerry <3", "Tom &amp; Jerry &lt;3"),
        ("`a < b` and `**x**`", "`a &lt; b` and `**x**`"),
        ("```\nif a < b:\n    **x**\n```", "```\nif a &lt; b:\n    **x**\n```"),
        ("| a | b |\n|---|---|\n| 1 | 2 |", "```\n| a | b |\n|---|---|\n| 1 | 2 |\n```"),
        ("2*3*4 and snake_case_name", "2*3*4 and snake_case_name"),
        ("---", "──────────"),
    ],
)
def test_markdown_renders_as_mrkdwn(markdown, mrkdwn):
    assert to_mrkdwn(markdown) == mrkdwn


def test_a_draft_cannot_carry_a_mention_that_notifies_people():
    assert to_mrkdwn("<!channel> <!here> <@U123> <#C1>") == (
        "&lt;!channel&gt; &lt;!here&gt; &lt;@U123&gt; &lt;#C1&gt;"
    )


def test_a_dry_run_needs_nothing_configured_and_is_the_draft_to_paste(monkeypatch):
    monkeypatch.delenv("CORRESPOND_SLACK_WEBHOOK_TEAM_UPDATES", raising=False)
    asked = []
    registry = _registry(keychain=lambda service: asked.append(service) or "")
    planned = correspond.send(
        "slack:#Team-Updates",
        "**Shipped** the [export](https://example.org/e)",
        title="Release 1.2",
        dry_run=True,
        registry=registry,
    )
    assert planned.ok and planned.dry_run and planned.conversation == "slack:team-updates"
    assert (
        planned.plan["text"]
        == "*Release 1.2*\n*Shipped* the <https://example.org/e|export>"
    )
    assert planned.plan["characters"] == len(planned.plan["text"])
    assert "CORRESPOND_SLACK_WEBHOOK_TEAM_UPDATES" in planned.plan["webhook"]
    assert asked == [], "a dry run does not open the Keychain"


def test_a_send_posts_the_rendered_text_through_the_webhook(monkeypatch):
    monkeypatch.setenv("CORRESPOND_SLACK_WEBHOOK_TEAM_UPDATES", WEBHOOK)
    hook = FakeWebhook()
    result = correspond.send("slack:team-updates", "**done**", registry=_registry(hook))
    assert result.ok and hook.posts == [("POST", WEBHOOK, {"text": "*done*"})]
    assert WEBHOOK not in json.dumps(
        result.to_dict()
    ) and "example-secret-part" not in json.dumps(result.to_dict())


def test_a_webhook_in_the_keychain_is_used_when_sending(monkeypatch):
    monkeypatch.delenv("CORRESPOND_SLACK_WEBHOOK_OPS", raising=False)
    hook = FakeWebhook()
    registry = _registry(
        hook,
        keychain=lambda service: (
            WEBHOOK if service == "correspond-slack-webhook-ops" else ""
        ),
    )
    assert correspond.send("slack:ops", "hi", registry=registry).ok
    assert hook.posts[0][1] == WEBHOOK


def test_without_a_webhook_a_send_says_how_to_make_one(monkeypatch):
    monkeypatch.delenv("CORRESPOND_SLACK_WEBHOOK_OPS", raising=False)
    result = correspond.send("slack:ops", "hi", registry=_registry())
    assert (result.ok, result.error_kind) == (False, "validation")
    assert (
        "Incoming Webhooks" in result.error
        and "CORRESPOND_SLACK_WEBHOOK_OPS" in result.error
    )


def test_a_webhook_that_is_not_slacks_is_refused_without_showing_it(monkeypatch):
    monkeypatch.setenv(
        "CORRESPOND_SLACK_WEBHOOK_OPS", "https://example.org/collect?key=example-secret"
    )
    hook = FakeWebhook()
    result = correspond.send("slack:ops", "hi", registry=_registry(hook))
    assert (result.ok, result.error_kind) == (False, "validation") and hook.posts == []
    assert "example-secret" not in result.error


@pytest.mark.parametrize(
    "status, body, kind, retryable",
    [
        (400, b"invalid_payload", "validation", False),
        (403, b"action_prohibited", "permission", False),
        (404, b"channel_not_found", "not_found", False),
        (410, b"channel_is_archived", "not_found", False),
        (429, b"rate_limited", "rate_limited", True),
        (500, b"", "unavailable", True),
    ],
)
def test_slacks_refusals_are_classified(monkeypatch, status, body, kind, retryable):
    monkeypatch.setenv("CORRESPOND_SLACK_WEBHOOK_OPS", WEBHOOK)
    result = correspond.send(
        "slack:ops", "hi", registry=_registry(FakeWebhook(status, body))
    )
    assert (result.ok, result.error_kind, result.retryable) == (False, kind, retryable)
    assert "example-secret-part" not in result.error


def test_what_a_webhook_cannot_do_is_refused_by_name(monkeypatch):
    registry = _registry()
    with pytest.raises(NotSupported, match="reply"):
        correspond.send("slack:ops", "x", reply_to="1700000000.000100", registry=registry)
    drafted = correspond.send(
        "slack:ops",
        "**yes**",
        reply_to="1700000000.000100",
        dry_run=True,
        registry=registry,
    )
    assert drafted.ok and drafted.plan["paste_in_thread_of"] == "1700000000.000100"
    with pytest.raises(NotSupported, match="notify"):
        correspond.send("slack:ops", "x", notify=["ada"], registry=registry)
    with pytest.raises(NotSupported):
        correspond.read("slack:ops", registry=registry)
    too_long = correspond.send(
        "slack:ops", "x" * (MAX_TEXT_CHARS + 1), dry_run=True, registry=registry
    )
    assert (too_long.ok, too_long.error_kind) == (False, "validation")


def test_references_are_names_never_webhook_urls():
    slack = Slack()
    assert slack.parse_ref("#Team-Updates_2").encoded == "slack:team-updates_2"
    assert slack.parse_ref("C024BE91L").encoded == "slack:C024BE91L", (
        "an id keeps its case"
    )
    for bad in ("", "#", "a b", WEBHOOK, "x" * 81, "-lead", "a.b"):
        with pytest.raises(InvalidRef):
            slack.parse_ref(bad)


def test_the_audience_is_the_channels_members_never_listed():
    audience = correspond.audience("slack:ops", registry=_registry())
    assert (audience.scope.value, audience.complete) == ("group", False)


def test_requirements_explain_the_webhook():
    from correspond import tools

    report = tools.requirements("slack")
    assert "Incoming Webhooks" in json.dumps(report)


@pytest.mark.parametrize(
    "markdown",
    [
        "[ping](!channel)",
        "[x](@U123ABC)",
        "[](!here)",
        "[x](!subteam^S1)",
        "[x](#C1)",
        "[x](<!channel>)",
        "<!everyone>",
        "[mail](mailto:<!channel>)",
    ],
)
def test_no_link_can_become_a_mention(markdown):
    rendered = to_mrkdwn(markdown)
    for live in ("<!", "<@", "<#"):
        assert live not in rendered, rendered


def test_a_title_cannot_carry_a_mention_either():
    planned = correspond.send(
        "slack:ops", "x", title="[t](!here)", dry_run=True, registry=_registry()
    )
    assert "<!" not in planned.plan["text"]


@pytest.mark.parametrize(
    "markdown", ["`\x000\x00`", "a \x000\x00 b", "`a` \x000\x00", "\x00\x00\x001\x00"]
)
def test_input_cannot_forge_the_renderers_placeholders(markdown):
    rendered = to_mrkdwn(markdown.encode().decode("unicode_escape"))
    assert "\x00" not in rendered


@pytest.mark.parametrize(
    "markdown", ["[" * 40_000, "[a](" * 10_000, "*_" * 20_000, "**" * 20_000]
)
def test_long_adversarial_input_renders_quickly(markdown):
    import time

    start = time.perf_counter()
    to_mrkdwn(markdown)
    assert time.perf_counter() - start < 2


def test_a_table_inside_a_code_fence_stays_in_the_fence():
    assert to_mrkdwn("```\n| a | b |\n|---|---|\n```\n**after**") == (
        "```\n| a | b |\n|---|---|\n```\n*after*"
    )


def test_a_line_of_pipes_without_a_separator_is_not_a_table():
    assert to_mrkdwn("|a|") == "|a|"


@pytest.mark.parametrize(
    "markdown, mrkdwn",
    [
        ("[wiki](https://example.org/Foo_(bar))", "<https://example.org/Foo_(bar)|wiki>"),
        ("[q](https://example.org/a?x=1&y=2)", "<https://example.org/a?x=1&y=2|q>"),
        ("[p](https://example.org/a|b)", "<https://example.org/a%7Cb|p>"),
        ("# **Head** _x_", "*Head _x_*"),
        ("***both***", "*_both_*"),
        ("\\*not italic\\*", "*not italic*"),
        ("- - -", "──────────"),
        ("Tom &amp; Jerry", "Tom &amp; Jerry"),
        (
            "https://example.org/a_b_c and snake_case",
            "https://example.org/a_b_c and snake_case",
        ),
    ],
)
def test_review_cases_render_as_slack_expects(markdown, mrkdwn):
    assert to_mrkdwn(markdown) == mrkdwn


@pytest.mark.parametrize(
    "url",
    [
        "https://hooks.slack.com.evil.example/services/T/B/x",
        "https://hooks.slack.com/services/T/B/with space",
        "https://hooks.slack.com/services/T/B/x\ty",
        "http://hooks.slack.com/services/T/B/x",
    ],
)
def test_a_malformed_or_lookalike_webhook_is_refused_without_showing_it(monkeypatch, url):
    monkeypatch.setenv(
        "CORRESPOND_SLACK_WEBHOOK_OPS", url.encode().decode("unicode_escape")
    )
    hook = FakeWebhook()
    result = correspond.send("slack:ops", "hi", registry=_registry(hook))
    assert (result.ok, result.error_kind, hook.posts) == (False, "validation", [])
    assert "services/T/B" not in result.error


def test_govslack_webhooks_are_accepted(monkeypatch):
    monkeypatch.setenv(
        "CORRESPOND_SLACK_WEBHOOK_OPS", "https://hooks.slack-gov.com/services/T/B/x"
    )
    assert correspond.send("slack:ops", "hi", registry=_registry()).ok


def test_names_that_differ_only_by_dash_or_underscore_use_different_webhooks():
    from correspond.channels.slack import webhook_env

    assert webhook_env("a-b") != webhook_env("a_b")


def test_a_malformed_url_never_reaches_an_error_through_the_stdlib_transport():
    from correspond.channels._http import urllib_http
    from correspond.errors import ChannelError

    with pytest.raises(ChannelError) as caught:
        urllib_http("POST", "https://hooks.slack.com/services/T/B/sec ret", body=b"{}")
    assert "sec ret" not in str(caught.value) and "services" not in str(caught.value)
