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
    assert any(
        "CORRESPOND_SLACK_WEBHOOK_TEAM_UPDATES" in item
        for item in planned.plan["looked_up_when_sending"]
    )
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
    with pytest.raises(correspond.MissingRequirement, match="CORRESPOND_SLACK_TOKEN"):
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
    "markdown",
    ["[" * 40_000, "[a](" * 10_000, "*_" * 20_000, "**" * 20_000],
    ids=["brackets", "unclosed-links", "emphasis-runs", "bold-runs"],
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


# ------------------------------------------------------------- the Web API, with a token

TOKEN = "xoxb-fake"  # fictional; short enough not to look like a real token
CHANNEL = "C024BE91L"


class FakeSlackApi:
    """The Web API methods the adapter calls, answering as Slack does (HTTP 200, ok true or false)."""

    def __init__(
        self,
        history=(),
        channels=(),
        errors=None,
        pages=1,
        users=None,
        replies=None,
        *,
        token=TOKEN,
        channel_page=None,
    ):
        self.token, self.channel_page = token, channel_page
        self.history = sorted(history, key=lambda m: float(m["ts"]), reverse=True)
        self.channels = list(channels)
        self.users = dict(users or {})
        self.replies = dict(replies or {})
        self.errors = dict(errors or {})
        self.pages = pages
        self.calls = []

    def __call__(self, method, url, *, headers=None, body=None, timeout=None):
        from urllib.parse import parse_qsl

        name = url.rsplit("/", 1)[-1]
        assert headers["Authorization"] == f"Bearer {self.token}"
        params = dict(parse_qsl(body.decode())) if body else {}
        self.calls.append((name, params))
        if name in self.errors:
            return Response(
                200, {}, json.dumps({"ok": False, "error": self.errors[name]}).encode()
            )
        if name == "conversations.list":
            size = self.channel_page or len(self.channels) or 1
            start = int(params.get("cursor") or 0)
            more = start + size < len(self.channels)
            return self._json(
                {
                    "ok": True,
                    "channels": self.channels[start : start + size],
                    "response_metadata": {
                        "next_cursor": str(start + size) if more else ""
                    },
                }
            )
        if name == "conversations.history":
            found = [
                m
                for m in self.history
                if (
                    float(m["ts"]) >= float(params.get("oldest") or 0)
                    if params.get("inclusive") == "true"
                    else float(m["ts"]) > float(params.get("oldest") or 0)
                )
                and (
                    float(m["ts"]) <= float(params.get("latest") or "inf")
                    if params.get("inclusive") == "true"
                    else float(m["ts"]) < float(params.get("latest") or "inf")
                )
            ]
            size = int(params.get("limit") or 200)
            start = int(params.get("cursor") or 0)
            page = found[start : start + size]
            more = start + size < len(found)
            return self._json(
                {
                    "ok": True,
                    "messages": page,
                    "has_more": more,
                    "response_metadata": {
                        "next_cursor": str(start + size) if more else ""
                    },
                }
            )
        if name == "users.info":
            user = self.users.get(params["user"])
            if user is None:
                return self._json({"ok": False, "error": "user_not_found"})
            return self._json({"ok": True, "user": user})
        if name == "conversations.replies":
            thread = sorted(
                self.replies.get(params["ts"], ()), key=lambda m: float(m["ts"])
            )
            inclusive = params.get("inclusive") == "true"
            oldest = float(params.get("oldest") or 0)
            latest = float(params.get("latest") or "inf")
            found = [
                m
                for m in thread
                if (
                    (float(m["ts"]) >= oldest if inclusive else float(m["ts"]) > oldest)
                    and (float(m["ts"]) <= latest if inclusive else float(m["ts"]) < latest)
                )
                or m["ts"] == params["ts"]  # Slack always returns the parent
            ]
            size = int(params.get("limit") or 200)
            start = int(params.get("cursor") or 0)
            more = start + size < len(found)
            return self._json(
                {
                    "ok": True,
                    "messages": found[start : start + size],
                    "has_more": more,
                    "response_metadata": {
                        "next_cursor": str(start + size) if more else ""
                    },
                }
            )
        if name == "auth.test":
            return self._json(
                {"ok": True, "user_id": "U0BOT", "bot_id": "B0BOT", "team": "Example"}
            )
        if name == "chat.postMessage":
            return self._json(
                {
                    "ok": True,
                    "ts": "1760000100.000200",
                    "message": {
                        "user": "U0BOT",
                        "text": params["text"],
                        "ts": "1760000100.000200",
                    },
                }
            )
        raise AssertionError(name)

    @staticmethod
    def _json(data):
        return Response(200, {}, json.dumps(data).encode())


def _msg(ts, text="hello", **extra):
    return {"type": "message", "user": "U0ADA", "text": text, "ts": ts, **extra}


HISTORY = [
    _msg("1760000000.000100", "first"),
    _msg("1760000010.000100", "second", thread_ts="1760000010.000100", reply_count=2),
    _msg(
        "1760000020.000100",
        "a thread reply shown in the channel",
        thread_ts="1760000010.000100",
    ),
    _msg(
        "1760000030.000100",
        "*bot* says",
        bot_id="B01",
        user=None,
        bot_profile={"name": "Example Bot"},
        files=[
            {
                "id": "F1",
                "name": "report.pdf",
                "mimetype": "application/pdf",
                "size": 8,
                "url_private": "https://files.slack.com/F1",
            }
        ],
    ),
]


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setenv("CORRESPOND_SLACK_TOKEN", TOKEN)


#: This clock: an hour after the fixtures, so the walk reaches them (it stops a minute short of now).
NOW = 1760003640.0


def _api_registry(api):
    return {"slack": Slack(http=api, keychain=lambda service: "", clock=lambda: NOW)}


def test_read_returns_the_latest_messages_oldest_first(token):
    api = FakeSlackApi(HISTORY)
    messages = correspond.read(f"slack:{CHANNEL}", limit=3, registry=_api_registry(api))
    assert [m.text for m in messages] == [
        "second",
        "a thread reply shown in the channel",
        "*bot* says",
    ]
    parent, reply, bot = messages
    assert (
        parent.reply_to is None
        and reply.reply_to == reply.thread_root == "1760000010.000100"
    )
    assert bot.author.is_bot and bot.author.display_name == "Example Bot"
    assert bot.attachments[0].name == "report.pdf" and bot.body_format == "mrkdwn"
    assert set(bot.native) <= set(Slack().capabilities.native_fields)


def test_a_channel_named_is_looked_up_once(token):
    api = FakeSlackApi(
        HISTORY,
        channels=[
            {"id": "C0OTHER01", "name": "random"},
            {"id": CHANNEL, "name": "releases"},
        ],
    )
    registry = _api_registry(api)
    correspond.read("slack:#releases", registry=registry)
    correspond.read("slack:releases", registry=registry)
    assert [name for name, _ in api.calls].count("conversations.list") == 1
    assert {
        p.get("channel") for name, p in api.calls if name == "conversations.history"
    } == {CHANNEL}
    with pytest.raises(correspond.ChannelError) as caught:
        correspond.read("slack:nowhere", registry=registry)
    assert caught.value.kind == "not_found"


def test_listen_polls_by_ts_and_sets_a_cursor_on_a_quiet_first_poll(token):
    api = FakeSlackApi(HISTORY)
    cursors = {f"slack:{CHANNEL}": "1760000010.000100"}
    events = list(
        correspond.listen(
            f"slack:{CHANNEL}", cursors=cursors, registry=_api_registry(api)
        )
    )
    assert [e.message.id for e in events] == ["1760000020.000100", "1760000030.000100"]
    assert cursors[f"slack:{CHANNEL}"].split("/")[0] == f"{NOW - 60:.6f}", (
        "the end of the windows read"
    )
    quiet = {}
    assert (
        list(
            correspond.listen(
                f"slack:{CHANNEL}", cursors=quiet, registry=_api_registry(FakeSlackApi())
            )
        )
        == []
    )
    assert quiet[f"slack:{CHANNEL}"].split("/")[0] == f"{NOW - 60:.6f}", (
        "a minute behind this clock"
    )


def test_listen_reads_every_page_and_a_limit_drains_over_polls(token):
    many = [_msg(f"17600001{i:02d}.000000", f"m{i}") for i in range(30)]
    api = FakeSlackApi(many)
    from correspond.channels import slack as module

    cursors = {f"slack:{CHANNEL}": "1760000000.000000"}
    original = module.PAGE_SIZE
    module.PAGE_SIZE = 7
    try:
        seen = []
        for _ in range(4):
            seen += [
                e.message.text
                for e in correspond.listen(
                    f"slack:{CHANNEL}",
                    cursors=cursors,
                    limit=10,
                    registry=_api_registry(api),
                )
            ]
    finally:
        module.PAGE_SIZE = original
    assert seen == [f"m{i}" for i in range(30)]


def test_send_with_a_token_posts_and_replies_in_a_thread(token):
    api = FakeSlackApi()
    registry = _api_registry(api)
    planned = correspond.send(
        f"slack:{CHANNEL}",
        "**yes**",
        reply_to="1760000010.000100",
        dry_run=True,
        registry=registry,
    )
    assert (
        planned.plan["in_thread_of"] == "1760000010.000100"
        and "chat.postMessage" in planned.plan["action"]
    )
    assert api.calls == []
    result = correspond.send(
        f"slack:{CHANNEL}", "**yes**", reply_to="1760000010.000100", registry=registry
    )
    assert result.ok and result.message_id == "1760000100.000200"
    assert result.account.acts_as == "bot" and result.account.id == "U0BOT"
    (name, params), (who, _) = api.calls
    assert who == "auth.test"
    assert (name, params["text"], params["thread_ts"], params["channel"]) == (
        "chat.postMessage",
        "*yes*",
        "1760000010.000100",
        CHANNEL,
    )
    assert TOKEN not in json.dumps(result.to_dict())


@pytest.mark.parametrize(
    "error, kind",
    [
        ("invalid_auth", "auth"),
        ("not_in_channel", "permission"),
        ("channel_not_found", "not_found"),
        ("msg_too_long", "validation"),
        ("fatal_error", "unavailable"),
    ],
)
def test_web_api_errors_become_classified_errors(token, error, kind):
    api = FakeSlackApi(errors={"chat.postMessage": error})
    result = correspond.send(f"slack:{CHANNEL}", "x", registry=_api_registry(api))
    assert (result.ok, result.error_kind) == (False, kind)
    assert TOKEN not in result.error


def test_a_rate_limit_carries_its_wait(token):
    def limited(method, url, **kwargs):
        return Response(429, {"retry-after": "30"}, b"")

    with pytest.raises(correspond.ChannelError) as caught:
        correspond.read(f"slack:{CHANNEL}", registry=_api_registry(limited))
    assert (caught.value.kind, caught.value.retry_after) == ("rate_limited", 30.0)


def test_a_reply_to_that_is_not_a_ts_is_refused(token):
    result = correspond.send(
        f"slack:{CHANNEL}",
        "x",
        reply_to="not-a-ts",
        registry=_api_registry(FakeSlackApi()),
    )
    assert (result.ok, result.error_kind) == (False, "validation")


def test_a_file_is_fetched_with_the_token(token):
    fetched = []

    def http(method, url, *, headers=None, body=None, timeout=None):
        if url.startswith("https://files.slack.com/"):
            fetched.append(headers["Authorization"])
            return Response(200, {}, b"%PDF")
        return FakeSlackApi(HISTORY)(method, url, headers=headers, body=body)

    bot = correspond.read(f"slack:{CHANNEL}", limit=1, registry=_api_registry(http))[-1]
    assert bot.attachments[0].content() == b"%PDF" and fetched == [f"Bearer {TOKEN}"]


def _signed(secret, body, stamp):
    import hashlib
    import hmac

    base = f"v0:{stamp}:".encode() + body
    return {
        "X-Slack-Request-Timestamp": str(stamp),
        "X-Slack-Signature": "v0="
        + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest(),
    }


def test_verify_checks_the_signature_and_refuses_a_replay(monkeypatch):
    monkeypatch.setenv("CORRESPOND_SLACK_SIGNING_SECRET", "example-signing-secret")
    registry = _api_registry(FakeSlackApi())
    body = b'{"type":"event_callback"}'
    now = int(NOW)
    good = correspond.verify(
        "slack", _signed("example-signing-secret", body, now), body, registry=registry
    )
    assert good.grade.value == "crypto"
    for headers, reason in (
        (_signed("wrong-secret", body, now), "mismatch"),
        (_signed("example-signing-secret", body, now - 600), "stale"),
        ({}, "no timestamp"),
    ):
        bad = correspond.verify("slack", headers, body, registry=registry)
        assert bad.grade.value == "forged" and reason in bad.evidence["signature"]
    tampered = correspond.verify(
        "slack",
        _signed("example-signing-secret", body, now),
        body + b" ",
        registry=registry,
    )
    assert tampered.grade.value == "forged"


def test_verify_fails_closed_on_odd_inputs(monkeypatch):
    monkeypatch.setenv("CORRESPOND_SLACK_SIGNING_SECRET", "example-signing-secret")
    registry = _api_registry(FakeSlackApi())
    body = b'{"type":"event_callback"}'
    now = int(NOW)
    signed = _signed("example-signing-secret", body, now)
    class Unprintable:
        def __str__(self):
            raise RuntimeError("no")

    cases = [
        ({"X-Slack-Signature": Unprintable()}, body, "no headers"),
        (signed, body.decode(), "body is not bytes"),
        (signed, None, "body is not bytes"),
        (None, body, "no headers"),
        ("not a mapping", body, "no headers"),
        ({b"X-Slack-Signature": b"v0=00"}, body, "no timestamp"),
        (_signed("example-signing-secret", body, "9" * 5000), body, "no timestamp"),
        ({**signed, "X-Slack-Request-Timestamp": "\u0661\u0662"}, body, "no timestamp"),
        ({"X-Slack-Request-Timestamp": str(now)}, body, "missing"),
        ({**signed, "X-Slack-Signature": "v0=\u00e9"}, body, "mismatch"),
        (_signed("example-signing-secret", body, now + 600), body, "stale"),  # future-dated
        (_signed("wrong-secret", body, now - 600), body, "mismatch"),  # forged beats stale
    ]
    for headers, raw, reason in cases:
        verdict = correspond.verify("slack", headers, raw, registry=registry)
        assert verdict.grade.value == "forged" and reason in verdict.evidence["signature"], reason
    for raw in (bytearray(body), memoryview(body)):
        assert correspond.verify("slack", signed, raw, registry=registry).grade.value == "crypto"


THREAD = "1760000010.000100"
REPLIES = {
    THREAD: [
        _msg(THREAD, "the parent", thread_ts=THREAD, reply_count=3),
        _msg("1760000015.000100", "a reply inside the thread", thread_ts=THREAD),
        _msg("1760000025.000100", "another reply", thread_ts=THREAD, user="U0BEA"),
        _msg("1760000035.000100", "a third", thread_ts=THREAD),
    ]
}


def test_a_thread_reference_parses_and_reads_its_replies(token):
    ref = Slack().parse_ref(f"#Releases/{THREAD}")
    assert (ref.id, ref.kind, ref.parent.id) == (f"releases/{THREAD}", "thread", "releases")
    for bad in (f"{CHANNEL}/not-a-ts", f"{CHANNEL}/"):
        with pytest.raises(correspond.InvalidRef):
            Slack().parse_ref(bad)
    api = FakeSlackApi(HISTORY, replies=REPLIES)
    messages = correspond.read(f"slack:{CHANNEL}/{THREAD}", registry=_api_registry(api))
    assert [m.text for m in messages][:2] == ["the parent", "a reply inside the thread"]
    assert messages[1].thread_root == THREAD and str(messages[1].conversation) == f"slack:{CHANNEL}/{THREAD}"
    assert [p["ts"] for name, p in api.calls if name == "conversations.replies"] == [THREAD]


def test_listen_to_a_thread_delivers_new_replies_and_resumes(token):
    registry = _api_registry(FakeSlackApi(replies=REPLIES))
    ref = f"slack:{CHANNEL}/{THREAD}"
    cursors = {ref: "1760000015.000100"}
    first = list(correspond.listen(ref, cursors=cursors, limit=1, registry=registry))
    assert [e.message.text for e in first] == ["another reply"]
    rest = list(correspond.listen(ref, cursors=cursors, registry=registry))
    assert [e.message.text for e in rest] == ["a third"]
    assert list(correspond.listen(ref, cursors=cursors, registry=registry)) == []
    assert cursors[ref] == "1760000035.000100"


def test_send_to_a_thread_reference_replies_in_it(token):
    api = FakeSlackApi()
    sent = correspond.send(f"slack:{CHANNEL}/{THREAD}", "noted", registry=_api_registry(api))
    assert sent.ok
    posted = [p for name, p in api.calls if name == "chat.postMessage"][-1]
    assert (posted["channel"], posted["thread_ts"]) == (CHANNEL, THREAD)


def test_authors_carry_their_handle_asked_once(token):
    users = {
        "U0ADA": {
            "id": "U0ADA",
            "name": "ada",
            "profile": {"display_name": "Ada", "real_name": "Ada Example"},
        }
    }
    api = FakeSlackApi(HISTORY, users=users, replies=REPLIES)
    registry = _api_registry(api)
    messages = correspond.read(f"slack:{CHANNEL}", registry=registry)
    ada = [m for m in messages if m.author.native_id == "U0ADA"][0]
    assert (ada.author.handle, ada.author.display_name) == ("ada", "Ada")
    correspond.read(f"slack:{CHANNEL}/{THREAD}", registry=registry)  # U0BEA is unknown
    asked = [p["user"] for name, p in api.calls if name == "users.info"]
    assert asked.count("U0ADA") == 1 and asked.count("U0BEA") == 1
    beatrice = [
        m
        for m in correspond.read(f"slack:{CHANNEL}/{THREAD}", registry=registry)
        if m.author.native_id == "U0BEA"
    ][0]
    assert beatrice.author.handle is None  # Slack would not say: the id alone
    assert [p["user"] for name, p in api.calls if name == "users.info"].count("U0BEA") == 1


def test_a_renamed_channel_is_found_by_its_new_name(token):
    clock = [NOW]
    api = FakeSlackApi(HISTORY, channels=[{"id": CHANNEL, "name": "releases"}])
    registry = {
        "slack": Slack(http=api, keychain=lambda service: "", clock=lambda: clock[0])
    }
    correspond.read("slack:releases", registry=registry)
    api.channels = [{"id": CHANNEL, "name": "shipped"}]  # renamed in Slack
    correspond.read("slack:releases", registry=registry)  # within the cache's lifetime
    clock[0] += 3600  # past it: the old name no longer resolves
    with pytest.raises(correspond.ChannelError) as caught:
        correspond.read("slack:releases", registry=registry)
    assert caught.value.kind == "not_found"
    correspond.read("slack:shipped", registry=registry)
    assert [name for name, _ in api.calls].count("conversations.list") == 2


def test_a_file_outside_slack_never_gets_the_token(token):
    sent = []

    def http(method, url, *, headers=None, body=None, timeout=None):
        if "slack.com/api/" not in url:
            sent.append((url, headers))
            return Response(200, {}, b"x")
        return FakeSlackApi(
            [
                _msg(
                    "1760000030.000100",
                    "x",
                    files=[
                        {
                            "id": "F2",
                            "name": "a.pdf",
                            "mimetype": "application/pdf",
                            "url_private": "https://docs.example.org/a.pdf",
                        }
                    ],
                )
            ]
        )(method, url, headers=headers, body=body)

    message = correspond.read(f"slack:{CHANNEL}", registry=_api_registry(http))[-1]
    with pytest.raises(correspond.ChannelError, match="outside Slack"):
        message.attachments[0].content()
    assert sent == []


def test_a_sign_in_page_is_not_handed_back_as_the_file(token):
    def http(method, url, *, headers=None, body=None, timeout=None):
        if url.startswith("https://files.slack.com/"):
            return Response(
                200, {"content-type": "text/html; charset=utf-8"}, b"<html>sign in</html>"
            )
        return FakeSlackApi(HISTORY)(method, url, headers=headers, body=body)

    bot = correspond.read(f"slack:{CHANNEL}", limit=1, registry=_api_registry(http))[-1]
    with pytest.raises(correspond.ChannelError, match="files:read"):
        bot.attachments[0].content()


def test_a_rate_limit_stops_a_poll_where_it_got_to_and_the_next_resumes(token):
    many = [_msg(f"{1760000000 + 600 * i}.000000", f"m{i}") for i in range(10)]
    inner = FakeSlackApi(many)
    state = {"history": 0}

    def http(method, url, *, headers=None, body=None, timeout=None):
        if "conversations.history" in url:
            state["history"] += 1
            if state["history"] == 3:
                return Response(429, {"retry-after": "60"}, b"")
        return inner(method, url, headers=headers, body=body)

    registry = {
        "slack": Slack(http=http, keychain=lambda s: "", clock=lambda: 1760010000.0)
    }
    cursors = {f"slack:{CHANNEL}": "1759999000.000000"}
    first = [
        e.message.text
        for e in correspond.listen(f"slack:{CHANNEL}", cursors=cursors, registry=registry)
    ]
    assert first and first == [f"m{i}" for i in range(len(first))], (
        "what was read is delivered"
    )
    rest = [
        e.message.text
        for e in correspond.listen(f"slack:{CHANNEL}", cursors=cursors, registry=registry)
    ]
    assert first + rest == [f"m{i}" for i in range(10)]


def test_a_backlog_on_the_restricted_tier_drains_a_little_each_poll(token):
    """One history request a minute, 15 messages: every poll still moves forward."""
    many = [_msg(f"{1760000000 + 60 * i}.000000", f"m{i}") for i in range(40)]
    inner = FakeSlackApi(many)
    state = {"allowed": 0}

    def restricted(method, url, *, headers=None, body=None, timeout=None):
        if "conversations.history" in url:
            if state["allowed"] == 0:
                return Response(429, {"retry-after": "60"}, b"")
            state["allowed"] -= 1
        return inner(method, url, headers=headers, body=body)

    from correspond.channels import slack as module

    original, module.PAGE_SIZE = module.PAGE_SIZE, 15
    try:
        registry = {
            "slack": Slack(
                http=restricted, keychain=lambda s: "", clock=lambda: 1760010000.0
            )
        }
        cursors = {f"slack:{CHANNEL}": "1759999999.000000"}
        seen = []
        for _ in range(60):
            state["allowed"] = 2  # a request or two between polls
            try:
                seen += [
                    e.message.text
                    for e in correspond.listen(
                        f"slack:{CHANNEL}", cursors=cursors, registry=registry
                    )
                ]
            except correspond.ChannelError:
                pass
            if len(seen) == 40:
                break
    finally:
        module.PAGE_SIZE = original
    assert seen == [f"m{i}" for i in range(40)]


def test_slacks_own_notices_are_not_messages_but_the_cursor_moves_past_them(token):
    api = FakeSlackApi(
        [
            _msg("1760000011.000000", "joined", subtype="channel_join"),
            _msg("1760000012.000000", "hi"),
        ]
    )
    cursors = {f"slack:{CHANNEL}": "1760000010.000000"}
    events = list(
        correspond.listen(
            f"slack:{CHANNEL}", cursors=cursors, registry=_api_registry(api)
        )
    )
    assert [e.message.text for e in events] == ["hi"]
    only_notice = FakeSlackApi(
        [_msg("1760000013.000000", "left", subtype="channel_leave")]
    )
    cursors = {f"slack:{CHANNEL}": "1760000012.000000"}
    assert (
        list(
            correspond.listen(
                f"slack:{CHANNEL}", cursors=cursors, registry=_api_registry(only_notice)
            )
        )
        == []
    )
    assert float(cursors[f"slack:{CHANNEL}"].split("/")[0]) >= 1760000013.0, (
        "past the notice"
    )
    assert [
        m.text for m in correspond.read(f"slack:{CHANNEL}", registry=_api_registry(api))
    ] == ["hi"]


def test_a_dry_run_says_whose_name_the_post_goes_out_under(monkeypatch):
    for token, words in (
        ("xoxb-fake", "the app"),
        ("xoxp-fake", "the person"),
        ("xoxe.xoxb-fake", "the app"),
    ):
        monkeypatch.setenv("CORRESPOND_SLACK_TOKEN", token)
        planned = correspond.send(
            f"slack:{CHANNEL}", "x", dry_run=True, registry=_api_registry(FakeSlackApi())
        )
        assert words in planned.plan["posts_as"]


def test_a_user_token_is_reported_as_acting_as_the_person(monkeypatch):
    class UserApi(FakeSlackApi):
        def __call__(self, method, url, *, headers=None, body=None, timeout=None):
            if url.endswith("auth.test"):
                return self._json({"ok": True, "user_id": "U0ADA"})
            return super().__call__(method, url, headers=headers, body=body)

    monkeypatch.setenv("CORRESPOND_SLACK_TOKEN", TOKEN)
    result = correspond.send(f"slack:{CHANNEL}", "x", registry=_api_registry(UserApi()))
    assert (result.account.acts_as, result.account.id) == ("user", "U0ADA")


def test_a_channels_own_webhook_wins_for_a_plain_post_and_the_token_threads(
    token, monkeypatch
):
    monkeypatch.setenv(f"CORRESPOND_SLACK_WEBHOOK_{CHANNEL}", WEBHOOK)
    hooked = []

    def http(method, url, *, headers=None, body=None, timeout=None):
        if url == WEBHOOK:
            hooked.append(json.loads(body))
            return Response(200, {}, b"ok")
        return FakeSlackApi()(method, url, headers=headers, body=body)

    registry = _api_registry(http)
    assert (
        correspond.send(f"slack:{CHANNEL}", "plain", registry=registry).plan["via"]
        == "webhook"
    )
    threaded = correspond.send(
        f"slack:{CHANNEL}", "reply", reply_to="1760000010.000100", registry=registry
    )
    assert (
        threaded.ok
        and threaded.message_id == "1760000100.000200"
        and hooked == [{"text": "plain"}]
    )


# ------------------------------------------------------------ the walk's review (#69)


def _walker(api, *, clock=1760010000.0):
    return {"slack": Slack(http=api, keychain=lambda s: "", clock=lambda: clock)}


def test_a_message_on_a_window_boundary_is_delivered_once(token):
    start = 1760000000.0
    on_edge = _msg(f"{start + 3600:.6f}", "on the edge")
    api = FakeSlackApi([on_edge, _msg(f"{start + 4000:.6f}", "after")])
    cursors = {f"slack:{CHANNEL}": f"{start:.6f}"}
    seen = []
    for _ in range(3):
        seen += [
            e.message.text
            for e in correspond.listen(
                f"slack:{CHANNEL}", cursors=cursors, registry=_walker(api)
            )
        ]
    assert seen == ["on the edge", "after"]


def test_one_request_a_poll_still_drains_a_dense_backlog(token):
    """The tier the walk is for: a narrowing is progress, kept in the cursor."""
    many = [_msg(f"{1760000000 + 60 * i}.000000", f"m{i}") for i in range(40)]
    inner = FakeSlackApi(many)
    state = {"allowed": 0}

    def one_a_poll(method, url, *, headers=None, body=None, timeout=None):
        if "conversations.history" in url:
            if not state["allowed"]:
                return Response(429, {"retry-after": "60"}, b"")
            state["allowed"] -= 1
        return inner(method, url, headers=headers, body=body)

    from correspond.channels import slack as module

    original, module.PAGE_SIZE = module.PAGE_SIZE, 15
    try:
        cursors = {f"slack:{CHANNEL}": "1759999999.000000"}
        seen = []
        for _ in range(80):
            state["allowed"] = 1
            seen += [
                e.message.text
                for e in correspond.listen(
                    f"slack:{CHANNEL}", cursors=cursors, registry=_walker(one_a_poll)
                )
            ]
            if len(seen) == 40:
                break
    finally:
        module.PAGE_SIZE = original
    assert seen == [f"m{i}" for i in range(40)]


def test_slacks_clock_bounds_the_walk_when_this_one_runs_ahead(token):
    from email.utils import formatdate

    slack_now = 1760010000.0
    later = _msg(f"{slack_now + 30:.6f}", "posted after the poll, by Slack's clock")

    class Behind(FakeSlackApi):
        def __call__(self, method, url, **kwargs):
            reply = super().__call__(method, url, **kwargs)
            return Response(
                reply.status, {"date": formatdate(slack_now, usegmt=True)}, reply.body
            )

    api = Behind([])
    cursors = {f"slack:{CHANNEL}": f"{slack_now - 7200:.6f}"}
    list(
        correspond.listen(
            f"slack:{CHANNEL}",
            cursors=cursors,
            registry=_walker(api, clock=slack_now + 120),
        )
    )
    assert float(cursors[f"slack:{CHANNEL}"].split("/")[0]) <= slack_now - 60
    api.history = [later]
    events = list(
        correspond.listen(
            f"slack:{CHANNEL}",
            cursors=cursors,
            registry=_walker(api, clock=slack_now + 240),
        )
    )
    assert [e.message.text for e in events] == ["posted after the poll, by Slack's clock"]


def test_a_plain_ts_cursor_from_before_still_works(token):
    api = FakeSlackApi([_msg("1760000100.000000", "hi")])
    cursors = {f"slack:{CHANNEL}": "1760000000.000000"}
    events = list(
        correspond.listen(f"slack:{CHANNEL}", cursors=cursors, registry=_walker(api))
    )
    assert [e.message.text for e in events] == ["hi"] and "/" in cursors[
        f"slack:{CHANNEL}"
    ]
    with pytest.raises(correspond.ChannelError):
        list(
            correspond.listen(
                f"slack:{CHANNEL}",
                cursors={f"slack:{CHANNEL}": "x/1"},
                registry=_walker(api),
            )
        )


def test_without_users_read_authors_are_asked_about_once(token):
    api = FakeSlackApi(HISTORY, errors={"users.info": "missing_scope"})
    correspond.read(f"slack:{CHANNEL}", registry=_api_registry(api))
    assert [name for name, _ in api.calls].count("users.info") == 1


def test_a_thread_reference_refuses_a_reply_to_another_thread(token):
    refused = correspond.send(
        f"slack:{CHANNEL}/{THREAD}",
        "noted",
        reply_to="1760000099.000100",
        dry_run=True,
        registry=_api_registry(FakeSlackApi()),
    )
    assert not refused.ok and refused.error_kind == "validation"


def test_a_thread_listen_leaves_the_newest_minute_for_the_next_poll(token):
    late = float(REPLIES[THREAD][-1]["ts"]) + 55  # the third reply is 55 s old
    api = FakeSlackApi(replies=REPLIES)
    registry = {"slack": Slack(http=api, keychain=lambda s: "", clock=lambda: late)}
    ref = f"slack:{CHANNEL}/{THREAD}"
    cursors = {ref: "1760000015.000100"}
    got = list(correspond.listen(ref, cursors=cursors, registry=registry))
    assert [e.message.text for e in got] == ["another reply"]
    assert cursors[ref] == "1760000025.000100"


def _long_thread(count, *, step=1.0, burst=0, burst_step=0.001, latest_reply=True):
    """A thread of ``count`` replies ``step`` s apart, its last ``burst`` of them ``burst_step`` s apart."""
    stamps, at = [], float(THREAD)
    for i in range(count):
        at += burst_step if i >= count - burst else step
        stamps.append(f"{at:.6f}")
    parent = _msg(THREAD, "the parent", thread_ts=THREAD, reply_count=count)
    if latest_reply:
        parent["latest_reply"] = stamps[-1]
    return {THREAD: [parent] + [_msg(ts, f"r{i}", thread_ts=THREAD) for i, ts in enumerate(stamps)]}


@pytest.mark.parametrize(
    "shape",
    [
        {},  # evenly spread
        {"step": 0.01},  # dense
        {"burst": 500},  # the newest 500 in half a second: the walk narrows its window
        {"latest_reply": False},  # no latest_reply on the parent: the walk starts at the clock
    ],
)
def test_reading_a_thread_longer_than_the_page_cap_returns_its_newest_replies(token, shape):
    from correspond.channels.slack import MAX_THREAD_PAGES

    replies = _long_thread(4500, **shape)
    api = FakeSlackApi(replies=replies)
    clock = float(replies[THREAD][-1]["ts"]) + 5
    registry = {"slack": Slack(http=api, keychain=lambda s: "", clock=lambda: clock)}
    messages = correspond.read(f"slack:{CHANNEL}/{THREAD}", registry=registry)
    assert [m.text for m in messages] == [f"r{i}" for i in range(4450, 4500)]
    more = correspond.read(f"slack:{CHANNEL}/{THREAD}", limit=300, registry=registry)
    assert [m.text for m in more] == [f"r{i}" for i in range(4200, 4500)]
    asked = [name for name, _ in api.calls].count("conversations.replies")
    assert asked <= 2 * 2 * MAX_THREAD_PAGES, asked


def test_a_long_threads_read_since_a_time_keeps_the_newest_after_it(token):
    from datetime import datetime, timezone

    replies = _long_thread(4500)
    api = FakeSlackApi(replies=replies)
    since = datetime.fromtimestamp(float(replies[THREAD][100]["ts"]) + 0.5, tz=timezone.utc)
    messages = correspond.read(
        f"slack:{CHANNEL}/{THREAD}", since=since, limit=500, registry=_api_registry(api)
    )
    assert [m.text for m in messages] == [f"r{i}" for i in range(4000, 4500)]
    short = correspond.read(  # since near the end: within the cap, read whole
        f"slack:{CHANNEL}/{THREAD}",
        since=datetime.fromtimestamp(float(replies[THREAD][4400]["ts"]) + 0.5, tz=timezone.utc),
        limit=500,
        registry=_api_registry(api),
    )
    assert [m.text for m in short] == [f"r{i}" for i in range(4400, 4500)]


def test_a_thread_within_the_page_cap_is_still_read_whole(token):
    api = FakeSlackApi(replies=_long_thread(450))
    messages = correspond.read(f"slack:{CHANNEL}/{THREAD}", limit=500, registry=_api_registry(api))
    assert [m.text for m in messages] == ["the parent"] + [f"r{i}" for i in range(450)]
    assert [name for name, _ in api.calls].count("conversations.replies") == 3


def test_listen_on_a_long_thread_still_moves_forward_from_its_cursor(token):
    replies = _long_thread(4500)
    api = FakeSlackApi(replies=replies)
    clock = float(replies[THREAD][-1]["ts"]) + 3600
    registry = {"slack": Slack(http=api, keychain=lambda s: "", clock=lambda: clock)}
    ref = f"slack:{CHANNEL}/{THREAD}"
    cursors = {ref: replies[THREAD][10]["ts"]}
    got = list(correspond.listen(ref, cursors=cursors, limit=3, registry=registry))
    assert [e.message.text for e in got] == ["r10", "r11", "r12"]


OTHER_TOKEN = "xoxb-other"  # a second, equally fictional workspace


def _two_workspaces(first, second):
    """One transport answering for two tokens, each with its own fake workspace."""

    def http(method, url, *, headers=None, body=None, timeout=None):
        api = second if headers["Authorization"] == f"Bearer {OTHER_TOKEN}" else first
        return api(method, url, headers=headers, body=body, timeout=timeout)

    return http


def test_channel_names_and_authors_are_kept_per_token(monkeypatch):
    user = lambda handle: {"id": "U0ADA", "name": handle, "profile": {}}
    first = FakeSlackApi(
        HISTORY, channels=[{"id": CHANNEL, "name": "releases"}], users={"U0ADA": user("ada")}
    )
    second = FakeSlackApi(
        HISTORY,
        channels=[{"id": "C0SECOND1", "name": "releases"}],
        users={"U0ADA": user("ana")},
        token=OTHER_TOKEN,
    )
    registry = {"slack": Slack(http=_two_workspaces(first, second), keychain=lambda s: "", clock=lambda: NOW)}

    def handles(token):
        monkeypatch.setenv("CORRESPOND_SLACK_TOKEN", token)
        messages = correspond.read("slack:releases", registry=registry)
        return {m.author.handle for m in messages if m.author.native_id == "U0ADA"}

    assert handles(TOKEN) == {"ada"}
    assert handles(OTHER_TOKEN) == {"ana"}
    assert handles(TOKEN) == {"ada"}  # still cached for the first token: no new calls
    assert {p["channel"] for n, p in second.calls if n == "conversations.history"} == {"C0SECOND1"}
    assert [n for n, _ in first.calls].count("conversations.list") == 1
    assert [n for n, _ in first.calls].count("users.info") == 1


def test_a_token_without_users_read_does_not_silence_another(monkeypatch):
    first = FakeSlackApi(HISTORY, errors={"users.info": "missing_scope"})
    second = FakeSlackApi(
        HISTORY, users={"U0ADA": {"id": "U0ADA", "name": "ada", "profile": {}}}, token=OTHER_TOKEN
    )
    registry = {"slack": Slack(http=_two_workspaces(first, second), keychain=lambda s: "", clock=lambda: NOW)}
    monkeypatch.setenv("CORRESPOND_SLACK_TOKEN", TOKEN)
    correspond.read(f"slack:{CHANNEL}", registry=registry)
    monkeypatch.setenv("CORRESPOND_SLACK_TOKEN", OTHER_TOKEN)
    messages = correspond.read(f"slack:{CHANNEL}", registry=registry)
    assert {m.author.handle for m in messages if m.author.native_id == "U0ADA"} == {"ada"}


def test_the_caches_never_hold_the_token_itself(token):
    slack = Slack(http=FakeSlackApi(HISTORY), keychain=lambda s: "", clock=lambda: NOW)
    correspond.read(f"slack:{CHANNEL}", registry={"slack": slack})
    assert TOKEN not in repr(vars(slack))


def test_a_listing_stopped_early_resumes_where_it_stopped(token):
    channels = [{"id": f"C0000000{i}", "name": f"c{i}"} for i in range(6)]
    api = FakeSlackApi(HISTORY, channels=channels, channel_page=2)
    registry = _api_registry(api)
    for name in ("c0", "c5", "c0", "c3", "c5"):
        correspond.read(f"slack:{name}", registry=registry)
    listed = [p.get("cursor") for n, p in api.calls if n == "conversations.list"]
    assert listed == [None, "2", "4"], listed
    with pytest.raises(correspond.ChannelError) as caught:
        correspond.read("slack:nowhere", registry=registry)  # a whole, fresh list without it: listed again
    assert caught.value.kind == "not_found"
    assert [n for n, _ in api.calls].count("conversations.list") == 6


def _thread_of(*segments, latest_reply=True):
    """A thread whose replies come in ``(count, step)`` segments, each ``step`` s after the last."""
    stamps, at = [], float(THREAD)
    for count, step in segments:
        for _ in range(count):
            at += step
            stamps.append(f"{at:.6f}")
    parent = _msg(THREAD, "the parent", thread_ts=THREAD, reply_count=len(stamps))
    if latest_reply:
        parent["latest_reply"] = stamps[-1]
    return {THREAD: [parent] + [_msg(ts, f"r{i}", thread_ts=THREAD) for i, ts in enumerate(stamps)]}


@pytest.mark.parametrize(
    "segments",
    [
        [(4200, 0.01), (100, 30 * 86400)],  # a fast start, then a monthly trickle
        [(4200, 3600), (3000, 0.0005)],  # an hourly thread, then a burst
        [(4200, 1), (100, 7 * 86400)],  # a weekly trickle after a busy hour
        [(4200, 1), (500, 0.000001)],  # 500 replies a microsecond apart
        [(500, 0.5), (5000, 600), (200, 0.01)],  # three paces
    ],
)
def test_a_long_threads_newest_replies_are_found_whatever_its_pace(token, segments):
    from correspond.channels.slack import MAX_THREAD_PAGES

    replies = _thread_of(*segments)
    total = len(replies[THREAD]) - 1
    api = FakeSlackApi(replies=replies)
    clock = float(replies[THREAD][-1]["ts"]) + 5
    registry = {"slack": Slack(http=api, keychain=lambda s: "", clock=lambda: clock)}
    messages = correspond.read(f"slack:{CHANNEL}/{THREAD}", registry=registry)
    assert [m.text for m in messages] == [f"r{i}" for i in range(total - 50, total)]
    asked = [name for name, _ in api.calls].count("conversations.replies")
    assert asked <= 1 + 2 * MAX_THREAD_PAGES, asked


def test_a_thread_whose_parent_counts_too_many_replies_skips_its_oldest_pages(token):
    api = FakeSlackApi(replies=_thread_of((4500, 1)))
    correspond.read(f"slack:{CHANNEL}/{THREAD}", registry=_api_registry(api))
    paged = [p for n, p in api.calls if n == "conversations.replies" and p.get("cursor")]
    assert paged == [], "the first page's reply_count sends the read to the newest end"
    assert [n for n, _ in api.calls].count("conversations.replies") <= 6


def test_a_thread_ts_written_short_still_finds_its_parent(token):
    replies = _thread_of((4500, 1))
    short = THREAD.rstrip("0")  # 1760000010.0001
    replies = {short: replies[THREAD]}
    api = FakeSlackApi(replies=replies)
    clock = float(replies[short][-1]["ts"]) - 3 * 86400  # a clock far behind: latest_reply is used
    registry = {"slack": Slack(http=api, keychain=lambda s: "", clock=lambda: clock)}
    messages = correspond.read(f"slack:{CHANNEL}/{short}", registry=registry)
    assert [m.text for m in messages] == [f"r{i}" for i in range(4450, 4500)]


def test_a_long_threads_walk_waits_a_short_rate_limit_out(token):
    replies = _thread_of((4500, 1))
    api = FakeSlackApi(replies=replies)
    limited = [2]

    def http(method, url, *, headers=None, body=None, timeout=None):
        if "conversations.replies" in url and b"latest=" in (body or b"") and limited[0]:
            limited[0] -= 1
            return Response(429, {"retry-after": "1"}, b"")
        return api(method, url, headers=headers, body=body, timeout=timeout)

    slept = []
    registry = {
        "slack": Slack(http=http, keychain=lambda s: "", clock=lambda: NOW, sleep=slept.append)
    }
    messages = correspond.read(f"slack:{CHANNEL}/{THREAD}", registry=registry)
    assert [m.text for m in messages] == [f"r{i}" for i in range(4450, 4500)]
    assert slept == [1, 1]


def test_a_channel_created_on_a_page_already_listed_is_found(token):
    channels = [{"id": f"C0000000{i}", "name": f"c{i}"} for i in range(6)]
    api = FakeSlackApi(HISTORY, channels=channels, channel_page=2)
    registry = _api_registry(api)
    correspond.read("slack:c0", registry=registry)  # stops after the first page
    api.channels.insert(1, {"id": "C0000NEW1", "name": "fresh"})  # lands on page one
    correspond.read("slack:fresh", registry=registry)
    history = [p["channel"] for n, p in api.calls if n == "conversations.history"]
    assert history[-1] == "C0000NEW1"


def test_a_stale_listing_cursor_starts_the_listing_over(token):
    channels = [{"id": f"C0000000{i}", "name": f"c{i}"} for i in range(6)]
    api = FakeSlackApi(HISTORY, channels=channels, channel_page=2)
    registry = _api_registry(api)
    correspond.read("slack:c0", registry=registry)
    api.errors["conversations.list"] = "invalid_cursor"
    original = api.__call__

    def http(method, url, *, headers=None, body=None, timeout=None):
        from urllib.parse import parse_qsl

        params = dict(parse_qsl(body.decode())) if body else {}
        if "conversations.list" in url and not params.get("cursor"):
            api.errors.pop("conversations.list", None)  # from the start, it answers
        return original(method, url, headers=headers, body=body, timeout=timeout)

    registry["slack"].http = http
    correspond.read("slack:c5", registry=registry)
    assert [p["channel"] for n, p in api.calls if n == "conversations.history"][-1] == "C00000005"


def test_an_adapter_keeps_caches_for_a_few_tokens_only():
    from correspond.channels.slack import MAX_WORKSPACES

    slack = Slack()
    for i in range(MAX_WORKSPACES + 3):
        slack._workspace(f"xoxb-rotated-{i}").channel_ids["x"] = str(i)
    assert len(slack._workspaces) == MAX_WORKSPACES
    assert slack._workspace(f"xoxb-rotated-{MAX_WORKSPACES + 2}").channel_ids == {"x": str(MAX_WORKSPACES + 2)}
    assert slack._workspace("xoxb-rotated-0").channel_ids == {}  # dropped, started anew


def test_a_read_counts_only_written_messages_toward_its_limit(token):
    notices = [_msg(f"17600001{i:02d}.000100", "joined", subtype="channel_join") for i in range(5)]
    written = [_msg(f"17600000{i:02d}.000100", f"m{i}") for i in range(5)]
    api = FakeSlackApi(notices + written)
    messages = correspond.read(f"slack:{CHANNEL}", limit=3, registry=_api_registry(api))
    assert [m.text for m in messages] == ["m2", "m3", "m4"]


def test_a_resumed_listing_takes_a_later_pages_channel_for_a_reused_name(token):
    channels = [{"id": f"C0000000{i}", "name": f"c{i}"} for i in range(6)]
    api = FakeSlackApi(HISTORY, channels=channels, channel_page=2)
    registry = _api_registry(api)
    correspond.read("slack:c0", registry=registry)  # stops after the first page
    api.channels[4] = {"id": "C0000NEW0", "name": "c0"}  # the name is now another channel's
    correspond.read("slack:c5", registry=registry)  # resumes, reading the later pages
    correspond.read("slack:c0", registry=registry)
    history = [p["channel"] for n, p in api.calls if n == "conversations.history"]
    assert history[-1] == "C0000NEW0"


def test_one_adapter_shared_across_threads_lists_channels_once(token):
    import time
    from concurrent.futures import ThreadPoolExecutor

    channels = [{"id": f"C0000000{i}", "name": f"c{i}"} for i in range(6)]
    api = FakeSlackApi(HISTORY, channels=channels, channel_page=2)

    def http(method, url, *, headers=None, body=None, timeout=None):
        if "conversations.list" in url:
            time.sleep(0.02)  # widen the window two threads could list in at once
        return api(method, url, headers=headers, body=body, timeout=timeout)

    registry = {"slack": Slack(http=http, keychain=lambda s: "", clock=lambda: NOW)}
    with ThreadPoolExecutor(4) as pool:
        list(pool.map(lambda n: correspond.read(f"slack:c{n % 6}", registry=registry), range(24)))
    listed = [p.get("cursor") for n, p in api.calls if n == "conversations.list"]
    assert listed == [None, "2", "4"], listed


GOV_API = "https://slack-gov.com/api"


def _gov_history(url):
    """HISTORY as a GovSlack workspace answers it: its file on files.slack-gov.com."""
    gov = [dict(m) for m in HISTORY]
    gov[-1]["files"] = [{**gov[-1]["files"][0], "url_private": url}]
    return gov


def test_govslack_sends_the_web_api_and_file_fetches_to_slack_gov_only(token, monkeypatch):
    monkeypatch.setenv("CORRESPOND_SLACK_API_BASE", GOV_API + "/")
    asked = []
    api = FakeSlackApi(_gov_history("https://files.slack-gov.com/F1"))

    def http(method, url, *, headers=None, body=None, timeout=None):
        asked.append(url)
        if url.startswith("https://files.slack-gov.com/"):
            return Response(200, {"content-type": "application/pdf"}, b"%PDF-1.7")
        return api(method, url, headers=headers, body=body)

    bot = correspond.read(f"slack:{CHANNEL}", limit=1, registry=_api_registry(http))[-1]
    assert bot.attachments[0].content() == b"%PDF-1.7"
    assert asked and all(u.startswith(("https://slack-gov.com/api/", "https://files.slack-gov.com/")) for u in asked)


def test_govslack_never_sends_its_token_to_a_commercial_slack_host(token, monkeypatch):
    monkeypatch.setenv("CORRESPOND_SLACK_API_BASE", GOV_API)
    sent = []
    api = FakeSlackApi(_gov_history("https://files.slack.com/F1"))

    def http(method, url, *, headers=None, body=None, timeout=None):
        if url.startswith("https://files."):
            sent.append(url)
        return api(method, url, headers=headers, body=body)

    bot = correspond.read(f"slack:{CHANNEL}", limit=1, registry=_api_registry(http))[-1]
    with pytest.raises(correspond.ChannelError, match="outside Slack"):
        bot.attachments[0].content()
    assert sent == []


def test_commercial_slack_never_sends_its_token_to_a_govslack_host(token):
    sent = []
    api = FakeSlackApi(_gov_history("https://files.slack-gov.com/F1"))

    def http(method, url, *, headers=None, body=None, timeout=None):
        if url.startswith("https://files."):
            sent.append(url)
        return api(method, url, headers=headers, body=body)

    bot = correspond.read(f"slack:{CHANNEL}", limit=1, registry=_api_registry(http))[-1]
    with pytest.raises(correspond.ChannelError, match="outside Slack"):
        bot.attachments[0].content()
    assert sent == []


def test_an_api_base_other_than_slacks_is_refused_before_the_token_goes_anywhere(token, monkeypatch):
    monkeypatch.setenv("CORRESPOND_SLACK_API_BASE", "https://example.org/api")
    asked = []

    def http(method, url, *, headers=None, body=None, timeout=None):
        asked.append(url)
        return FakeSlackApi(HISTORY)(method, url, headers=headers, body=body)

    with pytest.raises(correspond.ChannelError) as caught:
        correspond.read(f"slack:{CHANNEL}", registry=_api_registry(http))
    assert caught.value.kind == "validation" and asked == []
