"""Idempotent sends: a key that sent posts nothing again, even after a send that went out and then failed.

Every channel here is in memory (correspond.testing); nothing leaves the process.
"""

from dataclasses import replace
from datetime import timedelta

import pytest

from correspond import idempotency, ops
from correspond.errors import ChannelError
from correspond.idempotency import ATTEMPTED, FAILED, SENT
from correspond.model import Draft
from correspond.stores import send_store
from correspond.testing import _EPOCH, FakeChannel, demo_channel

REF = "fake:example/demo"
TEXT = "The fix is deployed."


class PostsThenFails(FakeChannel):
    """Posts the message, then raises: the platform took it and its answer never came."""

    def __init__(self, *args, kind="network", post=True, **kwargs):
        super().__init__(*args, **kwargs)
        self.kind, self.post, self.calls = kind, post, 0

    def send(self, ref, draft, *, dry_run=False):
        if dry_run:
            return super().send(ref, draft, dry_run=True)
        self.calls += 1
        if self.calls == 1:
            if self.post:
                super().send(ref, draft)
            raise ChannelError("the platform did not answer", kind=self.kind)
        return super().send(ref, draft)


class WriteOnly:
    """A channel that can send and cannot be read (as a notification service)."""

    def __init__(self, inner):
        self.inner, self.name = inner, inner.name

    @property
    def capabilities(self):
        return self.inner.capabilities

    def parse_ref(self, id):
        return self.inner.parse_ref(id)

    def send(self, ref, draft, *, dry_run=False):
        return self.inner.send(ref, draft, dry_run=dry_run)


def _seeded(cls=FakeChannel, **kwargs):
    return cls("fake", conversations=demo_channel().conversations, **kwargs)


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    """Claims are stamped at the fake channel's epoch, where its messages are dated."""
    monkeypatch.setattr(idempotency, "now", lambda: _EPOCH)


def _send(channel, text=TEXT, *, sends, key="k-1", **kwargs):
    return ops.send(
        REF, text, registry={"fake": channel}, idempotency_key=key, sends=sends, **kwargs
    )


def test_a_key_that_sent_posts_nothing_again_and_answers_the_first_result():
    channel, sends = _seeded(), {}

    first = _send(channel, sends=sends)
    again = _send(channel, sends=sends)

    assert first.ok and again.ok and len(channel.sent) == 1
    assert again.message_id == first.message_id
    assert "not sent again" in again.plan["idempotency"]
    assert sends["k-1"]["state"] == SENT and sends["k-1"]["result"]["ok"] is True


def test_a_send_that_posted_then_failed_is_confirmed_by_reading_back_not_posted_twice():
    channel, sends = _seeded(PostsThenFails), {}

    failed = _send(channel, sends=sends)
    assert not failed.ok and failed.error_kind == "network"
    assert sends["k-1"]["state"] == ATTEMPTED and len(channel.sent) == 1

    retried = _send(channel, sends=sends)

    assert retried.ok and len(channel.sent) == 1 and channel.calls == 1
    posted = channel.conversations["example/demo"][-1]
    assert retried.message_id == posted.id and "it did" in retried.plan["idempotency"]
    assert sends["k-1"]["state"] == SENT
    assert _send(channel, sends=sends).message_id == posted.id and len(channel.sent) == 1


def test_an_attempt_that_did_not_post_and_cannot_be_confirmed_is_refused_as_unconfirmed():
    channel, sends = _seeded(PostsThenFails, post=False), {}
    _send(channel, sends=sends)

    refused = _send(channel, sends=sends)

    assert not refused.ok and refused.error_kind == "unconfirmed"
    assert "check fake:example/demo" in refused.error and "new key" in refused.error
    assert channel.sent == [] and sends["k-1"]["state"] == ATTEMPTED
    # The caller who checked sends with a new key.
    assert _send(channel, sends=sends, key="k-2").ok and len(channel.sent) == 1


def test_a_channel_that_cannot_be_read_back_confirms_nothing():
    inner = _seeded(PostsThenFails)
    channel, sends = WriteOnly(inner), {}
    _send(channel, sends=sends)

    refused = _send(channel, sends=sends)

    assert refused.error_kind == "unconfirmed" and "cannot be read back" in refused.error
    assert len(inner.sent) == 1


def test_a_message_read_back_from_before_the_attempt_is_not_this_attempt(monkeypatch):
    channel, sends = _seeded(PostsThenFails, post=False), {}
    FakeChannel.send(
        channel, channel.parse_ref("example/demo"), Draft(text=TEXT)
    )  # an old copy
    monkeypatch.setattr(idempotency, "now", lambda: _EPOCH + timedelta(hours=1))
    _send(channel, sends=sends)

    assert _send(channel, sends=sends).error_kind == "unconfirmed"


def test_a_refusal_before_anything_was_accepted_lets_the_same_key_try_again():
    channel, sends = _seeded(PostsThenFails, kind="rate_limited", post=False), {}

    assert _send(channel, sends=sends).error_kind == "rate_limited"
    assert sends["k-1"]["state"] == FAILED

    again = _send(channel, sends=sends)
    assert again.ok and len(channel.sent) == 1 and sends["k-1"]["state"] == SENT


def test_a_key_used_again_for_another_message_or_conversation_is_refused():
    channel, sends = _seeded(), {}
    _send(channel, sends=sends)

    other = _send(channel, "Another message.", sends=sends)

    assert not other.ok and other.error_kind == "validation" and "new key" in other.error
    assert len(channel.sent) == 1


def test_a_crash_between_claim_and_result_leaves_the_key_attempted():
    class Crashes(FakeChannel):
        def send(self, ref, draft, *, dry_run=False):
            if dry_run:
                return super().send(ref, draft, dry_run=True)
            super().send(ref, draft)
            raise RuntimeError("the process died here")

    channel, sends = _seeded(Crashes), {}
    with pytest.raises(RuntimeError):
        _send(channel, sends=sends)
    assert sends["k-1"]["state"] == ATTEMPTED

    healthy = _seeded()
    healthy.conversations = channel.conversations
    assert _send(healthy, sends=sends).ok and healthy.sent == []


def test_a_dry_run_reads_the_store_and_writes_nothing():
    channel, sends = _seeded(PostsThenFails), {}

    planned = _send(channel, sends=sends, dry_run=True)
    assert planned.ok and planned.dry_run and sends == {} and channel.sent == []

    _send(channel, sends=sends)
    before = dict(sends)
    rehearsed = _send(channel, sends=sends, dry_run=True)
    assert (
        rehearsed.error_kind == "unconfirmed" and "reads" in rehearsed.plan["idempotency"]
    )
    assert sends == before and len(channel.sent) == 1


def test_a_before_send_refusal_claims_nothing():
    from correspond.errors import Refused

    def refuse(ref, draft, audience, **context):
        raise Refused("not on this conversation")

    channel, sends = _seeded(), {}
    stopped = _send(channel, sends=sends, before_send=refuse)

    assert stopped.error_kind == "refused" and sends == {} and channel.sent == []


def test_without_a_key_nothing_is_stored_and_every_send_posts():
    channel = _seeded()
    for _ in range(2):
        assert ops.send(REF, TEXT, registry={"fake": channel}).ok
    assert len(channel.sent) == 2


@pytest.mark.parametrize("key", ["", "   ", 12, "a" * 129, "tab\tinside"])
def test_a_key_that_is_blank_too_long_or_unprintable_raises(key):
    with pytest.raises(ValueError, match="idempotency key"):
        _send(_seeded(), sends={}, key=key)


def test_the_default_store_keeps_keys_as_files_under_the_data_root(tmp_path):
    channel = _seeded()
    ops.send(REF, TEXT, registry={"fake": channel}, idempotency_key="case-12/reply 3")

    store = send_store()
    assert store["case-12/reply 3"]["state"] == SENT
    assert list((tmp_path / "data" / "sends").iterdir())
    again = ops.send(
        REF, TEXT, registry={"fake": channel}, idempotency_key="case-12/reply 3"
    )
    assert again.ok and len(channel.sent) == 1


def test_a_send_that_overlaps_another_with_the_same_key_posts_nothing():
    channel, sends, inner = _seeded(), {}, []

    def retry_while_checking(ref, draft, audience, **context):
        if (
            not inner
        ):  # an MCP client that timed out retries while this call is in its check
            inner.append(_send(channel, sends=sends, before_send=lambda *a, **k: None))

    outer = _send(channel, sends=sends, before_send=retry_while_checking)

    assert inner[0].ok and outer.ok and len(channel.sent) == 1
    assert outer.message_id == inner[0].message_id
    assert sends["k-1"]["state"] == SENT


def test_the_default_store_claims_a_key_for_exactly_one_caller(tmp_path):
    store = send_store(data_dir=tmp_path)
    record = {"key": "k", "state": ATTEMPTED}

    assert store.claim("k", record) and not store.claim("k", {**record, "state": "x"})
    assert store["k"] == record
    store["k"] = {**record, "state": FAILED}  # a refused write frees the key
    assert (
        store.claim("k", {**record, "state": "again"}) and store["k"]["state"] == "again"
    )


def test_a_record_left_half_written_reads_as_an_attempt_of_unknown_outcome(tmp_path):
    store = send_store(data_dir=tmp_path)
    folder = tmp_path / "sends"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / store.name("k-1")).write_text('{"key": "k-1", "sta', encoding="utf-8")
    channel = _seeded()

    refused = _send(channel, sends=store)

    assert store["k-1"]["state"] == ATTEMPTED and not store.claim("k-1", {})
    assert refused.error_kind == "unconfirmed" and channel.sent == []
    assert len(list(store)) == 1  # a corrupt record is listed, not raised


def test_a_missing_requirement_raised_during_the_write_lets_the_key_be_used_again():
    from correspond.errors import MissingRequirement

    class NoToken(FakeChannel):
        def send(self, ref, draft, *, dry_run=False):
            if not dry_run:
                raise MissingRequirement("fake", "no token", fix="set one")
            return super().send(ref, draft, dry_run=True)

    sends = {}
    assert not _send(_seeded(NoToken), sends=sends).ok
    assert sends["k-1"]["state"] == FAILED
    assert _send(_seeded(), sends=sends).ok


@pytest.mark.parametrize("key", ["/" * 128, "é" * 128, "case-12/reply 3"])
def test_any_accepted_key_is_kept_under_a_short_safe_file_name(tmp_path, key):
    channel, store = _seeded(), send_store(data_dir=tmp_path)

    assert _send(channel, sends=store, key=key).ok
    assert list(store) == [key] and store[key]["state"] == SENT
    assert _send(channel, sends=store, key=key).ok and len(channel.sent) == 1


def test_a_mistake_raised_during_the_write_lets_the_key_be_used_again():
    from correspond.errors import NotSupported

    class RefusesReplies(FakeChannel):
        def send(self, ref, draft, *, dry_run=False):
            if not dry_run and draft.reply_to:
                raise NotSupported("reply", self.name)
            return super().send(ref, draft, dry_run=dry_run)

    channel, sends = _seeded(RefusesReplies), {}
    with pytest.raises(NotSupported):
        ops.send(
            REF,
            Draft(text=TEXT, reply_to="m1"),
            registry={"fake": channel},
            idempotency_key="k",
            sends=sends,
        )
    assert sends["k"]["state"] == FAILED


def test_a_platform_validation_error_after_the_claim_is_not_taken_as_nothing_posted():
    channel, sends = _seeded(PostsThenFails, kind="validation"), {}

    _send(channel, sends=sends)

    assert sends["k-1"]["state"] == ATTEMPTED
    assert (
        _send(channel, sends=sends).ok and len(channel.sent) == 1
    )  # confirmed by read-back


def test_a_failed_key_is_still_bound_to_its_message():
    channel, sends = _seeded(PostsThenFails, kind="rate_limited", post=False), {}
    _send(channel, sends=sends)

    other = _send(channel, "Another message.", sends=sends)

    assert other.error_kind == "validation" and channel.sent == []


def test_a_message_confirmed_by_reading_back_names_its_own_conversation():
    from correspond.model import ConversationRef

    class OpensElsewhere(PostsThenFails):
        def read(self, ref, *, since=None, limit=None):
            messages = super().read(ref, since=since, limit=limit)
            moved = ConversationRef(channel="fake", id="example/demo#7", kind="thread")
            return [replace(m, conversation=moved) for m in messages]

    channel, sends = _seeded(OpensElsewhere), {}
    _send(channel, sends=sends)

    assert _send(channel, sends=sends).conversation == "fake:example/demo#7"


def test_a_replay_keeps_the_plan_of_the_send_it_replays():
    channel, sends = _seeded(), {}
    first = _send(channel, sends=sends)

    again = _send(channel, sends=sends)

    assert (
        "before_send" in first.plan
        and again.plan["before_send"] == first.plan["before_send"]
    )


def test_the_send_tool_passes_its_key_through():
    from correspond import registry, tools

    channel = registry.register_channel(_seeded(), replace=True)
    try:
        first = tools.send(REF, TEXT, idempotency_key="tool-k")
        again = tools.send(REF, TEXT, idempotency_key="tool-k")
    finally:
        registry.unregister_channel("fake")

    assert first["ok"] and again["ok"] and len(channel.sent) == 1
    assert again["plan"]["idempotency_key"] == "tool-k"
