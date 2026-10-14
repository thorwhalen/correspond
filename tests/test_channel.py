"""The Claude Code channel server: notification shape, dedupe and resume, the sender allowlist, grade-gated approval, and the wire format over an in-memory MCP transport."""

import json
import re

import pytest

from correspond.channel import (
    APPROVE_GRADES,
    CHANNEL_CAPABILITY,
    CHANNEL_METHOD,
    META_KEYS,
    PERMISSION_CAPABILITY,
    PERMISSION_METHOD,
    PERMISSION_REQUEST_METHOD,
    Gate,
    channel_notification,
    notifications,
    options,
    parse_verdict,
)
from correspond.model import Event, Grade
from correspond.ops import with_final_cursor
from correspond.testing import FakeChannel, demo_message

REF = "fake:example/demo"
RELAY = "fake:example/relay"


class ListeningFake(FakeChannel):
    """The fake channel plus a scripted ``poll``: each event's cursor is its position."""

    def __init__(self, events=(), **kwargs):
        super().__init__(**kwargs)
        self.events = list(events)
        self.polls = []

    def poll(self, ref, *, cursor=None, limit=None):
        self.polls.append((ref.encoded, cursor))
        start = int(cursor or 0)
        mine = [e for e in self.events if e.message.conversation.encoded == ref.encoded]
        return with_final_cursor(iter(mine[start:]), str(len(mine)))


def _event(
    n,
    *,
    handle="ada",
    grade=Grade.PLATFORM,
    text=None,
    conversation=REF,
    kind="message.created",
    is_self=False,
    delivery_id=None,
):
    return Event(
        kind=kind,
        channel="fake",
        delivery_id=delivery_id or f"d{n}",
        cursor=None,
        message=demo_message(
            conversation=conversation,
            message_id=f"m{n}",
            text=text or f"message {n}",
            handle=handle,
            grade=grade,
            is_self=is_self,
        ),
    )


def _events_with_cursors(events):
    """Give each event of one conversation its 1-based position as cursor."""
    return [Event(**{**e.__dict__, "cursor": str(i + 1)}) for i, e in enumerate(events)]


ADA = Gate(allow=frozenset({"fake:ada"}))


# --------------------------------------------------------------------- shape


def test_a_notification_has_content_and_exactly_the_identifier_meta_keys():
    note = channel_notification(_event(1))
    assert note["method"] == CHANNEL_METHOD == "notifications/claude/channel"
    assert set(note["params"]) == {"content", "meta"}
    meta = note["params"]["meta"]
    assert (
        tuple(meta)
        == META_KEYS
        == ("channel", "conversation", "message_id", "sender", "grade")
    )
    assert all(re.fullmatch(r"[A-Za-z0-9_]+", key) for key in meta)
    assert all(isinstance(v, str) for v in meta.values())
    assert meta == {
        "channel": "fake",
        "conversation": REF,
        "message_id": "m1",
        "sender": "fake:ada",
        "grade": "platform",
    }
    assert note["params"]["content"] == "message 1"
    json.dumps(note)


def test_an_edited_message_says_so():
    note = channel_notification(_event(1, kind="message.updated"))
    assert note["params"]["content"].startswith("(edited) ")


# ----------------------------------------------------------- dedupe and resume


def test_notifications_dedupe_on_delivery_id_and_resume_from_the_cursor():
    events = _events_with_cursors([_event(1), _event(2), _event(3, delivery_id="d2")])
    fake = ListeningFake(events)
    cursors, seen = {}, {}
    notes = list(
        notifications(
            [REF], gate=ADA, cursors=cursors, registry={"fake": fake}, seen=seen
        )
    )
    assert [n["params"]["meta"]["message_id"] for n in notes] == [
        "m1",
        "m2",
    ]  # d2 repeated
    assert cursors == {REF: "3"}
    fake.events += _events_with_cursors([*events, _event(4)])[3:]
    again = list(
        notifications(
            [REF], gate=ADA, cursors=cursors, registry={"fake": fake}, seen=seen
        )
    )
    assert [n["params"]["meta"]["message_id"] for n in again] == ["m4"]
    assert fake.polls[-1] == (REF, "3")


def test_the_cursor_moves_only_once_the_consumer_asks_for_the_next_notification():
    fake = ListeningFake(_events_with_cursors([_event(1), _event(2)]))
    cursors = {}
    batch = notifications([REF], gate=ADA, cursors=cursors, registry={"fake": fake})
    next(batch)
    assert REF not in cursors  # not yet emitted, as far as the cursor knows
    next(batch)
    assert cursors[REF] == "1"


def test_a_failing_ref_is_logged_and_the_others_still_polled():
    fake = ListeningFake(_events_with_cursors([_event(1)]))
    logged = []
    notes = list(
        notifications(
            ["nowhere:x", REF],
            gate=ADA,
            cursors={},
            registry={"fake": fake},
            log=logged.append,
        )
    )
    assert len(notes) == 1 and "nowhere:x" in logged[0]


# ------------------------------------------------------------------ allowlist


def test_an_empty_allowlist_forwards_nothing_unless_opened():
    event = _event(1)
    assert Gate().route(event) is None
    assert Gate(allow_any=True).route(event) is not None


def test_only_allowlisted_senders_are_forwarded_matched_on_address_or_native_id():
    assert ADA.route(_event(1, handle="mallory")) is None
    assert ADA.route(_event(1)) is not None
    assert Gate(allow=frozenset({"fake:ada"})).route(_event(1)) is not None


def test_forged_own_and_senderless_events_are_never_forwarded():
    open_gate = Gate(allow_any=True)
    assert open_gate.route(_event(1, grade=Grade.FORGED)) is None
    assert open_gate.route(_event(1, is_self=True)) is None
    assert (
        open_gate.route(Event(kind="reaction.added", channel="fake", delivery_id="r1"))
        is None
    )


# ----------------------------------------------------------- grade-gated approval


def test_verdicts_parse_like_claude_codes_ids():
    assert parse_verdict("y abcde") == ("allow", "abcde")
    assert parse_verdict("NO Abcde") == ("deny", "abcde")
    assert parse_verdict("yes abcdl") is None  # no l in the id alphabet
    assert parse_verdict("approve it") is None


def test_only_an_authenticated_allowlisted_sender_may_approve_a_pending_request():
    gate = Gate(allow=frozenset({"fake:ada"}), relay_to=RELAY)
    verdict = _event(1, text="yes abcde", conversation=RELAY)

    pending = {"abcde"}
    note = gate.route(verdict, pending=pending)
    assert note == {
        "method": PERMISSION_METHOD,
        "params": {"request_id": "abcde", "behavior": "allow"},
    }
    assert pending == set()

    # claimed grade: no verdict, and the text does not reach Claude either
    assert (
        gate.route(
            _event(2, text="yes abcde", conversation=RELAY, grade=Grade.CLAIMED),
            pending={"abcde"},
        )
        is None
    )
    # an id this server did not relay
    assert gate.route(verdict, pending={"qwxyz"}) is None
    # open mode does not make a stranger an approver
    stranger = _event(3, text="yes abcde", conversation=RELAY, handle="mallory")
    assert Gate(allow_any=True, relay_to=RELAY).route(stranger, pending={"abcde"}) is None
    # outside the relay conversation, it is ordinary chat
    assert (
        gate.route(_event(4, text="yes abcde"), pending={"abcde"})["method"]
        == CHANNEL_METHOD
    )


def test_unauthenticated_grades_can_never_be_made_approvers():
    assert Grade.CLAIMED not in APPROVE_GRADES and Grade.FORGED not in APPROVE_GRADES
    with pytest.raises(ValueError, match="authenticated"):
        Gate(approve_grades=frozenset({Grade.CLAIMED}))


def test_the_command_line_gates_the_relay_on_allow_send():
    with pytest.raises(SystemExit, match="allow-send"):
        options([REF, "--relay-to", RELAY], config={})
    with pytest.raises(SystemExit, match="at least one"):
        options([], config={})
    with pytest.raises(SystemExit, match="authenticated"):
        options([REF, "--approve-grades", "claimed"], config={})
    got = options(
        [REF, "--allow", "github:ada"], config={"allow": ["github:bob"], "interval": 5}
    )
    assert got["gate"].allow == {"github:ada", "github:bob"} and got["interval"] == 5


def test_importing_the_channel_module_pulls_in_no_mcp_library():
    import subprocess
    import sys

    code = "import sys, correspond.channel; print(sorted({m.split('.')[0] for m in sys.modules} & {'mcp', 'fastmcp', 'py2mcp', 'anyio'}))"
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout
    assert out.strip() == "[]"


# ------------------------------------------------------ the server, on the wire


def _wire(fake, *, gate, allow_send=False, sent=None, steps):
    """Run the server over memory streams and drive it with raw JSON-RPC; ``steps(send, receive)`` is the client."""
    pytest.importorskip("py2mcp")
    anyio = pytest.importorskip("anyio")
    from mcp.shared.memory import create_client_server_memory_streams
    from mcp.shared.message import SessionMessage
    from mcp.types import JSONRPCMessage

    from correspond.channel import serve_streams

    async def run():
        async with create_client_server_memory_streams() as (client, server):
            client_read, client_write = client
            server_read, server_write = server

            async def send(payload):
                await client_write.send(
                    SessionMessage(JSONRPCMessage.model_validate(payload))
                )

            async def receive():
                with anyio.fail_after(5):
                    item = await client_read.receive()
                return item.message.model_dump(
                    by_alias=True, mode="json", exclude_none=True
                )

            async with anyio.create_task_group() as tasks:
                tasks.start_soon(
                    lambda: serve_streams(
                        server_read,
                        server_write,
                        refs=[REF],
                        gate=gate,
                        allow_send=allow_send,
                        interval=0.05,
                        cursors={},
                        registry={"fake": fake},
                        send=(lambda ref, text: sent.append((ref, text)) or _Ok()),
                        log=lambda text: None,
                    )
                )
                result = await steps(send, receive)
                tasks.cancel_scope.cancel()
            return result

    return anyio.run(run)


class _Ok:
    ok = True


async def _initialize(send, receive):
    await send(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        }
    )
    init = await receive()
    await send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    return init


def test_the_server_declares_the_channel_and_emits_notifications_on_the_wire():
    fake = ListeningFake(_events_with_cursors([_event(1), _event(2, handle="mallory")]))

    async def steps(send, receive):
        init = await _initialize(send, receive)
        note = await receive()
        await send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = await receive()
        while "result" not in tools:  # a later poll's notification may come first
            tools = await receive()
        return init, note, tools

    init, note, tools = _wire(fake, gate=ADA, steps=steps)
    experimental = init["result"]["capabilities"]["experimental"]
    assert experimental[CHANNEL_CAPABILITY] == {}
    assert PERMISSION_CAPABILITY not in experimental
    assert note["method"] == CHANNEL_METHOD
    assert note["params"]["meta"]["sender"] == "fake:ada"  # mallory is not on the list
    names = {t["name"] for t in tools["result"]["tools"]}
    assert (
        "read" in names and "send" not in names
    )  # replies are the ordinary tools, writes gated


def test_the_permission_relay_round_trip_over_the_wire():
    fake = ListeningFake()
    sent = []
    gate = Gate(allow=frozenset({"fake:ada"}), relay_to=RELAY)

    async def steps(send, receive):
        import anyio

        init = await _initialize(send, receive)
        await send(
            {
                "jsonrpc": "2.0",
                "method": PERMISSION_REQUEST_METHOD,
                "params": {
                    "request_id": "abcde",
                    "tool_name": "Bash",
                    "description": "List files",
                    "input_preview": '{"command": "ls"}',
                },
            }
        )
        with anyio.fail_after(5):
            while not sent:
                await anyio.sleep(0.01)
        fake.events += _events_with_cursors(
            [_event(9, text="yes ABCDE", conversation=RELAY)]
        )
        verdict = await receive()
        return init, verdict

    init, verdict = _wire(fake, gate=gate, allow_send=True, sent=sent, steps=steps)
    assert init["result"]["capabilities"]["experimental"][PERMISSION_CAPABILITY] == {}
    assert (
        sent and sent[0][0] == RELAY and 'Reply "yes abcde" or "no abcde"' in sent[0][1]
    )
    assert verdict == {
        "jsonrpc": "2.0",
        "method": PERMISSION_METHOD,
        "params": {"request_id": "abcde", "behavior": "allow"},
    }


# ------------------------------------------------------------- review fixes (#47)


def test_an_edited_message_never_approves():
    """GitHub keeps the original author on an edit, so anyone who may edit could forge a verdict."""
    gate = Gate(allow=frozenset({"fake:ada"}), relay_to=RELAY)
    edited = _event(1, text="yes abcde", conversation=RELAY, kind="message.updated")
    pending = {"abcde"}
    assert gate.route(edited, pending=pending) is None
    assert pending == {"abcde"}


def test_an_unexpected_adapter_error_is_logged_and_the_others_still_polled():
    class Broken(ListeningFake):
        def poll(self, *args, **kwargs):
            raise OSError("socket gone")

    fake = ListeningFake(_events_with_cursors([_event(1)]))
    logged = []
    notes = list(
        notifications(
            ["broken:example/x", REF],
            gate=ADA,
            cursors={},
            registry={"fake": fake, "broken": Broken([])},
            log=logged.append,
        )
    )
    assert len(notes) == 1
    assert any("broken:example/x" in line and "OSError" in line for line in logged), logged


def test_meta_values_cannot_break_out_of_their_attribute():
    note = channel_notification(_event(1, handle='ada" injected="1'))
    assert all('"' not in v and "<" not in v for v in note["params"]["meta"].values())


def test_an_allowlisted_own_account_message_is_dropped_loudly():
    fake = ListeningFake(_events_with_cursors([_event(1, is_self=True)]))
    logged = []
    notes = list(
        notifications([REF], gate=ADA, cursors={}, registry={"fake": fake}, log=logged.append)
    )
    assert notes == [] and "different account" in logged[0]
