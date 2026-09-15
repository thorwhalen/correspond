"""The audience operation: unknown resolves to public from every direction, and every surface shows the same record, in words first."""

import hashlib
import json
import os
import subprocess
import sys

import pytest

import correspond
from correspond import ops, registry, tools
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
def test_unknown_resolves_to_public_whatever_went_wrong(ref, channels, expected_ref, reason):
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


def test_only_github_reads_audiences_so_far():
    built = build_registry()
    grading = {
        c.name
        for c in CHANNELS
        if not c.planned and built[c.name].capabilities.audience is not Support.NONE
    }
    assert grading == {"github"}
    github = tools.capabilities("github")
    assert github["capabilities"]["audience"] == "full"
    assert "audience" in github["operations"]
    assert "audience: full" in github["text"].splitlines()


def test_the_tool_result_holds_the_record_its_hash_and_words(monkeypatch):
    monkeypatch.setattr(registry, "channels", lambda: {"room": _Room()})
    result = tools.audience("room:Kitchen")
    json.dumps(result)
    record = result["audience"]
    assert result["ok"] and (record["ref"], record["scope"]) == ("room:kitchen", "operator")
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
    record = json.loads(_cli(["audience", "fake:example/demo", "--json"]).stdout)["audience"]
    assert (record["scope"], record["defaulted"], record["complete"]) == (
        "public",
        True,
        False,
    )


def test_mcp_exposes_audience_as_a_read():
    assert tools.SIDE_EFFECTS["audience"] == "external-read"
    assert "correspond.tools:audience" in refs()
