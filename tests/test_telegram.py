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
    # Each write asks getChat first, for its audience; these are the writes.
    writes = lambda: [c for c in api.calls if c[0] != "getChat"]  # noqa: E731
    assert writes()[0] == (
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
    assert writes()[1] == (
        "editMessageText",
        {"chat_id": -4001, "message_id": 50, "text": "the fix is live (v2)"},
    )
    assert correspond.react("telegram:-4001", "50", "👍", registry=registry).ok
    assert writes()[2] == (
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


# ----------------------------------------------------------------- upload (#8)


def _multipart(headers, body):
    """The fields and files of a multipart/form-data body, parsed with the standard library."""
    from email import policy
    from email.parser import BytesParser

    head = f"Content-Type: {headers['Content-Type']}\r\n\r\n".encode()
    parsed = BytesParser(policy=policy.HTTP).parsebytes(head + body)
    fields, files = {}, {}
    for part in parsed.iter_parts():
        name = part.get_param("name", header="content-disposition")
        filename = part.get_filename()
        payload = part.get_payload(decode=True)
        if filename is None:
            fields[name] = payload.decode()
        else:
            files[name] = (filename, part.get_content_type(), payload)
    return fields, files


class FakeUploadApi:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def __call__(self, method, url, *, headers=None, body=None, timeout=None):
        assert url == f"https://api.telegram.org/bot{TOKEN}/sendDocument"
        self.calls.append(_multipart(headers, body))
        return Response(200, {}, json.dumps({"ok": True, "result": self.result}).encode())


def test_upload_sends_a_document_as_multipart_and_logs_it(token):
    sent = _message(
        77,
        sender=BOT,
        text="",
        document={"file_name": "report.csv", "mime_type": "text/csv"},
        message_thread_id=7,
        is_topic_message=True,
    )
    api, log = FakeUploadApi(sent), {}
    result = correspond.upload(
        "telegram:-4001/7",
        "report.csv",
        b"a,b\n1,2\n",
        media_type="text/csv",
        registry=_registry(api, log),
    )
    assert result.ok and result.operation == "upload" and result.message_id == "77"
    assert result.account.id == "@example_bot"
    [(fields, files)] = api.calls
    assert fields == {"chat_id": "-4001", "message_thread_id": "7"}
    assert files == {"document": ("report.csv", "text/csv", b"a,b\n1,2\n")}
    assert result.plan["action"] == "sendDocument" and result.plan["bytes"] == 8
    assert "data" not in result.plan and TOKEN not in json.dumps(result.plan)
    assert log["-4001/77.json"]["native"]["media"] == ["document"]


def test_an_upload_dry_run_contacts_nothing_and_bad_files_are_refused(token):
    api = FakeUploadApi({})
    registry = _registry(api)
    plan = correspond.upload(
        "telegram:-4001", "a.txt", b"x", dry_run=True, registry=registry
    )
    assert plan.ok and plan.dry_run and api.calls == []
    for name, data in (("", b"x"), ("a\r\nb.txt", b"x"), ("a.txt", b"")):
        refused = correspond.upload("telegram:-4001", name, data, registry=registry)
        assert not refused.ok and refused.error_kind == "validation", name
    too_big = Telegram().capabilities.max_upload_bytes + 1
    refused = correspond.upload(
        "telegram:-4001", "big.bin", b"\0" * too_big, registry=registry
    )
    assert not refused.ok and refused.error_kind == "validation"
    assert api.calls == []


def test_a_quote_in_a_file_name_cannot_break_out_of_its_header(token):
    api = FakeUploadApi(_message(78, sender=BOT, text="", document={}))
    result = correspond.upload(
        "telegram:-4001", 'a"; name="chat_id.txt', b"x", registry=_registry(api)
    )
    assert result.ok
    [(fields, files)] = api.calls
    assert fields == {"chat_id": "-4001"} and list(files) == ["document"]


def test_upload_failures_never_show_the_token(token):
    def failing(method, url, *, headers=None, body=None, timeout=None):
        return Response(
            400,
            {},
            json.dumps(
                {"ok": False, "error_code": 400, "description": f"bad file for {TOKEN}"}
            ).encode(),
        )

    result = correspond.upload(
        "telegram:-4001",
        "a.txt",
        b"x",
        registry={"telegram": Telegram(http=failing, log={})},
    )
    assert not result.ok and result.error_kind == "validation"
    assert TOKEN not in result.error


# ---------------------------------------------------- webhook delivery (#10)

WEBHOOK_SECRET = "example-webhook-secret_1"
SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"


@pytest.fixture
def webhook_secret(monkeypatch):
    monkeypatch.setenv("CORRESPOND_TELEGRAM_WEBHOOK_SECRET", WEBHOOK_SECRET)


def _update(update_id=31, **message):
    return json.dumps(
        {"update_id": update_id, "message": _message(9, **message)}
    ).encode()


def test_a_delivery_is_platform_graded_only_with_the_secret_token(webhook_secret):
    telegram = Telegram(log={})
    body = _update()
    good = telegram.verify({SECRET_HEADER.lower(): WEBHOOK_SECRET}, body)
    assert good.grade.value == "platform"
    assert good.evidence["secret_token"] == "valid" and good.evidence["update_id"] == 31
    wrong = telegram.verify({SECRET_HEADER: "not-the-secret"}, body)
    assert wrong.grade.value == "forged" and wrong.evidence["secret_token"] == "mismatch"
    missing = telegram.verify({}, body)
    assert (
        missing.grade.value == "forged" and missing.evidence["secret_token"] == "missing"
    )
    assert telegram.verify({SECRET_HEADER: "é"}, body).grade.value == "forged"
    assert (
        correspond.verify(
            "telegram",
            {SECRET_HEADER: WEBHOOK_SECRET},
            body,
            registry={"telegram": telegram},
        ).grade.value
        == "platform"
    )


def test_without_a_configured_secret_nothing_is_believed(monkeypatch):
    monkeypatch.delenv("CORRESPOND_TELEGRAM_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("CORRESPOND_CONFIG", "/nonexistent/correspond.toml")
    with pytest.raises(MissingRequirement):
        Telegram(log={}, run=lambda *a, **k: None).verify({SECRET_HEADER: ""}, _update())


def test_receive_logs_a_verified_update_as_polling_would(webhook_secret):
    log = {}
    events = Telegram(log=log).receive(
        {SECRET_HEADER: WEBHOOK_SECRET}, _update(text="hi")
    )
    [event] = events
    assert event.kind == "message.created" and event.delivery_id == "telegram:update-31"
    assert (
        event.message.text == "hi"
        and event.message.authenticity.grade.value == "platform"
    )
    assert "webhook" in event.message.authenticity.evidence["attested_by"]
    assert list(log) == ["-4001/9.json"]
    with pytest.raises(ChannelError) as refused:
        Telegram(log=log).receive(
            {SECRET_HEADER: "nope"}, _update(update_id=32, text="x")
        )
    assert refused.value.kind == "permission" and list(log) == ["-4001/9.json"]
    with pytest.raises(ChannelError) as malformed:
        Telegram(log=log).receive({SECRET_HEADER: WEBHOOK_SECRET}, b"not json")
    assert malformed.value.kind == "validation"


def _post(app, body, headers=None, method="POST"):
    import asyncio

    scope = {
        "type": "http",
        "method": method,
        "path": "/",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
    }
    incoming = [{"type": "http.request", "body": body, "more_body": False}]
    sent = []

    async def receive():
        return incoming.pop(0) if incoming else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    return sent[0]["status"]


def test_the_webhook_app_stores_only_verified_updates(webhook_secret):
    from correspond.channels.telegram import webhook_app

    log = {}
    app = webhook_app(telegram=Telegram(log=log))
    assert _post(app, _update(), {SECRET_HEADER: WEBHOOK_SECRET}) == 200
    assert list(log) == ["-4001/9.json"]
    assert _post(app, _update(update_id=40), {SECRET_HEADER: "nope"}) == 401
    assert _post(app, _update(update_id=41)) == 401
    assert _post(app, b"{", {SECRET_HEADER: WEBHOOK_SECRET}) == 400
    assert _post(app, b"", {SECRET_HEADER: WEBHOOK_SECRET}, method="GET") == 405
    big = b"{" + b" " * (2 * 1024 * 1024) + b"}"
    assert _post(app, big, {SECRET_HEADER: WEBHOOK_SECRET}) == 413
    assert list(log) == ["-4001/9.json"]


def test_the_webhook_app_fails_closed_without_a_secret(monkeypatch):
    from correspond.channels.telegram import webhook_app

    monkeypatch.delenv("CORRESPOND_TELEGRAM_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("CORRESPOND_CONFIG", "/nonexistent/correspond.toml")
    log = {}
    app = webhook_app(telegram=Telegram(log=log, run=lambda *a, **k: None))
    assert _post(app, _update(), {SECRET_HEADER: ""}) == 503 and log == {}


def test_capabilities_and_requirements_name_the_webhook():
    caps = Telegram().capabilities
    assert caps.verify.value == "full" and "webhook" in caps.listen_modes
    assert {g.value for g in caps.grades} == {"platform", "forged"}
    from correspond.registry import info

    keys = {s.key for s in info("telegram").settings}
    assert "webhook_secret" in keys
