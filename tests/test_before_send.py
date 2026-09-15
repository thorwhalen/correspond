"""The ``before_send`` seam: every send and edit runs the check, with the audience, before it leaves; a check that cannot run stops the write."""

import asyncio
import inspect
import json
import os

import pytest

import correspond
from correspond import registry, tools
from correspond.errors import CHECK_KINDS, NeedsApproval, Refused
from correspond.mcp import _resolve, _without, refs
from correspond.model import Audience, ConversationRef, Draft, SendResult
from correspond.outbound import DEFAULT, notice
from correspond.testing import FakeChannel, demo_channel

CHECKS_MODULE = "c3_invented_checks"
CHECKS = """
from correspond.errors import NeedsApproval, Refused

def refuse(ref, draft, audience, **context):
    raise Refused("a token")

def hold(ref, draft, audience, **context):
    raise NeedsApproval("public audience")

not_callable = 42
"""


class EditableFake(FakeChannel):
    """The fake channel, with an edit, a reaction and an upload that record what they did."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.edited, self.reacted, self.uploaded = [], [], []

    def _done(self, ref, operation, dry_run, record, **plan):
        if not dry_run:
            record.append((ref, plan))
        return SendResult(
            ok=True,
            channel=self.name,
            conversation=ref.encoded,
            operation=operation,
            dry_run=dry_run,
            plan=plan,
        )

    def edit(self, ref, message_id, draft, *, dry_run=False):
        return self._done(
            ref, "edit", dry_run, self.edited, message_id=message_id, text=draft.text
        )

    def react(self, ref, message_id, reaction, *, dry_run=False):
        return self._done(
            ref, "react", dry_run, self.reacted, message_id=message_id, reaction=reaction
        )

    def upload(self, ref, name, data, *, media_type="", dry_run=False):
        return self._done(ref, "upload", dry_run, self.uploaded, name=name)


@pytest.fixture
def editable():
    """An :class:`EditableFake` registered as ``fake``, seeded like the demo channel."""
    channel = EditableFake(conversations=demo_channel().conversations)
    registry.register_channel(channel, replace=True)
    yield channel
    if "fake" in registry.channels():
        registry.unregister_channel("fake")


@pytest.fixture
def configure(tmp_path, monkeypatch, config_file):
    """``configure('"c3_invented_checks:refuse"')`` writes that ``before_send`` line (a TOML value) into the config."""
    (tmp_path / f"{CHECKS_MODULE}.py").write_text(CHECKS, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(filter(None, [str(tmp_path), os.environ.get("PYTHONPATH")])),
    )

    def write(value: str):
        return config_file(f"before_send = {value}\n")

    return write


def _cli(capsys, *args):
    """The ``correspond`` CLI in this process, with the demo fake registered: ``(exit code, stdout, stderr, the fake channel)``."""
    from correspond.testing import main

    with pytest.raises(SystemExit) as exited:
        main(list(args))
    out, err = capsys.readouterr()
    return exited.value.code, out, err, registry.channels()["fake"]


# ------------------------------------------------------------------ the contract


def test_the_check_receives_the_reference_the_draft_and_the_audience_before_the_write(
    fake,
):
    seen = []

    def check(ref, draft, audience, **context):
        seen.append((ref, draft, audience, len(fake.sent)))

    for dry_run in (True, False):
        result = correspond.send(
            "fake:example/demo", "x", dry_run=dry_run, before_send=check
        )
        assert result.ok and result.dry_run is dry_run, result
        assert result.plan["before_send"].startswith("passed (")
    (ref, draft, audience, sent_before), (_, _, _, sent_before_real) = seen
    assert isinstance(ref, ConversationRef) and ref.encoded == "fake:example/demo"
    assert isinstance(draft, Draft) and draft.text == "x"
    assert isinstance(audience, Audience) and audience.ref == "fake:example/demo"
    assert (sent_before, sent_before_real) == (0, 0), "the check runs before the write"
    assert len(fake.sent) == 1


@pytest.mark.parametrize(
    "check, kind, reason",
    [
        (Refused, "refused", "a token"),
        (NeedsApproval, "needs_approval", "public audience"),
    ],
)
def test_refused_and_needs_approval_send_nothing_on_either_path(
    fake, check, kind, reason
):
    def before_send(ref, draft, audience, **context):
        raise check(reason)

    for dry_run in (True, False):
        result = correspond.send(
            "fake:example/demo", "x", dry_run=dry_run, before_send=before_send
        )
        assert (result.ok, result.error_kind, result.error) == (False, kind, reason)
        assert result.dry_run is dry_run
        assert result.plan["text"] == "x", "the plan of what was stopped is shown"
        assert result.plan["audience"] and result.plan["before_send"].startswith(
            f"{kind}: {reason}"
        )
    assert fake.sent == []


def test_a_check_that_crashes_or_answers_a_value_stops_the_write(fake):
    def crashes(ref, draft, audience, **context):
        raise ValueError("bug in the check")

    def answers(ref, draft, audience, **context):
        return False

    def exits(ref, draft, audience, **context):
        raise SystemExit(0)

    class Invented(Refused):
        error_kind = "invented"

    def invents_a_kind(ref, draft, audience, **context):
        raise Invented("strange", ticket="t-1")

    def takes_three_arguments(ref, draft, audience):
        return None

    for check, words in (
        (crashes, "ValueError: bug in the check"),
        (answers, "returned False"),
        (exits, "SystemExit"),
        (invents_a_kind, "'invented'"),
        (takes_three_arguments, "TypeError"),
    ):
        result = correspond.send("fake:example/demo", "x", before_send=check)
        assert (result.ok, result.error_kind) == (False, "before_send_failed")
        assert words in result.error
    assert fake.sent == []


def test_edit_react_and_upload_run_the_check_too(editable):
    def refuse(ref, draft, audience, **context):
        raise Refused("a token")

    writes = {
        "edit": lambda **kw: correspond.edit("fake:example/demo", "m1", "new", **kw),
        "react": lambda **kw: correspond.react("fake:example/demo", "m1", "eyes", **kw),
        "upload": lambda **kw: correspond.upload(
            "fake:example/demo", "a.txt", b"x", **kw
        ),
    }
    for operation, write in writes.items():
        result = write(before_send=refuse)
        assert (result.ok, result.error_kind, result.operation) == (
            False,
            "refused",
            operation,
        )
    assert editable.edited == editable.reacted == editable.uploaded == []
    for write in writes.values():
        assert write().ok
    assert len(editable.edited) == len(editable.reacted) == len(editable.uploaded) == 1


def test_the_check_is_told_the_operation_the_dry_run_and_the_message(editable):
    seen = []

    def check(ref, draft, audience, *, operation, dry_run, message_id, **context):
        seen.append((operation, dry_run, message_id, draft.text))

    correspond.send("fake:example/demo", "x", dry_run=True, before_send=check)
    correspond.edit("fake:example/demo", "m1", "y", before_send=check)
    correspond.react("fake:example/demo", "m2", "eyes", dry_run=True, before_send=check)
    correspond.upload("fake:example/demo", "a.txt", b"x", before_send=check)
    assert seen == [
        ("send", True, None, "x"),
        ("edit", False, "m1", "y"),
        ("react", True, "m2", "eyes"),
        ("upload", False, None, "a.txt"),
    ]


def test_details_a_check_attaches_travel_in_the_plan(fake):
    def hold(ref, draft, audience, **context):
        raise NeedsApproval("public audience", approval="a-1", draft_hash=b"x")

    result = correspond.send("fake:example/demo", "x", before_send=hold)
    assert result.error_kind == "needs_approval"
    assert result.plan["before_send_details"] == {"approval": "a-1", "draft_hash": "b'x'"}
    json.dumps(tools._write_result(result))


def test_a_before_send_key_inside_a_table_is_refused_not_ignored(fake, config_file):
    config_file(f'[ntfy]\nbefore_send = "{CHECKS_MODULE}:refuse"\n')
    result = correspond.send("fake:example/demo", "x")
    assert (result.ok, result.error_kind) == (False, "before_send_unavailable")
    assert "[ntfy]" in result.error and fake.sent == []


def test_with_no_configuration_the_default_shows_the_audience_and_lets_the_write_go(fake):
    dry = correspond.send("fake:example/demo", "x", dry_run=True)
    expected = correspond.audience("fake:example/demo")
    assert dry.ok and dry.plan["audience"] == expected.in_words()
    assert dry.plan["audience_hash"] == expected.hash
    assert DEFAULT in dry.plan["before_send"]
    assert notice(None, None, None) is None
    summary = tools.send("fake:example/demo", "x", dry_run=True)["summary"]
    assert expected.in_words() in summary
    assert correspond.send("fake:example/demo", "x").ok and len(fake.sent) == 1


def test_a_check_passed_in_python_wins_over_the_configured_one(fake, configure):
    configure(f'"{CHECKS_MODULE}:refuse"')
    assert correspond.send("fake:example/demo", "x").error_kind == "refused"
    assert correspond.send("fake:example/demo", "x", before_send=notice).ok


@pytest.mark.parametrize(
    "value",
    [
        '"no.such.module:fn"',
        f'"{CHECKS_MODULE}"',
        f'"{CHECKS_MODULE}:missing"',
        f'"{CHECKS_MODULE}:not_callable"',
        '""',
        "3",
    ],
)
def test_a_configured_check_that_cannot_load_stops_every_write(fake, configure, value):
    configure(value)
    for dry_run in (True, False):
        result = correspond.send("fake:example/demo", "x", dry_run=dry_run)
        assert (result.ok, result.error_kind) == (False, "before_send_unavailable"), (
            result
        )
        assert "nothing is sent" in result.error
        assert result.plan["audience"]
    assert fake.sent == []


def test_a_config_file_that_does_not_parse_stops_every_write(fake, config_file):
    config_file("before_send = \n")
    result = correspond.send("fake:example/demo", "x")
    assert (result.ok, result.error_kind) == (False, "before_send_unavailable")
    assert fake.sent == []


def test_every_check_kind_is_a_valid_send_result_kind():
    for kind in CHECK_KINDS:
        SendResult(ok=False, channel="fake", conversation="fake:x", error_kind=kind)
    assert tools._STOPPED.keys() == set(CHECK_KINDS)


# ------------------------------------------------------------------ the acceptance, on the command line


@pytest.mark.parametrize(
    "check, kind, reason",
    [("refuse", "refused", "a token"), ("hold", "needs_approval", "public audience")],
)
def test_cli_send_stopped_by_the_configured_check(
    capsys, fake, configure, check, kind, reason
):
    configure(f'"{CHECKS_MODULE}:{check}"')
    code, out, err, channel = _cli(capsys, "send", "fake:example/demo", "x")
    assert code == 1 and reason in err and channel.sent == []
    code, out, err, channel = _cli(capsys, "send", "fake:example/demo", "x", "--json")
    result = json.loads(out)
    assert code == 1 and (result["ok"], result["error_kind"], result["error"]) == (
        False,
        kind,
        reason,
    )
    assert channel.sent == []


def test_cli_dry_run_with_no_configuration_shows_the_audience_line(capsys, fake):
    code, out, err, channel = _cli(capsys, "send", "fake:example/demo", "x", "--dry-run")
    assert code == 0 and channel.sent == []
    assert "audience: world-readable" in out
    assert "before_send: no check configured" in out


def test_cli_unloadable_check_fails_closed_and_the_dry_run_says_so(
    capsys, fake, configure
):
    configure('"no.such.module:fn"')
    for extra in ([], ["--dry-run"]):
        code, out, err, channel = _cli(
            capsys, "send", "fake:example/demo", "x", *extra, "--json"
        )
        result = json.loads(out)
        assert code == 1 and result["error_kind"] == "before_send_unavailable"
        assert result["dry_run"] is bool(extra)
        assert "no.such.module" in result["summary"]
        assert channel.sent == []


# ------------------------------------------------------------------ MCP and the tool list


def test_no_tool_lets_its_caller_choose_or_skip_the_check():
    for tool in tools.TOOLS:
        assert "before_send" not in inspect.signature(tool).parameters, tool.__name__


def test_mcp_send_and_edit_pass_through_the_check_with_allow_send(editable, configure):
    configure(f'"{CHECKS_MODULE}:refuse"')
    exposed = {r.split(":", 1)[1]: _without(_resolve(r)) for r in refs(allow_send=True)}
    sent = exposed["send"](ref="fake:example/demo", text="x")
    edited = exposed["edit"](ref="fake:example/demo", message_id="m1", text="x")
    reacted = exposed["react"](ref="fake:example/demo", message_id="m1", reaction="eyes")
    for result in (sent, edited, reacted):
        assert (result["ok"], result["error_kind"]) == (False, "refused"), result
    assert editable.sent == editable.edited == editable.reacted == []


def test_the_mcp_server_itself_passes_through_the_check(editable, configure):
    pytest.importorskip("py2mcp")
    from correspond.mcp import mk_server

    configure(f'"{CHECKS_MODULE}:hold"')
    server = mk_server(allow_send=True)
    for name, arguments in (
        ("send", {"ref": "fake:example/demo", "text": "x"}),
        ("edit", {"ref": "fake:example/demo", "message_id": "m1", "text": "x"}),
    ):
        answer = asyncio.run(server.call_tool(name, arguments))
        assert "needs_approval" in json.dumps(
            getattr(answer, "structured_content", None) or str(answer), default=str
        )
    assert editable.sent == [] and editable.edited == []
