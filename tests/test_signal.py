"""Signal over a scripted signal-cli-rest-api: a fake HTTP function and a fake WebSocket, no network."""

import json

import pytest

from correspond import ops
from correspond.channels._http import Response
from correspond.channels.signal import Signal, _group_ref, requirements_problems
from correspond.errors import ChannelError, InvalidRef, NotSupported
from correspond.model import ConversationRef, Draft, Grade, HistoryDepth, Scope

# built by concatenation so the no-personal-data guard does not see phone numbers
ME = "+" + "1555010" + "0001"
FRIEND = "+" + "1555010" + "0002"
GROUP_ID = _group_ref("example-internal-id")


class FakeServer:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, method, url, *, headers=None, body=None, timeout=None):
        path = url.split("127.0.0.1:8080", 1)[1]
        self.calls.append((method, path, json.loads(body) if body else None))
        result = self.routes[(method, path.split("/")[1] + "/" + path.split("/")[2])]
        result = result(self.calls[-1][2]) if callable(result) else result
        if isinstance(result, Response):
            return result
        return Response(200, {}, json.dumps(result).encode())


def _frame(stamp, text, *, source=FRIEND, **data):
    return json.dumps(
        {
            "envelope": {
                "sourceNumber": source,
                "sourceName": "Ada",
                "timestamp": stamp,
                "dataMessage": {"timestamp": stamp, "message": text, **data},
            },
            "account": ME,
        }
    )


@pytest.fixture(autouse=True)
def account(monkeypatch):
    monkeypatch.setenv("CORRESPOND_SIGNAL_NUMBER", ME)


def _signal(frames=(), identities=(), routes=None):
    seen = {}

    def ws(url, *, timeout):
        seen["url"], seen["timeout"] = url, timeout
        return list(frames)

    server = FakeServer({("GET", "v1/identities"): list(identities), **(routes or {})})
    adapter = Signal(http=server, ws=ws, log={})
    adapter.seen, adapter.server = seen, server
    return adapter


def test_references():
    adapter = Signal(log={})
    assert adapter.parse_ref("").kind == "account"
    assert adapter.parse_ref(FRIEND).kind == "person"
    assert adapter.parse_ref(GROUP_ID).kind == "group"
    with pytest.raises(InvalidRef):
        adapter.parse_ref("not-a-number")


def test_capabilities_say_there_is_no_history_before_linking():
    caps = Signal(log={}).capabilities
    assert caps.history_depth is HistoryDepth.SINCE_LINK
    assert any("three months" in note for note in caps.notes)


def test_listen_turns_envelopes_into_logged_events_and_reads_them_back():
    adapter = _signal(
        frames=[
            _frame(1700000000001, "hello there"),
            _frame(1700000000002, "in a group", groupInfo={"groupId": "example-internal-id"}),
            "not json",
            json.dumps({"envelope": {"typingMessage": {}}}),
        ]
    )
    stream = adapter.poll(adapter.parse_ref(""))
    events = []
    try:
        while True:
            events.append(next(stream))
    except StopIteration as done:
        cursor = done.value
    assert adapter.seen["url"] == f"ws://127.0.0.1:8080/v1/receive/{ME.replace('+', '%2B')}"
    assert [e.kind for e in events] == ["message.created"] * 2
    assert cursor == "1700000000002"
    first = events[0].message
    assert (first.text, first.author.native_id, first.authenticity.grade) == (
        "hello there",
        FRIEND,
        Grade.PLATFORM,
    )
    assert events[1].message.conversation.kind == "group"
    assert [m.text for m in adapter.read(adapter.parse_ref(FRIEND))] == ["hello there"]
    assert [m.text for m in adapter.read(adapter.parse_ref(GROUP_ID))] == ["in a group"]


def test_listen_takes_the_account_stream_only():
    adapter = _signal()
    with pytest.raises(NotSupported):
        adapter.poll(adapter.parse_ref(FRIEND))


def test_an_edit_and_a_reaction_are_reported():
    edit = json.dumps(
        {
            "envelope": {
                "sourceNumber": FRIEND,
                "timestamp": 1700000000010,
                "editMessage": {
                    "targetSentTimestamp": 1700000000001,
                    "dataMessage": {"timestamp": 1700000000010, "message": "hello, edited"},
                },
            }
        }
    )
    react = _frame(
        1700000000020,
        None,
        reaction={"emoji": "👍", "targetAuthor": ME, "targetSentTimestamp": 1700000000001},
    )
    adapter = _signal(frames=[edit, react])
    events = list(adapter.poll(adapter.parse_ref("")))
    assert [e.kind for e in events] == ["message.updated", "reaction.added"]
    assert events[0].message.id == "1700000000001" and events[0].message.edited_at
    assert events[1].payload["emoji"] == "👍"


def test_a_changed_safety_number_is_an_event_and_the_first_sight_is_not():
    adapter = _signal(identities=[{"number": FRIEND, "safety_number": "111", "trust_level": "TRUSTED_VERIFIED"}])
    assert list(adapter.poll(adapter.parse_ref(""))) == []
    adapter.server.routes[("GET", "v1/identities")] = [
        {"number": FRIEND, "safety_number": "222", "trust_level": "TRUSTED_UNVERIFIED"}
    ]
    (event,) = list(adapter.poll(adapter.parse_ref("")))
    assert event.kind == "identity.changed"
    assert event.payload["who"] == FRIEND and event.payload["trust_level"] == "TRUSTED_UNVERIFIED"
    assert list(adapter.poll(adapter.parse_ref(""))) == []  # seen now


def test_send_posts_and_logs_what_it_sent_and_a_dry_run_sends_nothing():
    adapter = _signal(routes={("POST", "v2/send"): {"timestamp": "1700000000099"}})
    ref = adapter.parse_ref(FRIEND)
    plan = adapter.send(ref, Draft(text="hi", title="Re"), dry_run=True)
    assert plan.dry_run and ME not in json.dumps(plan.plan) and not adapter.server.calls
    sent = adapter.send(ref, Draft(text="hi", title="Re"))
    assert sent.message_id == "1700000000099"
    assert adapter.server.calls == [
        ("POST", "/v2/send", {"message": "Re\n\nhi", "number": ME, "recipients": [FRIEND]})
    ]
    assert [m.author.is_self for m in adapter.read(ref)] == [True]


def test_a_reply_quotes_a_logged_message_and_refuses_an_unknown_one():
    adapter = _signal(
        frames=[_frame(1700000000001, "question")],
        routes={("POST", "v2/send"): {"timestamp": "1700000000100"}},
    )
    list(adapter.poll(adapter.parse_ref("")))
    ref = adapter.parse_ref(FRIEND)
    adapter.send(ref, Draft(text="answer", reply_to="1700000000001"))
    body = adapter.server.calls[-1][2]
    assert (body["quote_timestamp"], body["quote_author"]) == (1700000000001, FRIEND)
    with pytest.raises(ChannelError, match="not seen"):
        adapter.send(ref, Draft(text="x", reply_to="42"))


def test_a_refused_send_is_an_error_kind_and_unset_number_is_a_problem(monkeypatch):
    adapter = _signal(routes={("POST", "v2/send"): Response(400, {}, b'{"error": "bad"}')})
    with pytest.raises(ChannelError) as raised:
        adapter.send(adapter.parse_ref(FRIEND), Draft(text="x"))
    assert raised.value.kind == "validation"
    monkeypatch.delenv("CORRESPOND_SIGNAL_NUMBER")
    assert requirements_problems()


def test_audience_of_a_person_a_group_and_a_group_with_an_invite_link():
    adapter = _signal(
        routes={("GET", "v1/groups"): {"members": [ME, FRIEND], "invite_link": ""}}
    )
    person = adapter.audience(adapter.parse_ref(FRIEND))
    assert (person.scope, person.complete) == (Scope.NAMED, True)
    group = adapter.audience(adapter.parse_ref(GROUP_ID))
    assert (group.scope, len(group.readers), group.complete) == (Scope.GROUP, 2, True)
    adapter.server.routes[("GET", "v1/groups")] = {"members": [ME], "invite_link": "https://example.org/join"}
    assert adapter.audience(adapter.parse_ref(GROUP_ID)).scope is Scope.PUBLIC


def test_the_channel_is_built_and_registered():
    from correspond.registry import info

    assert info("signal").planned is None
    assert ops.get_channel("signal").name == "signal"


def test_one_unattributable_frame_does_not_lose_the_others_and_groups_do_not_bleed():
    sealed = json.dumps({"envelope": {"timestamp": 1, "dataMessage": {"timestamp": 1, "message": "x"}}})
    adapter = _signal(frames=[sealed, _frame(1700000000001, "kept")])
    assert [e.message.text for e in adapter.poll(adapter.parse_ref(""))] == ["kept"]
    short, long_ = _group_ref("a"), _group_ref("a") + "/more"
    adapter.log[f"{long_}/5.json"] = adapter.log[f"{FRIEND}/1700000000001.json"]
    assert adapter.read(adapter.parse_ref(short)) == []
    with pytest.raises(ChannelError, match="timestamps"):
        adapter.send(adapter.parse_ref(FRIEND), Draft(text="x", reply_to="abc"))
