"""The data model: references round-trip, grades are unranked, every type survives JSON."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from correspond.errors import ChannelError, CorrespondError, InvalidRef
from correspond.model import (
    OPERATIONS,
    Account,
    Attachment,
    Authenticity,
    Capabilities,
    ChannelIdentity,
    ConversationRef,
    Draft,
    Event,
    Grade,
    HistoryDepth,
    Message,
    SendResult,
    Support,
    format_time,
    parse_time,
)

T0 = datetime(2026, 9, 11, 9, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "text",
    [
        "github:octocat/hello-world#1",
        "github:octocat/hello-world",
        "email:ada@example.org",
        "email:",
        "ntfy:",
        "ntfy:example-topic",
        "telegram:-4001/7",
        "webinbox:example-site",
        "fake:example/demo",
    ],
)
def test_encoded_references_round_trip(text):
    ref = ConversationRef.parse(text)
    assert ref.encoded == text == str(ref)
    assert ConversationRef.parse(ref.encoded) == ref
    assert ConversationRef.from_dict(json.loads(json.dumps(ref.to_dict()))) == ref


def test_equality_is_channel_and_id_so_normalised_references_equal_parsed_ones():
    parent = ConversationRef(
        channel="github", id="octocat/hello-world", kind="repository"
    )
    normalised = ConversationRef(
        channel="github", id="octocat/hello-world#1", kind="issue", parent=parent
    )
    assert normalised == ConversationRef.parse("github:octocat/hello-world#1")
    assert hash(normalised) == hash(ConversationRef.parse("github:octocat/hello-world#1"))
    restored = ConversationRef.from_dict(normalised.to_dict())
    assert restored.kind == "issue" and restored.parent == parent


@pytest.mark.parametrize(
    "text", ["nonsense", "GitHub:octocat/x", ":id", "github:with space", "1channel:x"]
)
def test_malformed_references_raise_invalid_ref(text):
    with pytest.raises(InvalidRef):
        ConversationRef.parse(text)


def test_grades_are_a_vocabulary_without_an_order():
    assert {g.value for g in Grade} == {
        "forged",
        "claimed",
        "platform",
        "domain",
        "bound",
        "crypto",
    }
    accepted = {Grade.PLATFORM, Grade.BOUND, Grade.CRYPTO}
    assert Grade("bound") in accepted and Grade("domain") not in accepted
    for compare in (
        lambda a, b: a < b,
        lambda a, b: a <= b,
        lambda a, b: a > b,
        lambda a, b: a >= b,
    ):
        with pytest.raises(TypeError, match="not ranked"):
            compare(Grade.CLAIMED, Grade.PLATFORM)
    assert Grade.CLAIMED == "claimed"


def test_every_model_type_survives_json():
    ref = ConversationRef.parse("fake:example/demo")
    author = ChannelIdentity(
        channel="fake", native_id="583231", handle="octocat", authority="OWNER"
    )
    attachment = Attachment(
        ref="blob:ab12", media_type="image/png", name="screen.png", size=3, sha256="ab12"
    )
    message = Message(
        id="m1",
        conversation=ref,
        author=author,
        authenticity=Authenticity(grade="platform", evidence={"attested_by": "tests"}),
        sent_at=T0,
        text="hello",
        body="**hello**",
        body_format="markdown",
        attachments=[attachment],
        reply_to="m0",
        thread_root="m0",
        edited_at=T0 + timedelta(minutes=1),
        url="https://example.org/m1",
        native={"labels": ["partner:ada"]},
    )
    event = Event(
        kind="message.created",
        channel="fake",
        delivery_id="d1",
        cursor="c1",
        message=message,
    )
    caps = Capabilities(
        channel="fake",
        read="full",
        send=Support.PARTIAL,
        grades=["platform"],
        notes=["n"],
    )
    result = SendResult(
        ok=True,
        channel="fake",
        conversation=ref.encoded,
        account=Account(channel="fake", id="bot", acts_as="bot"),
    )
    for original, cls in (
        (message, Message),
        (event, Event),
        (caps, Capabilities),
        (result, SendResult),
    ):
        assert cls.from_dict(json.loads(json.dumps(original.to_dict()))) == original
    assert author.address == "fake:octocat"
    assert Draft(text="hi", priority="high").to_dict()["priority"] == "high"


def test_attachments_are_fetched_lazily_and_never_serialised():
    calls = []
    attachment = Attachment(ref="blob:ab", loader=lambda: calls.append(1) or b"bytes")
    assert calls == [] and "loader" not in attachment.to_dict()
    assert attachment.content() == b"bytes" and calls == [1]
    with pytest.raises(CorrespondError, match="cannot be fetched"):
        Attachment.from_dict(attachment.to_dict()).content()


def test_capabilities_grade_every_operation_and_coerce_strings():
    caps = Capabilities(
        channel="x", read="full", listen="partial", history_depth="buffer_24h"
    )
    assert (
        caps.supports("read") is Support.FULL
        and caps.supports("listen") is Support.PARTIAL
    )
    assert (
        caps.operations == ("read", "listen")
        and caps.history_depth is HistoryDepth.BUFFER_24H
    )
    assert set(OPERATIONS) <= set(caps.to_dict())
    with pytest.raises(ValueError):
        caps.supports("teleport")


def test_times_are_utc_and_bad_times_say_how_to_write_them():
    naive = datetime(2026, 9, 11, 12, 30)
    assert format_time(naive) == "2026-09-11T12:30:00Z"
    assert parse_time("2026-09-11T14:30:00+02:00") == datetime(
        2026, 9, 11, 12, 30, tzinfo=timezone.utc
    )
    assert parse_time("2026-09-11T12:30:00").tzinfo is not None
    with pytest.raises(CorrespondError, match="ISO 8601"):
        parse_time("yesterday")


def test_validation_of_drafts_events_accounts_and_results():
    with pytest.raises(ValueError):
        Draft(text="x", priority="whenever")
    with pytest.raises(TypeError):
        Draft(text=None)
    with pytest.raises(ValueError):
        Event(kind="message.exploded", channel="x", delivery_id="d")
    with pytest.raises(ValueError):
        Account(channel="x", id="y", acts_as="ghost")
    with pytest.raises(ValueError):
        SendResult(ok=False, channel="x", conversation="x:", error_kind="bad luck")


def test_a_failed_send_keeps_the_error_classification():
    error = ChannelError("slow down", kind="rate_limited", retryable=True, retry_after=30)
    result = SendResult.failure(
        error, channel="github", conversation="github:octocat/hello-world#1"
    )
    assert (result.ok, result.error_kind, result.retryable, result.retry_after) == (
        False,
        "rate_limited",
        True,
        30.0,
    )


def test_native_fields_are_declared_or_none():
    assert Capabilities(channel="x").native_fields is None
    declared = Capabilities(channel="x", native_fields=["labels"])
    assert declared.native_fields == ("labels",)
    for caps in (Capabilities(channel="x"), declared):
        assert Capabilities.from_dict(json.loads(json.dumps(caps.to_dict()))) == caps
