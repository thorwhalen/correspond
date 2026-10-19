"""Telegram over a fake Bot API: account-wide listening logged locally, reads from the log, writes, and a token that never leaks."""

import json

import pytest

import correspond
from correspond.channels._http import Response
from correspond.channels.telegram import Telegram
from correspond.errors import ChannelError, MissingRequirement, NotSupported

TOKEN = "123456:example-token-for-tests"
PNG = b"\x89PNG\r\n\x1a\nexample"
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


# ------------------------------------------------------------------ webhook delivery (#10)

import asyncio  # noqa: E402

from correspond.channels.telegram import (  # noqa: E402
    SECRET_HEADER,
    WEBHOOK_LOOKBACK_NS,
    WEBHOOK_RETENTION_NS,
    mk_webhook_app,
    serve_webhook,
)

SECRET = "example-webhook-secret_1"
NOW = 1_760_000_000 * 10**9
HOOKED = {"url": "https://example.org/telegram", "pending_update_count": 0}


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


def _deliver(app, update=None, *, secret=SECRET, method="POST", body=None):
    raw = body if body is not None else json.dumps(update).encode()
    headers = [(b"content-type", b"application/json")]
    if secret is not None:
        headers.append((SECRET_HEADER.encode(), secret.encode()))
    scope = {
        "type": "http",
        "method": method,
        "path": "/telegram",
        "headers": headers,
        "client": ("127.0.0.1", 50000),
    }
    incoming = [{"type": "http.request", "body": raw, "more_body": False}]
    sent = []

    async def receive():
        return incoming.pop(0) if incoming else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    return sent[0]["status"]


def test_the_receiver_stores_an_update_only_with_the_secret():
    updates = {}
    app = mk_webhook_app(secret=SECRET, updates=updates, clock=Clock())
    update = {"update_id": 10, "message": _message(1)}
    assert _deliver(app, update, secret=None) == 401
    assert _deliver(app, update, secret="wrong-secret") == 401
    assert _deliver(app, update, secret="é") == 401, "a non-ASCII header never matches"
    assert _deliver(app, update, method="GET") == 405
    assert updates == {}
    assert _deliver(app, update) == 200
    assert _deliver(app, update) == 200, "a repeated delivery is kept, in arrival order"
    assert list(updates) == [f"{NOW:020d}-10.json", f"{NOW + 1:020d}-10.json"]


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[1]",
        b'{"update_id": true}',
        b'{"update_id": -1}',
        b"{}",
        b'{"update_id": 9223372036854775808}',
        b'{"update_id": ' + b"9" * 5000 + b"}",
    ],
)
def test_the_receiver_refuses_what_is_not_an_update(body):
    updates = {}
    assert _deliver(mk_webhook_app(secret=SECRET, updates=updates), body=body) == 400
    assert updates == {}


def test_the_receiver_refuses_a_body_over_its_limit():
    updates = {}
    app = mk_webhook_app(secret=SECRET, updates=updates, max_body_bytes=50)
    assert _deliver(app, {"update_id": 1, "message": _message(1, text="x" * 100)}) == 413
    assert updates == {}


@pytest.mark.parametrize("secret", ["", "has space", "x" * 257, None])
def test_a_secret_telegram_would_refuse_is_refused_up_front(secret):
    with pytest.raises(ValueError):
        mk_webhook_app(secret=secret, updates={})


def _webhook_bot(monkeypatch, updates, clock, **results):
    monkeypatch.setenv("CORRESPOND_TELEGRAM_DELIVERY", "webhook")
    api = FakeBotApi(getWebhookInfo=HOOKED, **results)
    log = {}
    return (
        api,
        log,
        {"telegram": Telegram(http=api, log=log, updates=updates, clock=clock)},
    )


def test_listen_in_webhook_mode_reads_the_receiver_in_arrival_order(token, monkeypatch):
    updates, clock = {}, Clock()
    app = mk_webhook_app(secret=SECRET, updates=updates, clock=clock)
    for update in (
        {"update_id": 12, "message": _message(3)},
        {"update_id": 9, "message": _message(1)},
        {"update_id": 11, "poll": {"id": "p"}},
    ):
        assert _deliver(app, update) == 200
    api, log, registry = _webhook_bot(monkeypatch, updates, clock)
    cursors = {}
    first = list(
        correspond.listen("telegram:", cursors=cursors, limit=1, registry=registry)
    )
    assert [e.delivery_id for e in first] == ["telegram:update-12"]
    assert "webhook" in first[0].message.authenticity.evidence["attested_by"]
    rest = list(correspond.listen("telegram:", cursors=cursors, registry=registry))
    assert [e.delivery_id for e in rest] == ["telegram:update-9"], (
        "11 is passed, not a message"
    )
    assert cursors == {"telegram:": f"w{NOW + 2}"}
    clock.now += 1000
    late = {
        "update_id": 10,
        "edited_message": _message(1, text="edited", edit_date=1_757_581_300),
    }
    assert _deliver(app, late) == 200
    again = list(correspond.listen("telegram:", cursors=cursors, registry=registry))
    assert [(e.kind, e.delivery_id) for e in again] == [
        ("message.updated", "telegram:update-10")
    ], "an id below the ones already passed still arrives"
    assert list(correspond.listen("telegram:", cursors=cursors, registry=registry)) == []
    assert [name for name, _ in api.calls] == ["getWebhookInfo"], "never getUpdates"
    assert set(log) == {"-4001/1.json", "-4001/3.json"}


def test_a_first_webhook_listen_looks_back_a_day_and_old_passed_updates_are_deleted(
    token, monkeypatch
):
    clock = Clock()
    stale = NOW - WEBHOOK_RETENTION_NS - 1
    updates = {
        f"{stale:020d}-1.json": {"update_id": 1, "message": _message(1)},
        f"{NOW - WEBHOOK_LOOKBACK_NS - 1:020d}-2.json": {
            "update_id": 2,
            "message": _message(2),
        },
        f"{NOW - 5:020d}-3.json": {"update_id": 3, "message": _message(3)},
        "not-a-received-key.json": {"update_id": 4},
    }
    _, _, registry = _webhook_bot(monkeypatch, updates, clock)
    events = list(correspond.listen("telegram:", cursors={}, registry=registry))
    assert [e.delivery_id for e in events] == ["telegram:update-3"]
    assert f"{stale:020d}-1.json" not in updates, "passed and a week old"
    assert f"{NOW - WEBHOOK_LOOKBACK_NS - 1:020d}-2.json" in updates, "passed, but recent"


def test_webhook_mode_with_no_webhook_set_says_so(token, monkeypatch):
    _, _, registry = _webhook_bot(monkeypatch, {}, Clock())
    registry["telegram"].http.results["getWebhookInfo"] = {
        "url": "",
        "pending_update_count": 3,
    }
    with pytest.raises(ChannelError, match="no webhook is set") as caught:
        list(correspond.listen("telegram:", cursors={}, registry=registry))
    assert caught.value.kind == "validation"


def test_a_getupdates_cursor_left_from_polling_starts_a_webhook_listen_over(
    token, monkeypatch
):
    updates = {f"{NOW - 5:020d}-3.json": {"update_id": 3, "message": _message(3)}}
    _, _, registry = _webhook_bot(monkeypatch, updates, Clock())
    events = list(
        correspond.listen("telegram:", cursors={"telegram:": "14"}, registry=registry)
    )
    assert [e.delivery_id for e in events] == ["telegram:update-3"]
    with pytest.raises(ChannelError):
        list(
            correspond.listen("telegram:", cursors={"telegram:": "wx"}, registry=registry)
        )


def test_an_unknown_delivery_is_a_validation_error(token, monkeypatch):
    monkeypatch.setenv("CORRESPOND_TELEGRAM_DELIVERY", "push")
    with pytest.raises(ChannelError) as caught:
        list(correspond.listen("telegram:", cursors={}, registry=_registry(FakeBotApi())))
    assert caught.value.kind == "validation"


def test_serve_webhook_binds_this_machine_unless_opted_in(monkeypatch):
    calls = []
    app = mk_webhook_app(secret=SECRET, updates={})
    serve_webhook(app=app, run=lambda *a, **k: calls.append((a, k)))
    ((args, kwargs),) = calls
    assert args == (app,) and kwargs == {
        "host": "127.0.0.1",
        "port": 8081,
        "workers": 1,
        "proxy_headers": False,
    }
    for host in ("0.0.0.0", "203.0.113.7", "example.org"):
        with pytest.raises(ChannelError, match="webhook_allow_remote"):
            serve_webhook(host=host, app=app, run=lambda *a, **k: None)
    serve_webhook(host="::1", app=app, run=lambda *a, **k: None)
    monkeypatch.setenv("CORRESPOND_TELEGRAM_WEBHOOK_ALLOW_REMOTE", "true")
    serve_webhook(host="0.0.0.0", app=app, run=lambda *a, **k: calls.append(k["host"]))
    assert calls[-1] == "0.0.0.0"


def test_verify_grades_a_delivery_by_its_secret_token(monkeypatch):
    monkeypatch.setenv("CORRESPOND_TELEGRAM_WEBHOOK_SECRET", SECRET)
    registry = {"telegram": Telegram(http=FakeBotApi())}
    good = correspond.verify(
        "telegram", {"X-Telegram-Bot-Api-Secret-Token": SECRET}, b"{}", registry=registry
    )
    assert good.grade.value == "platform" and good.evidence["secret_token"] == "valid"
    for headers, reason in (({}, "missing"), ({SECRET_HEADER: "nope"}, "mismatch")):
        bad = correspond.verify("telegram", headers, b"{}", registry=registry)
        assert (bad.grade.value, bad.evidence["secret_token"]) == ("forged", reason)


def test_set_and_delete_webhook_plan_first_and_read_the_state_back(token, monkeypatch):
    monkeypatch.setenv("CORRESPOND_TELEGRAM_WEBHOOK_SECRET", SECRET)
    info = {**HOOKED, "has_custom_certificate": False}
    api = FakeBotApi(setWebhook=True, deleteWebhook=True, getWebhookInfo=info)
    bot = Telegram(http=api, log={})
    planned = bot.set_webhook("https://example.org/telegram", dry_run=True)
    assert planned.dry_run and api.calls == []
    assert SECRET not in json.dumps(planned.plan)
    result = bot.set_webhook("HTTPS://example.org/telegram")
    assert result.ok and result.plan["now"] == HOOKED
    sent = dict(api.calls)["setWebhook"]
    assert sent["secret_token"] == SECRET and sent["max_connections"] == 1
    assert sent["allowed_updates"][0] == "message"
    assert SECRET not in json.dumps(result.plan)
    with pytest.raises(ChannelError):
        bot.set_webhook("http://example.org/telegram")
    api.results["getWebhookInfo"] = {"url": "", "pending_update_count": 2}
    gone = bot.delete_webhook()
    assert gone.plan["now"]["url"] == ""
    assert dict(api.calls)["deleteWebhook"] == {"drop_pending_updates": False}


def test_a_conflict_says_how_to_get_out_of_it(token):
    description = "Conflict: can't use getUpdates method while webhook is active"
    api = FakeBotApi(
        getUpdates=Response(
            409,
            {},
            json.dumps(
                {"ok": False, "error_code": 409, "description": description}
            ).encode(),
        )
    )
    with pytest.raises(ChannelError, match="delete_webhook"):
        list(correspond.listen("telegram:", cursors={}, registry=_registry(api)))


# ------------------------------------------------------------------------ uploads (#8)


class FakeUploadApi:
    """Records multipart calls: the method, the form's text fields, and each file part."""

    def __init__(self, result):
        self.result = result
        self.calls = []

    def __call__(self, method, url, *, headers=None, body=None, timeout=None):
        from email import message_from_bytes, policy as email_policy

        kind = headers["Content-Type"]
        assert kind.startswith("multipart/form-data; boundary=")
        parsed = message_from_bytes(
            b"Content-Type: " + kind.encode() + b"\r\n\r\n" + body,
            policy=email_policy.default,
        )
        parts = {
            p.get_param("name", header="content-disposition"): p
            for p in parsed.iter_parts()
        }
        self.calls.append((url.rsplit("/", 1)[1], parts, body))
        return Response(200, {}, json.dumps({"ok": True, "result": self.result}).encode())


def _sent(message_id=77, chat=-4001, **extra):
    return {**_message(message_id, chat=chat, sender=BOT, text=""), **extra}


def test_an_image_goes_as_a_photo_and_anything_else_as_a_document(token):
    api = FakeUploadApi(_sent())
    log = {}
    registry = {"telegram": Telegram(http=api, log=log)}
    photo = correspond.upload(
        "telegram:-4001/7", "screen.png", PNG, media_type="image/png", registry=registry
    )
    report = correspond.upload(
        "telegram:-4001",
        "Rapport été.pdf",
        b"%PDF-1.7",
        media_type="application/pdf",
        registry=registry,
    )
    assert photo.ok and photo.operation == "upload" and photo.message_id == "77"
    (method, parts, _), (method2, parts2, body2) = api.calls
    assert method == "sendPhoto" and parts["photo"].get_content() == PNG
    assert parts["chat_id"].get_content().strip() == "-4001"
    assert parts["message_thread_id"].get_content().strip() == "7"
    assert method2 == "sendDocument" and parts2["document"].get_content() == b"%PDF-1.7"
    assert 'filename="Rapport été.pdf"'.encode() in body2
    assert TOKEN.encode() not in body2
    assert report.plan["bytes"] == 8 and len(report.plan["sha256"]) == 64
    assert "-4001/77.json" in log, "what the bot sent is logged, as for send"


def test_a_large_image_goes_as_a_document(token, monkeypatch):
    from correspond.channels import telegram as module

    monkeypatch.setattr(module, "MAX_PHOTO_BYTES", 4)
    api = FakeUploadApi(_sent())
    correspond.upload(
        "telegram:-4001",
        "big.png",
        PNG,
        media_type="image/png",
        registry={"telegram": Telegram(http=api, log={})},
    )
    assert api.calls[0][0] == "sendDocument"


def test_an_upload_dry_run_calls_nothing_and_a_bad_name_is_refused(token):
    api = FakeUploadApi(_sent())
    registry = {"telegram": Telegram(http=api, log={})}
    planned = Telegram(http=api, log={}).upload(
        Telegram().parse_ref("-4001"),
        "a.txt",
        b"x",
        media_type="text/plain",
        dry_run=True,
    )
    assert (
        planned.dry_run and planned.plan["action"] == "sendDocument" and api.calls == []
    )
    for bad in ("", "../a.txt", "a/b.txt", "a\nb.txt"):
        result = correspond.upload("telegram:-4001", bad, b"x", registry=registry)
        assert (result.ok, result.error_kind) == (False, "validation")
    too_big = correspond.upload(
        "telegram:-4001", "a.bin", b"x" * 50_000_001, registry=registry
    )
    assert (too_big.ok, too_big.error_kind) == (False, "validation")
    assert api.calls == []


def test_a_media_type_that_could_inject_a_header_or_a_name_with_a_bidi_override_is_refused(
    token,
):
    api = FakeUploadApi(_sent())
    registry = {"telegram": Telegram(http=api, log={})}
    for name, kind in (
        ("a.txt", "text/plain\r\nX-Evil: 1"),
        ("a.txt", "text/plain; charset=utf-8"),
        ("invoice‮fdp.exe", "application/octet-stream"),
    ):
        result = correspond.upload(
            "telegram:-4001", name, b"x", media_type=kind, registry=registry
        )
        assert (result.ok, result.error_kind) == (False, "validation"), (name, kind)
    assert api.calls == []


def test_a_photo_telegram_refuses_is_sent_again_as_a_document(token):
    class Refusing(FakeUploadApi):
        def __call__(self, method, url, **kwargs):
            if url.endswith("/sendPhoto"):
                self.calls.append(("sendPhoto", {}, b""))
                refusal = {
                    "ok": False,
                    "error_code": 400,
                    "description": "Bad Request: PHOTO_INVALID_DIMENSIONS",
                }
                return Response(400, {}, json.dumps(refusal).encode())
            return super().__call__(method, url, **kwargs)

    api = Refusing(_sent())
    result = correspond.upload(
        "telegram:-4001",
        "wide.png",
        PNG,
        media_type="image/png",
        registry={"telegram": Telegram(http=api, log={})},
    )
    assert result.ok and [c[0] for c in api.calls] == ["sendPhoto", "sendDocument"]
    assert (
        result.plan["action"] == "sendDocument"
        and "DIMENSIONS" in result.plan["photo_refused"]
    )


def test_before_send_sees_an_uploads_metadata_and_a_dry_run_calls_nothing(token):
    seen = []

    def check(ref, draft, audience, **context):
        seen.append((context["operation"], draft.text, dict(draft.native)))

    api = FakeUploadApi(_sent())
    result = correspond.upload(
        "telegram:-4001",
        "a.txt",
        b"hello",
        media_type="text/plain",
        dry_run=True,
        before_send=check,
        registry={"telegram": Telegram(http=api, log={})},
    )
    assert result.ok and result.dry_run and api.calls == []
    ((operation, text, native),) = seen
    assert (operation, text) == ("upload", "a.txt")
    assert (
        native["upload"]["bytes"] == 5 and native["upload"]["media_type"] == "text/plain"
    )


# ------------------------------------------- the webhook as tools, its store, its command (#58)


def test_the_webhook_tools_plan_first_and_reach_the_bot(token, monkeypatch):
    from correspond import registry as registry_module
    from correspond import tools

    monkeypatch.setenv("CORRESPOND_TELEGRAM_WEBHOOK_SECRET", SECRET)
    api = FakeBotApi(setWebhook=True, deleteWebhook=True, getWebhookInfo=HOOKED)
    monkeypatch.setattr(
        registry_module, "channels", lambda: {"telegram": Telegram(http=api, log={})}
    )
    planned = tools.set_webhook("telegram", "https://example.org/telegram", dry_run=True)
    assert planned["ok"] and planned["dry_run"] and api.calls == []
    assert SECRET not in json.dumps(planned)
    done = tools.set_webhook("telegram", "https://example.org/telegram")
    assert done["ok"] and [name for name, _ in api.calls] == [
        "setWebhook",
        "getWebhookInfo",
    ]
    info = tools.webhook_info("telegram:")
    assert (
        info["ok"]
        and info["webhook"]["host"] == "example.org"
        and info["webhook"]["webhook_set"]
    )
    assert "/telegram" not in json.dumps(info), (
        "a URL's path may hold a token: only the host is shown"
    )
    gone = tools.delete_webhook("telegram", drop_pending_updates=True)
    assert gone["ok"] and dict(api.calls)["deleteWebhook"] == {
        "drop_pending_updates": True
    }
    refused = tools.set_webhook("telegram", "http://example.org/telegram")
    assert (refused["ok"], refused["error_kind"]) == (False, "validation")


def test_a_channel_without_a_webhook_says_so(monkeypatch):
    from correspond import registry as registry_module
    from correspond import tools
    from correspond.testing import FakeChannel

    monkeypatch.setattr(registry_module, "channels", lambda: {"fake": FakeChannel()})
    for result in (
        tools.webhook_info("fake"),
        tools.set_webhook("fake", "https://example.org/x", dry_run=True),
    ):
        assert (result["ok"], result["error_kind"]) == (False, "not_supported")


def test_a_configured_store_is_where_receiver_and_listen_meet(monkeypatch, tmp_path):
    import sys
    import types

    from correspond.channels.telegram import webhook_store

    shared = {}
    module = types.ModuleType("example_webhook_store")
    module.make = lambda: shared
    monkeypatch.setitem(sys.modules, "example_webhook_store", module)
    monkeypatch.setenv("CORRESPOND_TELEGRAM_WEBHOOK_STORE", "example_webhook_store:make")
    assert webhook_store() is shared and Telegram(http=FakeBotApi()).updates is shared
    for bad, words in (
        ("no-colon", "module:attr"),
        ("example_webhook_store:missing", "could not be loaded"),
    ):
        monkeypatch.setenv("CORRESPOND_TELEGRAM_WEBHOOK_STORE", bad)
        with pytest.raises(ChannelError, match=words):
            webhook_store()
    module.not_a_store = 42
    monkeypatch.setenv(
        "CORRESPOND_TELEGRAM_WEBHOOK_STORE", "example_webhook_store:not_a_store"
    )
    with pytest.raises(ChannelError, match="not a mutable mapping"):
        webhook_store()


def test_the_command_runs_the_receiver_on_this_machine(monkeypatch, capsys):
    from correspond.channels import telegram as module

    calls = []
    monkeypatch.setattr(module, "serve_webhook", lambda **kwargs: calls.append(kwargs))
    module.serve_main(["--port", "9000"])
    assert calls == [{"host": "127.0.0.1", "port": 9000, "allow_remote": None}]

    def refuse(**kwargs):
        raise ChannelError("binds to this machine only", kind="validation")

    monkeypatch.setattr(module, "serve_webhook", refuse)
    with pytest.raises(SystemExit) as stopped:
        module.serve_main(["--host", "0.0.0.0"])
    assert stopped.value.code == 1 and "this machine only" in capsys.readouterr().err
    monkeypatch.setattr(module, "serve_webhook", lambda **kwargs: calls.append(kwargs))
    module.serve_main(["--no-allow-remote"])
    assert calls[-1]["allow_remote"] is False


def test_set_and_delete_webhook_pass_the_operators_check(token, monkeypatch):
    from correspond.errors import Refused

    monkeypatch.setenv("CORRESPOND_TELEGRAM_WEBHOOK_SECRET", SECRET)
    api = FakeBotApi(setWebhook=True, deleteWebhook=True, getWebhookInfo=HOOKED)
    registry = {"telegram": Telegram(http=api, log={})}
    seen = []

    def check(ref, draft, audience, *, operation, **context):
        seen.append((operation, draft.text))
        raise Refused("no redirects")

    redirected = correspond.ops.set_webhook(
        "telegram",
        "https://attacker.example.org/hook",
        before_send=check,
        registry=registry,
    )
    deleted = correspond.ops.delete_webhook(
        "telegram", before_send=check, registry=registry
    )
    assert (redirected.ok, redirected.error_kind, deleted.ok) == (False, "refused", False)
    assert seen == [
        ("set_webhook", "https://attacker.example.org/hook"),
        ("delete_webhook", "delete the webhook"),
    ]
    assert [name for name, _ in api.calls] == [], "nothing reached Telegram"


def test_the_receiver_from_the_settings_writes_where_listen_reads(monkeypatch):
    import sys
    import types

    from correspond.channels.telegram import webhook_app_from_env

    shared = {}
    module = types.ModuleType("example_webhook_store_2")
    module.make = lambda: shared
    monkeypatch.setitem(sys.modules, "example_webhook_store_2", module)
    monkeypatch.setenv(
        "CORRESPOND_TELEGRAM_WEBHOOK_STORE", "example_webhook_store_2:make"
    )
    monkeypatch.setenv("CORRESPOND_TELEGRAM_WEBHOOK_SECRET", SECRET)
    assert (
        _deliver(webhook_app_from_env(), {"update_id": 5, "message": _message(1)}) == 200
    )
    assert len(shared) == 1 and Telegram(http=FakeBotApi()).updates is shared
