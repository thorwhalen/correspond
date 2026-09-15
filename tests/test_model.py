"""The data model: references round-trip, grades are unranked, every type survives JSON, an audience hashes by contract."""

import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from correspond.errors import ChannelError, CorrespondError, InvalidRef
from correspond.model import (
    AUDIENCE_UNHASHED,
    CLASS_WORDS,
    DURABILITY,
    OPERATIONS,
    WIDENING,
    Account,
    Attachment,
    Audience,
    Authenticity,
    Capabilities,
    ChannelIdentity,
    ConversationRef,
    Draft,
    Event,
    Grade,
    HistoryDepth,
    Message,
    Scope,
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


def test_capabilities_grade_audience_and_records_without_it_still_load():
    assert OPERATIONS[-1] == "audience"
    caps = Capabilities(channel="x", audience="full")
    assert caps.supports("audience") is Support.FULL and caps.operations == ("audience",)
    older = caps.to_dict()
    del older["audience"]
    assert Capabilities.from_dict(older).audience is Support.NONE


WATCHERS = "watchers and participants receive the body by email"
BASE = "every member of the organisation through its base permission (documented default: read)"


def _audience(**changes):
    fields = dict(
        ref="github:example/app#12",
        scope="org",
        readers=[
            ChannelIdentity(
                channel="github", native_id="7", handle="example-bot", is_self=True
            ),
            ChannelIdentity(channel="github", native_id="3", handle="ada"),
        ],
        classes=[WATCHERS, BASE],
        durability=["edit_history_visible", "copies_pushed"],
        widening=["visibility_flip", "forks", "joiners_read_history"],
        as_of="2026-09-15T12:00:00Z",
        evidence=["GET repos/example/app: visibility private"],
    )
    fields.update(changes)
    return Audience(**fields)


def test_an_audience_survives_json_and_ignores_keys_it_does_not_know():
    audience = _audience()
    data = json.loads(json.dumps(audience.to_dict()))
    assert Audience.from_dict(data) == audience
    assert Audience.from_dict({**data, "added_later": [1], "hash": "x"}) == audience
    assert list(data) == [
        "ref",
        "scope",
        "readers",
        "complete",
        "classes",
        "external",
        "retractable",
        "durability",
        "widening",
        "as_of",
        "evidence",
        "defaulted",
    ]
    assert (data["scope"], data["as_of"], data["external"]) == (
        "org",
        "2026-09-15T12:00:00Z",
        None,
    )


def test_set_valued_fields_are_sorted_so_the_listing_order_never_matters():
    audience = _audience()
    shuffled = _audience(
        readers=[*reversed(audience.readers), audience.readers[0]],
        classes=[*reversed(audience.classes), WATCHERS],
        durability=list(reversed(audience.durability)),
        widening=list(reversed(audience.widening)),
    )
    assert shuffled == audience and shuffled.hash == audience.hash
    assert audience.durability == ("copies_pushed", "edit_history_visible")
    assert [r.handle for r in audience.readers] == ["ada", "example-bot"]


def test_the_hash_covers_everything_but_when_and_how_it_was_computed():
    audience = _audience()
    assert AUDIENCE_UNHASHED == ("as_of", "evidence")
    later = _audience(as_of="2026-10-01T08:00:00+02:00", evidence=["another call"])
    assert later != audience and later.hash == audience.hash
    canonical = json.dumps(
        {k: v for k, v in audience.to_dict().items() if k not in AUDIENCE_UNHASHED},
        sort_keys=True,
        separators=(",", ":"),
    )
    assert audience.hash == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    changes = [
        dict(ref="github:example/app#13"),
        dict(scope="public"),
        dict(scope="public", defaulted=True),
        dict(readers=audience.readers[:1]),
        dict(complete=True),
        dict(classes=[WATCHERS]),
        dict(external=True),
        dict(retractable=True),
        dict(durability=["copies_pushed"]),
        dict(widening=["forks"]),
    ]
    hashes = {audience.hash} | {_audience(**change).hash for change in changes}
    assert len(hashes) == len(changes) + 1


def test_an_audience_refuses_unknown_vocabulary_and_a_default_other_than_public():
    for bad in (
        dict(scope="everyone"),
        dict(durability=["carved_in_stone"]),
        dict(widening=["gossip"]),
        dict(as_of=""),
    ):
        with pytest.raises(ValueError):
            _audience(**bad)
    for bad in (
        dict(classes="one string"),
        dict(durability="indexed"),
        dict(readers=[{"channel": "github", "native_id": "3"}]),
        dict(complete="yes"),
        dict(external="maybe"),
        dict(ref=ConversationRef.parse("github:example/app")),
    ):
        with pytest.raises(TypeError):
            _audience(**bad)
    for bad in (dict(defaulted=True), dict(scope="public", complete=True, defaulted=True)):
        with pytest.raises(ValueError, match="unknown resolves to public"):
            _audience(**bad)


def test_the_unknown_audience_assumes_the_widest_of_everything():
    found = Audience.unknown("slack:example", "slack is planned")
    assert (
        found.scope,
        found.complete,
        found.external,
        found.retractable,
        found.defaulted,
    ) == (Scope.PUBLIC, False, None, False, True)
    assert set(found.durability) == set(DURABILITY) and set(found.widening) == set(WIDENING)
    assert found.evidence == ("slack is planned", "unknown resolves to public")
    assert (
        found.in_words()
        == "world-readable (assumed: the audience could not be determined); not retractable"
    )


def test_an_audience_in_words_leads_with_its_scope():
    assert CLASS_WORDS[WATCHERS] == "emailed to watchers and participants"
    assert _audience().in_words() == (
        "organisation-wide; at least 2 known readers; " + BASE + "; "
        "emailed to watchers and participants; edits keep a visible history; not retractable"
    )
    assert (
        _audience(scope="named", complete=True, classes=[], durability=[]).in_words()
        == "named readers; exactly 2 readers; not retractable"
    )
    assert (
        _audience(
            scope="operator", readers=[], classes=[], durability=[], retractable=True
        ).in_words()
        == "only the operator; retractable"
    )
