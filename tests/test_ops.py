"""The verbs: NotSupported by name, writes checked against capabilities, failures as results, cursors committed at-least-once."""

from datetime import datetime, timedelta, timezone

import pytest

import correspond
from correspond import ops
from correspond.errors import ChannelError, NotSupported, UnknownChannel
from correspond.model import Capabilities, ConversationRef, Event, Grade, Support
from correspond.registry import CHANNELS, build_registry
from correspond.stores import cursor_store
from correspond.testing import FakeChannel, demo_channel, demo_message


@pytest.fixture
def registry():
    return {"fake": demo_channel()}


@pytest.mark.parametrize(
    "call, operation",
    [
        (lambda r: ops.react("fake:example/demo", "m1", "eyes", registry=r), "react"),
        (lambda r: ops.edit("fake:example/demo", "m1", "new", registry=r), "edit"),
        (lambda r: ops.listen("fake:example/demo", cursors={}, registry=r), "listen"),
        (lambda r: ops.upload("fake:example/demo", "a.txt", b"x", registry=r), "upload"),
        (lambda r: ops.verify("fake", {}, b"", registry=r), "verify"),
    ],
)
def test_an_operation_the_channel_lacks_raises_not_supported_naming_it(
    registry, call, operation
):
    with pytest.raises(NotSupported) as caught:
        call(registry)
    assert caught.value.operation == operation and caught.value.channel == "fake"
    assert operation in str(caught.value)


def test_not_supported_suggests_what_the_channel_can_do_instead(registry):
    with pytest.raises(NotSupported, match="reply with a message"):
        ops.react("fake:example/demo", "m1", "eyes", registry=registry)


def test_unknown_channels_list_what_exists_and_point_built_ins_at_requirements(registry):
    with pytest.raises(UnknownChannel, match="available: fake"):
        ops.read("nowhere:x", registry=registry)
    with pytest.raises(UnknownChannel, match="correspond requirements telegram"):
        ops.read("telegram:-4001", registry=registry)


def test_a_dry_run_sends_nothing_and_a_send_is_readable_afterwards(registry):
    fake = registry["fake"]
    planned = ops.send("fake:example/demo", "hello", dry_run=True, registry=registry)
    assert (
        planned.ok
        and planned.dry_run
        and planned.plan["text"] == "hello"
        and fake.sent == []
    )
    sent = ops.send("fake:example/demo", "hello", reply_to="m2", registry=registry)
    assert (
        sent.ok
        and not sent.dry_run
        and sent.message_id == "m3"
        and sent.account.acts_as == "bot"
    )
    assert [m.text for m in ops.read("fake:example/demo", registry=registry)][
        -1
    ] == "hello"


def test_writes_are_checked_against_capabilities_before_the_adapter_runs(registry):
    fake = registry["fake"]
    empty = ops.send("fake:example/demo", "   ", registry=registry)
    assert (empty.ok, empty.error_kind) == (False, "validation")
    long = ops.send("fake:example/demo", "x" * 10_001, registry=registry)
    assert (long.ok, long.error_kind) == (False, "validation") and "10000" in long.error
    with pytest.raises(NotSupported, match="priority"):
        ops.send("fake:example/demo", "hi", priority="urgent", registry=registry)
    assert ops.send(
        "fake:example/demo", "hi", priority="normal", dry_run=True, registry=registry
    ).ok
    assert fake.sent == []


class _Refusing(FakeChannel):
    def send(self, ref, draft, *, dry_run=False):
        raise ChannelError(
            "slow down", kind="rate_limited", retryable=True, retry_after=12
        )


def test_a_platform_failure_becomes_a_result_not_an_exception():
    result = ops.send("fake:example/demo", "hello", registry={"fake": _Refusing()})
    assert (result.ok, result.error_kind, result.retryable, result.retry_after) == (
        False,
        "rate_limited",
        True,
        12.0,
    )


def test_a_reply_on_a_channel_without_replies_is_refused_by_name():
    class Flat(FakeChannel):
        @property
        def capabilities(self):
            return Capabilities(channel=self.name, send=Support.FULL, read=Support.FULL)

    with pytest.raises(NotSupported, match="reply"):
        ops.send("fake:example/demo", "hi", reply_to="m1", registry={"fake": Flat()})


def test_read_windows_by_time_and_keeps_the_most_recent(registry):
    messages = ops.read("fake:example/demo", limit=1, registry=registry)
    assert [m.id for m in messages] == ["m2"]
    assert (
        ops.read("fake:example/demo", since="2026-09-11T09:04:00Z", registry=registry)[
            0
        ].id
        == "m2"
    )


def test_references_are_normalised_by_their_adapter(registry):
    assert ops.parse_ref("fake:example/demo", registry=registry).kind == "thread"
    with pytest.raises(correspond.InvalidRef):
        ops.parse_ref("fake:not a ref", registry=registry)


class _Stream(FakeChannel):
    """A listener over a fixed list of events, ending with a final cursor."""

    def __init__(self, *, final="c-final"):
        super().__init__()
        self.final = final
        self.polled_with = []

    def poll(self, ref, *, cursor=None, limit=None):
        self.polled_with.append(cursor)
        events = [
            Event(
                kind="message.created",
                channel=self.name,
                delivery_id=f"d{i}",
                cursor=f"c{i}",
                message=demo_message(message_id=f"m{i}"),
            )
            for i in (1, 2, 3)
        ]
        return ops.with_final_cursor(events, self.final)


def test_listen_commits_each_cursor_only_after_the_consumer_moves_on():
    stream = _Stream()
    cursors = {}
    events = ops.listen("fake:example/demo", cursors=cursors, registry={"fake": stream})
    first = next(events)
    assert first.cursor == "c1" and cursors == {}, (
        "not committed while the consumer still holds the event"
    )
    next(events)
    assert cursors == {"fake:example/demo": "c1"}
    events.close()  # the consumer stopped: c2 was handed over but never acknowledged
    assert cursors == {"fake:example/demo": "c1"}
    list(ops.listen("fake:example/demo", cursors=cursors, registry={"fake": stream}))
    assert (
        stream.polled_with == [None, "c1"] and cursors["fake:example/demo"] == "c-final"
    )


def test_peeking_moves_no_cursor_and_a_quiet_poll_still_moves_forward():
    cursors = {}
    list(
        ops.listen(
            "fake:example/demo",
            cursors=cursors,
            commit=False,
            registry={"fake": _Stream()},
        )
    )
    assert cursors == {}

    class Quiet(_Stream):
        def poll(self, ref, *, cursor=None, limit=None):
            return ops.with_final_cursor([], "c-later")

    assert (
        list(ops.listen("fake:example/demo", cursors=cursors, registry={"fake": Quiet()}))
        == []
    )
    assert cursors == {"fake:example/demo": "c-later"}


def test_cursors_persist_in_files_under_the_data_root(isolated):
    stream = _Stream()
    list(
        ops.listen("fake:example/demo", cursors=cursor_store(), registry={"fake": stream})
    )
    assert cursor_store()["fake:example/demo"] == "c-final"
    assert list((isolated / "data" / "cursors").iterdir())


def test_every_built_in_adapter_implements_exactly_the_operations_its_capabilities_grade():
    registry = build_registry()
    for info in CHANNELS:
        if info.planned:
            continue
        adapter = registry[info.name]
        assert adapter.name == info.name
        assert ops.implemented(adapter) == adapter.capabilities.operations, info.name
        assert isinstance(adapter, ops.Channel)
    fake = demo_channel()
    assert ops.implemented(fake) == fake.capabilities.operations == ("read", "send")


def test_window_keeps_edited_messages_and_orders_by_time():
    late = demo_message(
        message_id="late", sent_at=datetime(2026, 9, 11, 10, tzinfo=timezone.utc)
    )
    early = demo_message(message_id="early")
    edited = demo_message(
        message_id="edited", sent_at=datetime(2026, 9, 10, tzinfo=timezone.utc)
    )
    edited = type(edited)(
        **{**edited.__dict__, "edited_at": datetime(2026, 9, 11, 11, tzinfo=timezone.utc)}
    )
    since = datetime(2026, 9, 11, 9, 30, tzinfo=timezone.utc)
    assert [m.id for m in ops.window([late, early, edited], since=since)] == [
        "edited",
        "late",
    ]
    assert [m.id for m in ops.window([late, early], limit=1)] == ["late"]
    assert Grade.PLATFORM in {m.authenticity.grade for m in (late, early)}
    assert ConversationRef.parse("fake:example/demo") == late.conversation
    assert timedelta(0) <= datetime.now(timezone.utc) - late.sent_at
