"""Telegram over a fake Bot API: account-wide listening logged locally, reads from the log, writes, and a token that never leaks."""

import json

import pytest

import correspond
from correspond.channels._http import Response
from correspond.channels.telegram import Telegram
from correspond.errors import ChannelError, MissingRequirement, NotSupported

TOKEN = "123456:example-token-for-tests"
BOT = {"id": 900, "is_bot": True, "first_name": "Example", "username": "example_bot"}
ADA = {"id": 42, "is_bot": False, "first_name": "Ada", "username": "ada"}


def _message(
    message_id, *, chat=-4001, text="hello", sender=ADA, date=1_757_581_200, **extra
):
    return {
        "message_id": message_id,
        "chat": {"id": chat, "type": "supergroup", "title": "Example group"},
        "from": sender,
        "date": date,
        "text": text,
        **extra,
    }


class FakeBotApi:
    def __init__(self, **results):
        self.results = results
        self.calls = []

    def __call__(self, method, url, *, headers=None, body=None, timeout=None):
        name = url.rsplit("/", 1)[1]
        assert url.startswith(f"https://api.telegram.org/bot{TOKEN}/")
        self.calls.append((name, json.loads(body)))
        result = self.results[name]
        if isinstance(result, Exception):
            raise result
        if isinstance(result, Response):
            return result
        return Response(
            200,
            {},
            json.dumps(
                {
                    "ok": True,
                    "result": result(json.loads(body)) if callable(result) else result,
                }
            ).encode(),
        )


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)


def _registry(api, log=None):
    return {"telegram": Telegram(http=api, log={} if log is None else log)}


def test_listening_logs_every_update_and_moves_the_offset(token):
    updates = [
        {"update_id": 10, "message": _message(1)},
        {
            "update_id": 11,
            "edited_message": _message(1, text="hello, edited", edit_date=1_757_581_300),
        },
        {
            "update_id": 12,
            "channel_post": _message(5, chat=-4002, sender={}, text="a post"),
        },
        {"update_id": 13, "poll": {"id": "p"}},
    ]
    api = FakeBotApi(getUpdates=updates)
    log, cursors = {}, {}
    events = list(
        correspond.listen("telegram:", cursors=cursors, registry=_registry(api, log))
    )
    assert [(e.kind, e.cursor) for e in events] == [
        ("message.created", "11"),
        ("message.updated", "12"),
        ("message.created", "13"),
    ]
    assert cursors == {"telegram:": "14"}, "the unknown update 13 is confirmed too"
    assert {"-4001/1.json", "-4002/5.json"} == set(log)
    assert (
        events[1].message.edited_at is not None
        and events[0].message.authenticity.grade.value == "platform"
    )
    api.results["getUpdates"] = []
    list(correspond.listen("telegram:", cursors=cursors, registry=_registry(api, log)))
    assert api.calls[-1][1]["offset"] == 14


def test_listening_to_one_chat_is_refused_with_the_way_that_works(token):
    with pytest.raises(NotSupported, match="telegram: and route"):
        correspond.listen("telegram:-4001", cursors={}, registry=_registry(FakeBotApi()))


def test_read_returns_what_the_log_holds_for_a_chat_or_a_topic(token):
    topic_start = _message(7, text="topic created")
    in_topic = _message(
        8,
        text="in the topic",
        message_thread_id=7,
        is_topic_message=True,
        reply_to_message=topic_start,
        date=1_757_581_500,
    )
    answer = _message(
        9,
        text="an answer",
        message_thread_id=7,
        is_topic_message=True,
        reply_to_message=in_topic,
        date=1_757_581_600,
    )
    api = FakeBotApi(
        getUpdates=[
            {"update_id": i, "message": m}
            for i, m in enumerate((_message(1), in_topic, answer), start=1)
        ]
    )
    log = {}
    list(correspond.listen("telegram:", cursors={}, registry=_registry(api, log)))
    chat = correspond.read("telegram:-4001", registry=_registry(api, log))
    assert [m.id for m in chat] == ["1", "8", "9"]
    topic = correspond.read("telegram:-4001/7", registry=_registry(api, log))
    assert [m.id for m in topic] == ["8", "9"]
    assert topic[0].reply_to is None, (
        "a message in a topic does not reply to the topic's first message"
    )
    assert (
        topic[1].reply_to == "8" and topic[0].conversation.encoded == "telegram:-4001/7"
    )


def test_send_edit_and_react_payloads_and_the_log_of_what_was_sent(token):
    api = FakeBotApi(
        sendMessage=lambda params: _message(
            50,
            text=params["text"],
            sender=BOT,
            message_thread_id=params.get("message_thread_id"),
            is_topic_message=True,
        ),
        editMessageText=lambda params: _message(50, text=params["text"], sender=BOT),
        setMessageReaction=True,
    )
    log = {}
    registry = _registry(api, log)
    sent = correspond.send(
        "telegram:-4001/7",
        "the fix is live",
        title="Update",
        reply_to="8",
        registry=registry,
    )
    assert (
        sent.ok
        and sent.message_id == "50"
        and sent.account.id == "@example_bot"
        and sent.account.acts_as == "bot"
    )
    assert api.calls[0] == (
        "sendMessage",
        {
            "chat_id": -4001,
            "message_thread_id": 7,
            "text": "Update\n\nthe fix is live",
            "reply_parameters": {"message_id": 8},
        },
    )
    assert "-4001/50.json" in log and log["-4001/50.json"]["author"]["is_self"]
    assert correspond.edit(
        "telegram:-4001", "50", "the fix is live (v2)", registry=registry
    ).ok
    assert api.calls[1] == (
        "editMessageText",
        {"chat_id": -4001, "message_id": 50, "text": "the fix is live (v2)"},
    )
    assert correspond.react("telegram:-4001", "50", "👍", registry=registry).ok
    assert api.calls[2] == (
        "setMessageReaction",
        {
            "chat_id": -4001,
            "message_id": 50,
            "reaction": [{"type": "emoji", "emoji": "👍"}],
        },
    )


def test_dry_runs_call_nothing_and_need_no_token():
    api = FakeBotApi()
    registry = _registry(api)
    assert correspond.send("telegram:-4001", "hi", dry_run=True, registry=registry).ok
    assert correspond.edit(
        "telegram:-4001", "5", "hi", dry_run=True, registry=registry
    ).ok
    assert correspond.react(
        "telegram:-4001", "5", "👍", dry_run=True, registry=registry
    ).ok
    assert api.calls == []


def test_errors_carry_retry_after_and_never_the_token(token):
    def failing(code, description, **parameters):
        return Response(
            code,
            {},
            json.dumps(
                {
                    "ok": False,
                    "error_code": code,
                    "description": description,
                    "parameters": parameters,
                }
            ).encode(),
        )

    cases = [
        (
            failing(429, "Too Many Requests: retry after 5", retry_after=5),
            "rate_limited",
            5.0,
        ),
        (failing(403, "Forbidden: bot was blocked by the user"), "permission", None),
        (failing(400, "Bad Request: chat not found"), "not_found", None),
        (failing(401, f"Unauthorized for bot{TOKEN}"), "auth", None),
        (
            ChannelError(
                f"could not reach api.telegram.org/bot{TOKEN}: timed out",
                kind="network",
                retryable=True,
            ),
            "network",
            None,
        ),
    ]
    for response, kind, retry_after in cases:
        result = correspond.send(
            "telegram:-4001", "hi", registry=_registry(FakeBotApi(sendMessage=response))
        )
        assert (result.ok, result.error_kind, result.retry_after) == (
            False,
            kind,
            retry_after,
        )
        assert TOKEN not in json.dumps(result.to_dict())


def test_limits_and_malformed_ids_are_validation_failures(token):
    registry = _registry(FakeBotApi())
    assert (
        correspond.send("telegram:-4001", "x" * 4097, registry=registry).error_kind
        == "validation"
    )
    assert (
        correspond.send(
            "telegram:-4001", "x" * 4090, title="A long title", registry=registry
        ).error_kind
        == "validation"
    )
    assert (
        correspond.send(
            "telegram:-4001", "hi", reply_to="m1", registry=registry
        ).error_kind
        == "validation"
    )
    assert (
        correspond.edit("telegram:-4001", "m1", "hi", registry=registry).error_kind
        == "validation"
    )
    assert (
        correspond.send("telegram:", "hi", registry=registry).error_kind == "validation"
    )
    for bad in ("0", "chat", "-4001/", "-4001/x", "@ab"):
        with pytest.raises(correspond.InvalidRef):
            Telegram().parse_ref(bad)


def test_a_missing_token_says_where_to_get_one():
    with pytest.raises(MissingRequirement) as caught:
        list(correspond.listen("telegram:", cursors={}, registry=_registry(FakeBotApi())))
    assert (
        caught.value.kind == "auth"
        and "TELEGRAM_BOT_TOKEN" in str(caught.value)
        and "BotFather" in str(caught.value)
    )
    result = correspond.send("telegram:-4001", "hi", registry=_registry(FakeBotApi()))
    assert result.error_kind == "auth"


def test_the_native_fields_messages_carry_are_the_ones_capabilities_declare(token):
    api = FakeBotApi(
        getUpdates=[
            {
                "update_id": 1,
                "message": _message(1, message_thread_id=3, is_topic_message=True),
            }
        ]
    )
    declared = set(Telegram().capabilities.native_fields)
    for event in correspond.listen("telegram:", cursors={}, registry=_registry(api)):
        assert set(event.message.native) <= declared
