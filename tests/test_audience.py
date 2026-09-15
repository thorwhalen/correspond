"""The audience operation: unknown resolves to public from every direction, and every surface shows the same record, in words first."""

import hashlib
import json
import os
import subprocess
import sys

import pytest

import correspond
from correspond import ops, registry, tools
from correspond.channels._http import Response
from correspond.channels.telegram import Telegram
from correspond.errors import ChannelError
from correspond.mcp import refs
from correspond.model import (
    AUDIENCE_UNHASHED,
    DURABILITY,
    WIDENING,
    Audience,
    Capabilities,
    ConversationRef,
    Scope,
    Support,
)
from correspond.registry import CHANNELS, build_registry
from correspond.testing import demo_channel

UNKNOWN_WORDS = (
    "world-readable (assumed: the audience could not be determined); not retractable"
)


class _Room:
    """A channel read only by the operator; it records what it was asked, and can be told to misbehave."""

    name = "room"

    def __init__(self, answer=None):
        self.answer = answer
        self.asked = []

    @property
    def capabilities(self):
        return Capabilities(channel=self.name, audience=Support.FULL)

    def parse_ref(self, id):
        return ConversationRef(channel=self.name, id=id.lower(), kind="room")

    def audience(self, ref, *, draft=None):
        self.asked.append((ref, draft))
        if self.answer is not None:
            return self.answer(ref)
        return Audience(
            ref=ref.encoded,
            scope="operator",
            complete=True,
            external=False,
            retractable=True,
            evidence=["asked the room"],
        )


def _raise(error):
    def answer(ref):
        raise error

    return answer


def _cli(args):
    return subprocess.run(
        [sys.executable, "-m", "correspond.testing", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )


def _assert_defaulted(found, ref, reason):
    assert isinstance(found, Audience)
    assert (found.ref, found.scope, found.complete, found.defaulted) == (
        ref,
        Scope.PUBLIC,
        False,
        True,
    )
    assert set(found.durability) == set(DURABILITY) and set(found.widening) == set(
        WIDENING
    )
    assert found.retractable is False and found.external is None
    assert any(reason in line for line in found.evidence), found.evidence
    assert found.evidence[-1] == "unknown resolves to public"


def test_an_audience_reader_answers_for_its_canonical_reference_and_sees_the_draft():
    room = _Room()
    draft = correspond.Draft(text="hello")
    found = ops.audience("room:Kitchen", draft, registry={"room": room})
    assert (found.ref, found.scope, found.retractable, found.defaulted) == (
        "room:kitchen",
        "operator",
        True,
        False,
    )
    assert room.asked == [(ConversationRef(channel="room", id="kitchen"), draft)]
    assert ops.implemented(room) == room.capabilities.operations == ("audience",)


@pytest.mark.parametrize(
    "ref, channels, expected_ref, reason",
    [
        (
            "fake:example/demo",
            {"fake": demo_channel()},
            "fake:example/demo",
            "fake has no audience reader",
        ),
        (
            "nowhere:example",
            {"fake": demo_channel()},
            "nowhere:example",
            "unknown channel 'nowhere'",
        ),
        ("nonsense", {}, "nonsense", "is not a conversation reference"),
        (
            "room:Kitchen",
            {"room": _Room(_raise(RuntimeError("the platform fell over")))},
            "room:kitchen",
            "(RuntimeError): the platform fell over",
        ),
        (
            "room:x",
            {"room": _Room(_raise(ChannelError("offline", kind="network")))},
            "room:x",
            "(ChannelError): offline",
        ),
        (
            "room:x",
            {"room": _Room(lambda ref: {"scope": "operator"})},
            "room:x",
            "returned dict, not an Audience",
        ),
        (
            "room:Kitchen",
            {"room": _Room(lambda ref: Audience(ref="room:elsewhere", scope="operator"))},
            "room:kitchen",
            "answered for room:elsewhere, not room:kitchen",
        ),
    ],
)
def test_unknown_resolves_to_public_whatever_went_wrong(
    ref, channels, expected_ref, reason
):
    _assert_defaulted(ops.audience(ref, registry=channels), expected_ref, reason)


def test_a_planned_channel_says_it_is_planned_and_where():
    discord = next(c for c in CHANNELS if c.name == "discord")
    found = ops.audience("discord:example-channel")
    _assert_defaulted(found, "discord:example-channel", "discord is planned, not built")
    assert discord.planned in found.evidence[0]
    _assert_defaulted(
        ops.audience(ConversationRef.parse("slack:example"), registry={}),
        "slack:example",
        "slack is planned",
    )


#: How honestly each built channel can say who reads it.
AUDIENCE_GRADES = {
    "github": "full",
    "email": "full",
    "telegram": "partial",
    "ntfy": "partial",
    "macos": "full",
    "webinbox": "full",
}


def test_every_built_channel_grades_its_audience_reader():
    built = build_registry()
    grading = {
        c.name: built[c.name].capabilities.audience.value
        for c in CHANNELS
        if not c.planned and c.name in built
    }
    assert {"github", "email", "ntfy", "telegram"} <= set(grading)
    assert grading == {k: v for k, v in AUDIENCE_GRADES.items() if k in grading}
    for name, grade in grading.items():
        shown = tools.capabilities(name)
        assert "audience" in shown["operations"]
        assert f"audience: {grade}" in shown["text"].splitlines()


def test_the_tool_result_holds_the_record_its_hash_and_words(monkeypatch):
    monkeypatch.setattr(registry, "channels", lambda: {"room": _Room()})
    result = tools.audience("room:Kitchen")
    json.dumps(result)
    record = result["audience"]
    assert result["ok"] and (record["ref"], record["scope"]) == (
        "room:kitchen",
        "operator",
    )
    assert result["words"] == "only the operator; retractable"
    assert result["summary"] == "room:kitchen: only the operator; retractable"
    # A consumer hashes the record as it came, or through from_dict: the same value.
    canonical = json.dumps(
        {k: v for k, v in record.items() if k not in AUDIENCE_UNHASHED},
        sort_keys=True,
        separators=(",", ":"),
    )
    assert (
        hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        == result["hash"]
        == Audience.from_dict(json.loads(json.dumps(record))).hash
    )
    lines = result["text"].splitlines()
    assert lines[0] == result["words"] and lines[-1] == f"hash: {result['hash']}"
    assert {"scope: operator", "complete: true", "external: false"} <= set(lines)
    assert "evidence: asked the room" in lines and "defaulted: true" not in lines

    unknown = tools.audience("nowhere:example")
    assert unknown["ok"] and unknown["audience"]["defaulted"]
    assert unknown["audience"]["scope"] == "public" and unknown["words"] == UNKNOWN_WORDS
    assert "defaulted: true" in unknown["text"].splitlines()


def test_the_cli_answers_in_words_first_and_with_the_record_on_request():
    shown = _cli(["audience", "fake:example/demo"])
    assert shown.returncode == 0, shown.stderr
    lines = shown.stdout.splitlines()
    assert lines[0] == UNKNOWN_WORDS
    assert "scope: public" in lines and "evidence: fake has no audience reader" in lines
    record = json.loads(_cli(["audience", "fake:example/demo", "--json"]).stdout)[
        "audience"
    ]
    assert (record["scope"], record["defaulted"], record["complete"]) == (
        "public",
        True,
        False,
    )


def test_mcp_exposes_audience_as_a_read():
    assert tools.SIDE_EFFECTS["audience"] == "external-read"
    assert "correspond.tools:audience" in refs()


# ------------------------------------------------------------ the other channels (C2)


def _cli_here(capsys, *args):
    """The CLI in this process, where the conftest keeps the Keychain and real settings out: ``(exit code, stdout)``."""
    from correspond.testing import main

    with pytest.raises(SystemExit) as exited:
        main(list(args))
    return exited.value.code, capsys.readouterr().out


def test_the_acceptance_lines_on_the_command_line(capsys):
    code, out = _cli_here(
        capsys, "audience", "email:ada@example.org", "--cc", "list@example.org"
    )
    lines = out.splitlines()
    assert code == 0 and {
        "scope: named",
        "complete: false",
        "external: true",
        "widening: forwarding, list_expansion",
        "readers: ada@example.org, list@example.org",
    } <= set(lines)
    assert "class: everyone behind list@example.org, which may be a mailing list" in lines

    code, out = _cli_here(
        capsys,
        "send",
        "email:ada@example.org",
        "hi",
        "--cc",
        "bob@example.org",
        "--dry-run",
    )
    assert code == 0
    assert {"  to: ada@example.org", "  cc: bob@example.org"} <= set(out.splitlines())
    assert any(
        line.startswith("  audience: named readers; at least 2 known readers;")
        for line in out.splitlines()
    )

    code, out = _cli_here(capsys, "audience", "telegram:@somechannel")
    assert code == 0 and "scope: public" in out.splitlines()

    code, out = _cli_here(capsys, "audience", "ntfy:some-topic")
    assert {"scope: public", "class: anyone who knows the topic name"} <= set(
        out.splitlines()
    )

    for ref in ("macos:", "webinbox:example-site"):
        code, out = _cli_here(capsys, "audience", ref, "--json")
        record = json.loads(out)["audience"]
        assert (code, record["scope"], record["retractable"]) == (0, "operator", True)

    code, out = _cli_here(capsys, "audience", "discord:123", "--json")
    record = json.loads(out)["audience"]
    assert (record["scope"], record["defaulted"]) == ("public", True)
    assert any("channel not built" in line for line in record["evidence"])


def test_an_email_reaches_its_address_and_every_copy():
    found = tools.audience("email:ada@example.org", cc="list@example.org")["audience"]
    assert found["scope"] == "named" and found["complete"] is False
    assert [r["handle"] for r in found["readers"]] == [
        "ada@example.org",
        "list@example.org",
    ]
    assert "list_expansion" in found["widening"] and found["external"] is True
    blind = ops.audience(
        "email:ada@example.org", correspond.Draft(text="", bcc=("cy@example.net",))
    )
    assert [r.handle for r in blind.readers] == ["ada@example.org", "cy@example.net"]
    assert not blind.complete and blind.widening == ("forwarding",)
    assert any("cy@example.net" in c and "blind" in c for c in blind.classes)
    copied = ops.audience(
        "email:ada@example.org", correspond.Draft(text="", cc=("cy@example.net",))
    )
    assert copied.hash != blind.hash, "moving a copy from bcc to cc shows it to everyone"
    malformed = ops.audience(
        "email:ada@example.org", correspond.Draft(text="", cc=("not an address",))
    )
    assert malformed.defaulted
    assert ops.audience("email:").defaulted


def test_no_email_audience_claims_to_be_complete_and_a_list_hides_who_is_external(
    config_file,
):
    config_file(
        '[email]\nown_domains = ["example.org"]\nlists = ["@lists.example.org"]\n'
    )
    inside = ops.audience(
        "email:ada@example.org", correspond.Draft(text="", cc=("bob@mail.example.org",))
    )
    assert (inside.external, inside.complete) == (False, False)
    assert any("forward" in c for c in inside.classes)
    for group in ("x@lists.example.org", "team@example.org"):
        listed = ops.audience(
            "email:ada@example.org", correspond.Draft(text="", cc=(group,))
        )
        assert listed.external is None and "list_expansion" in listed.widening
    assert any(
        "under lists in the config" in e
        for e in ops.audience("email:x@lists.example.org").evidence
    )
    outside = ops.audience(
        "email:ada@example.net", correspond.Draft(text="", cc=("team@example.org",))
    )
    assert outside.external is True


def test_the_mail_writer_puts_cc_in_a_header_and_bcc_only_on_the_envelope(monkeypatch):
    from correspond.channels.mail import Email

    monkeypatch.setenv("CORRESPOND_EMAIL_USER", "me@example.org")
    sent = []

    class Smtp:
        def send_message(self, message, to_addrs=None):
            sent.append((message, to_addrs))

        def quit(self):
            pass

    channels = {"email": Email(smtp=Smtp)}
    result = correspond.send(
        "email:ada@example.org",
        "hi",
        cc=["bob@example.org"],
        bcc=["cy@example.org"],
        registry=channels,
    )
    assert result.ok, result
    [(message, to_addrs)] = sent
    assert message["Cc"] == "bob@example.org" and message["Bcc"] is None
    assert "cy@example.org" not in message.as_string()
    assert to_addrs == ["ada@example.org", "bob@example.org", "cy@example.org"]
    assert result.plan["bcc"] == "cy@example.org"
    assert "at least 3 known readers" in result.plan["audience"]
    bad = correspond.send(
        "email:ada@example.org", "hi", cc=["not an address"], registry=channels
    )
    assert bad.error_kind == "validation" and len(sent) == 1
    with pytest.raises(ValueError, match="cc"):
        correspond.send(
            "email:ada@example.org",
            correspond.Draft(text="hi"),
            cc=["bob@example.org"],
            registry=channels,
        )


def test_copies_on_a_channel_without_them_are_refused(fake):
    result = tools.send("fake:example/demo", "hi", cc="bob@example.org", dry_run=True)
    assert (result["ok"], result["error_kind"], result["operation"]) == (
        False,
        "not_supported",
        "cc",
    )
    assert fake.sent == []
    asked = tools.audience("ntfy:some-topic", bcc="bob@example.org")
    assert (asked["ok"], asked["error_kind"], asked["operation"]) == (
        False,
        "not_supported",
        "cc",
    )


class _BotApi:
    """``getChat`` answering the chats it holds (``linked`` for a second one), or failing."""

    def __init__(self, chat=None, *, linked=None, error=None):
        self.chats = {c["id"]: c for c in (chat, linked) if c}
        self.error, self.calls = error, []

    def __call__(self, method, url, *, headers=None, body=None, timeout=None):
        params = json.loads(body)
        self.calls.append((url.rsplit("/", 1)[1], params))
        if self.error:
            raise self.error
        if params["chat_id"] not in self.chats:
            payload = {
                "ok": False,
                "error_code": 400,
                "description": "Bad Request: chat not found",
            }
            return Response(400, {}, json.dumps(payload).encode())
        result = self.chats[params["chat_id"]]
        return Response(200, {}, json.dumps({"ok": True, "result": result}).encode())


@pytest.fixture
def bot_token(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:example-token-for-tests")


def _telegram(ref, api):
    return ops.audience(ref, registry={"telegram": Telegram(http=api, log={})})


def test_a_telegram_audience_follows_the_chat(bot_token):
    group = _BotApi({"id": -4001, "type": "supergroup", "title": "Example group"})
    found = _telegram("telegram:-4001", group)
    assert (found.scope, found.complete, found.defaulted) == (Scope.GROUP, False, False)
    assert set(found.widening) == {"forwarding", "joiners_read_history"}
    assert group.calls == [("getChat", {"chat_id": -4001})]
    topic = _telegram("telegram:-4001/7", group)
    assert (topic.ref, topic.scope) == ("telegram:-4001/7", Scope.GROUP)

    closed = _BotApi(
        {
            "id": -4001,
            "type": "supergroup",
            "has_visible_history": False,
            "has_protected_content": True,
        }
    )
    assert _telegram("telegram:-4001", closed).widening == ()

    channel = _BotApi({"id": -1004002, "type": "channel", "username": "examplechannel"})
    public = _telegram("telegram:-1004002", channel)
    assert (public.scope, public.external) == (Scope.PUBLIC, True)

    private = _BotApi(
        {"id": 42, "type": "private", "username": "ada", "first_name": "Ada"}
    )
    named = _telegram("telegram:42", private)
    assert (named.scope, named.complete) == (Scope.NAMED, True)
    assert [(r.native_id, r.handle) for r in named.readers] == [("42", None)]
    assert "joiners_read_history" not in named.widening
    renamed = _BotApi(
        {"id": 42, "type": "private", "username": "ada2", "first_name": "A"}
    )
    assert _telegram("telegram:42", renamed).hash == named.hash


def test_a_linked_discussion_group_widens_a_channel(bot_token):
    linked_group = {"id": -4009, "type": "supergroup", "linked_chat_id": -1004003}
    private_channel = {"id": -1004003, "type": "channel", "linked_chat_id": -4009}
    found = _telegram("telegram:-1004003", _BotApi(private_channel, linked=linked_group))
    assert found.scope is Scope.GROUP
    assert any("linked" in c for c in found.classes)

    public_channel = {**private_channel, "username": "examplechannel"}
    group = _telegram("telegram:-4009", _BotApi(linked_group, linked=public_channel))
    assert group.scope is Scope.PUBLIC

    unreadable = _telegram("telegram:-1004003", _BotApi(private_channel))
    assert unreadable.defaulted


def test_a_telegram_chat_nobody_could_ask_about_is_public(bot_token):
    failing = _BotApi(error=ChannelError("offline", kind="network"))
    by_id = _telegram("telegram:-4001", failing)
    assert by_id.defaulted and any("offline" in e for e in by_id.evidence)
    by_name = _telegram("telegram:@examplechannel", failing)
    assert (by_name.scope, by_name.defaulted) == (Scope.PUBLIC, True)
    assert _telegram("telegram:", failing).defaulted


def test_ntfy_is_public_unless_the_server_denies_anonymous_reads(
    config_file, monkeypatch
):
    open_topic = ops.audience("ntfy:some-topic")
    assert open_topic.scope is Scope.PUBLIC and open_topic.external is True
    assert "anyone who knows the topic name" in open_topic.classes
    assert any("ntfy.sh" in c for c in open_topic.classes)
    assert any("12h" in e for e in open_topic.evidence)
    config_file('[ntfy]\ndenies_anonymous_read = true\ncache_duration = "1h"\n')
    closed = ops.audience("ntfy:")
    assert (closed.scope, closed.external) == (Scope.GROUP, None)
    config_file('[ntfy]\ndenies_anonymous_read = true\ncache_duration = "60m"\n')
    assert ops.audience("ntfy:").hash == closed.hash, (
        "the cache value is evidence, not a reader"
    )
    monkeypatch.setenv("NTFY_URL", "https://ntfy.example.org")
    assert ops.audience("ntfy:").hash != closed.hash, "another server has other readers"
