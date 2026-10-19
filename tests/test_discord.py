"""Discord over a fake discorddol backend: history, polling by message id, posting, errors as kinds, a token that never leaks."""

from datetime import datetime, timezone

import pytest

import correspond
from correspond.channels.discord import Discord
from correspond.errors import ChannelError, InvalidRef, MissingRequirement, NotSupported

CHANNEL = "123456789012345678"
TOKEN = "example.discord-token.for-tests"


def _message(message_id, *, created, content="hello", author="ada", bot=False, **extra):
    return {
        "id": str(message_id),
        "channel_id": CHANNEL,
        "created_at": created,
        "edited_at": None,
        "author": {
            "id": f"9{message_id}",
            "name": author,
            "display_name": author.title(),
            "bot": bot,
        },
        "content": content,
        "attachments": [],
        "embeds": [],
        "reply_to": None,
        "pinned": False,
        "type": "MessageType.default",
        "jump_url": f"https://discord.com/channels/1/{CHANNEL}/{message_id}",
        **extra,
    }


HISTORY = [
    _message(1001, created="2026-09-11T08:00:00+00:00"),
    _message(
        1002, created="2026-09-11T09:00:00+00:00", content="a reply", reply_to="1001"
    ),
    _message(
        1003,
        created="2026-09-11T10:00:00+00:00",
        author="example-bot",
        bot=True,
        attachments=[
            {
                "filename": "screen.png",
                "url": "https://example.org/a",
                "size": 5,
                "id": "77",
            }
        ],
    ),
]


class FakeBackend:
    """discorddol's backend calls: messages(...) filtered like DictBackend's, send_message(...)."""

    def __init__(self, messages=(), error=None):
        self.stored = list(messages)
        self.error = error
        self.calls, self.sent = [], []

    def messages(
        self, channel_id, *, limit=None, after=None, before=None, oldest_first=True
    ):
        self.calls.append(
            {
                "channel_id": channel_id,
                "limit": limit,
                "after": after,
                "oldest_first": oldest_first,
            }
        )
        if self.error:
            raise self.error
        found = [m for m in self.stored if m["channel_id"] == str(channel_id)]
        if isinstance(after, datetime):
            found = [m for m in found if datetime.fromisoformat(m["created_at"]) > after]
        elif after is not None:
            found = [m for m in found if int(m["id"]) > int(after)]
        if not oldest_first:
            found.reverse()
        return found if limit is None else found[:limit]

    def send_message(self, channel_id, text):
        if self.error:
            raise self.error
        self.sent.append((channel_id, text))
        return _message(
            2001,
            created="2026-09-11T11:00:00+00:00",
            content=text,
            author="example-bot",
            bot=True,
        )


def _registry(backend):
    return {"discord": Discord(backend=backend)}


def test_read_returns_the_latest_messages_oldest_first():
    backend = FakeBackend(HISTORY)
    messages = correspond.read(f"discord:{CHANNEL}", limit=2, registry=_registry(backend))
    assert [m.id for m in messages] == ["1002", "1003"]
    assert backend.calls[-1]["oldest_first"] is False, "the newest two, not the oldest"
    reply, bot = messages
    assert reply.reply_to == "1001" and reply.authenticity.grade.value == "platform"
    assert bot.author.is_bot and bot.attachments[0].name == "screen.png"
    assert set(bot.native) <= set(Discord().capabilities.native_fields)


def test_read_since_keeps_what_came_after():
    since = datetime(2026, 9, 11, 8, 30, tzinfo=timezone.utc)
    messages = correspond.read(
        f"discord:{CHANNEL}", since=since, registry=_registry(FakeBackend(HISTORY))
    )
    assert [m.id for m in messages] == ["1002", "1003"]


def test_listen_polls_by_message_id_and_moves_the_cursor():
    backend = FakeBackend(HISTORY)
    cursors = {f"discord:{CHANNEL}": "1001"}
    events = list(
        correspond.listen(
            f"discord:{CHANNEL}", cursors=cursors, registry=_registry(backend)
        )
    )
    assert [(e.kind, e.message.id) for e in events] == [
        ("message.created", "1002"),
        ("message.created", "1003"),
    ]
    assert cursors == {f"discord:{CHANNEL}": "1003"}
    assert (
        list(
            correspond.listen(
                f"discord:{CHANNEL}", cursors=cursors, registry=_registry(backend)
            )
        )
        == []
    )
    first = list(
        correspond.listen(f"discord:{CHANNEL}", cursors={}, registry=_registry(backend))
    )
    assert isinstance(backend.calls[-1]["after"], datetime), (
        "a first poll looks back in time, not from an id"
    )
    assert first == [], "the fixtures are older than a day"


def test_send_posts_and_prepends_a_title_and_a_dry_run_posts_nothing():
    backend = FakeBackend()
    planned = correspond.send(
        f"discord:{CHANNEL}",
        "body",
        title="Release",
        dry_run=True,
        registry=_registry(backend),
    )
    assert planned.ok and planned.dry_run and backend.sent == []
    assert planned.plan["text"] == "**Release**\n\nbody"
    result = correspond.send(f"discord:{CHANNEL}", "body", registry=_registry(backend))
    assert (
        result.ok and result.message_id == "2001" and backend.sent == [(CHANNEL, "body")]
    )


def test_what_discord_cannot_do_is_refused_by_name():
    registry = _registry(FakeBackend())
    too_long = correspond.send(f"discord:{CHANNEL}", "x" * 2001, registry=registry)
    assert (too_long.ok, too_long.error_kind) == (False, "validation")
    with pytest.raises(NotSupported, match="reply"):
        correspond.send(f"discord:{CHANNEL}", "x", reply_to="1001", registry=registry)
    with pytest.raises(NotSupported):
        correspond.react(f"discord:{CHANNEL}", "1001", "eyes", registry=registry)


@pytest.mark.parametrize(
    "error_name, kind",
    [
        ("Forbidden", "permission"),
        ("NotFound", "not_found"),
        ("LoginFailure", "auth"),
        ("TimeoutError", "unavailable"),
    ],
)
def test_backend_failures_become_classified_errors(error_name, kind):
    error = type(error_name, (Exception,), {})("refused")
    with pytest.raises(ChannelError) as caught:
        correspond.read(
            f"discord:{CHANNEL}", registry=_registry(FakeBackend(HISTORY, error=error))
        )
    assert caught.value.kind == kind


def test_references_are_snowflakes():
    discord = Discord()
    assert discord.parse_ref(f" {CHANNEL} ").encoded == f"discord:{CHANNEL}"
    for bad in ("", "general", "0123", "1" * 21, "-5"):
        with pytest.raises(InvalidRef):
            discord.parse_ref(bad)


def test_without_a_token_it_says_where_to_get_one(monkeypatch):
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    with pytest.raises(MissingRequirement, match="Developer Portal"):
        Discord().backend


def test_the_token_goes_to_discorddol_and_nowhere_else(monkeypatch):
    pytest.importorskip("discorddol")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", TOKEN)
    backend = Discord().backend
    assert backend.token == TOKEN
    planned = correspond.send(
        f"discord:{CHANNEL}",
        "hi",
        dry_run=True,
        registry={"discord": Discord(backend=backend)},
    )
    assert TOKEN not in str(planned.to_dict())


def test_the_audience_is_a_group_nobody_lists():
    audience = correspond.audience(
        f"discord:{CHANNEL}", registry=_registry(FakeBackend())
    )
    assert (audience.scope.value, audience.complete) == ("group", False)


def _burst(n, *, start=2000, day="2026-09-12"):
    return [
        _message(start + i, created=f"{day}T10:{i:02d}:00+00:00", content=f"m{i}")
        for i in range(n)
    ]


def test_read_with_since_and_limit_keeps_the_latest_not_the_oldest():
    since = datetime(2026, 9, 12, tzinfo=timezone.utc)
    messages = correspond.read(
        f"discord:{CHANNEL}",
        since=since,
        limit=3,
        registry=_registry(FakeBackend(_burst(10))),
    )
    assert [m.text for m in messages] == ["m7", "m8", "m9"]


def test_a_quiet_first_poll_sets_a_cursor_from_now():
    from correspond.channels.discord import snowflake_at

    cursors = {}
    assert (
        list(
            correspond.listen(
                f"discord:{CHANNEL}", cursors=cursors, registry=_registry(FakeBackend())
            )
        )
        == []
    )
    now = int(snowflake_at(datetime.now(timezone.utc)))
    assert 0 < now - int(cursors[f"discord:{CHANNEL}"]) <= 6000 << 22, (
        "about five seconds back"
    )


def test_a_burst_over_the_limit_drains_over_later_polls():
    backend = FakeBackend(_burst(5))
    cursors = {f"discord:{CHANNEL}": "1999"}
    seen = []
    for _ in range(3):
        seen += [
            e.message.text
            for e in correspond.listen(
                f"discord:{CHANNEL}",
                cursors=cursors,
                limit=2,
                registry=_registry(backend),
            )
        ]
    assert seen == ["m0", "m1", "m2", "m3", "m4"]


@pytest.mark.parametrize(
    "status, kind, retryable",
    [
        (400, "validation", False),
        (401, "auth", False),
        (429, "rate_limited", True),
        (503, "unavailable", True),
    ],
)
def test_an_http_failure_is_classified_by_its_status(status, kind, retryable):
    error = type("HTTPException", (Exception,), {"status": status})("Discord said no")
    with pytest.raises(ChannelError) as caught:
        correspond.read(
            f"discord:{CHANNEL}", registry=_registry(FakeBackend(HISTORY, error=error))
        )
    assert (caught.value.kind, caught.value.retryable) == (kind, retryable)


def test_a_failure_that_is_not_discords_answer_is_not_retryable():
    for error in (
        TypeError("a forum channel has no history"),
        RuntimeError("asyncio.run() cannot be called from a running event loop"),
    ):
        with pytest.raises(ChannelError) as caught:
            correspond.read(
                f"discord:{CHANNEL}",
                registry=_registry(FakeBackend(HISTORY, error=error)),
            )
        assert caught.value.retryable is False


def test_mass_mentions_are_refused_and_user_mentions_listed():
    backend = FakeBackend()
    for text in ("ping @everyone", "@here look", "for <@&4242> only"):
        result = correspond.send(f"discord:{CHANNEL}", text, registry=_registry(backend))
        assert (result.ok, result.error_kind) == (False, "validation"), text
    assert backend.sent == []
    planned = correspond.send(
        f"discord:{CHANNEL}",
        "thanks <@!42> and <@7>",
        dry_run=True,
        registry=_registry(backend),
    )
    assert planned.plan["mentions"] == ["42", "7"]


def test_a_title_is_one_escaped_bold_line():
    planned = correspond.send(
        f"discord:{CHANNEL}",
        "body",
        title="a**b\nc",
        dry_run=True,
        registry=_registry(FakeBackend()),
    )
    assert planned.plan["text"] == "**a\\*\\*b c**\n\nbody"


def test_an_attachment_is_fetched_from_its_link():
    from correspond.channels._http import Response

    fetched = []

    def http(method, url, **kwargs):
        fetched.append((method, url))
        return Response(200, {}, b"PNG") if url.endswith("/a") else Response(403, {}, b"")

    message = correspond.read(
        f"discord:{CHANNEL}",
        registry={"discord": Discord(backend=FakeBackend(HISTORY), http=http)},
    )[-1]
    (attachment,) = message.attachments
    assert attachment.media_type == "image/png" and attachment.content() == b"PNG"
    assert fetched == [("GET", "https://example.org/a")]
