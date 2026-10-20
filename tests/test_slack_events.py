"""The Slack Events API receiver and events-mode listen (correspond#67). Offline: no server, no network."""

import asyncio
import hashlib
import hmac
import json

import pytest

import correspond
from correspond.channels.slack import Slack, received_key
from correspond.channels.slack_events import (
    mk_events_app,
    prune_events,
    serve_events,
)

SECRET = "example-signing-secret"
TOKEN = "xoxb-fake"  # fictional
CHANNEL = "C024BE91L"
NOW_S = 1760003640


def _headers(raw, *, secret=SECRET, stamp=NOW_S, retry=None):
    base = f"v0:{stamp}:".encode() + raw
    signature = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    headers = [
        (b"content-type", b"application/json"),
        (b"x-slack-request-timestamp", str(stamp).encode()),
        (b"x-slack-signature", signature.encode()),
    ]
    if retry is not None:
        headers.append((b"x-slack-retry-num", str(retry).encode()))
    return headers


def _deliver(app, payload=None, *, body=None, method="POST", **header_options):
    raw = body if body is not None else json.dumps(payload).encode()
    scope = {
        "type": "http",
        "method": method,
        "path": "/slack/events",
        "headers": _headers(raw, **header_options),
    }
    incoming = [{"type": "http.request", "body": raw, "more_body": False}]
    sent = []

    async def receive():
        return incoming.pop(0) if incoming else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    body_sent = sent[1]["body"] if len(sent) > 1 else b""
    return sent[0]["status"], json.loads(body_sent) if body_sent else None


def _verifier(monkeypatch):
    monkeypatch.setenv("CORRESPOND_SLACK_SIGNING_SECRET", SECRET)
    return Slack(keychain=lambda service: "", clock=lambda: NOW_S).verify


def _callback(event, event_id="Ev0001"):
    return {
        "type": "event_callback",
        "team_id": "T0EXAMPLE",
        "event_id": event_id,
        "event": {"channel": CHANNEL, **event},
    }


def _message(ts, text="hello", **extra):
    return {"type": "message", "user": "U0ADA", "text": text, "ts": ts, **extra}


def test_the_receiver_answers_the_challenge_and_stores_only_signed_messages(monkeypatch):
    store, clock = {}, iter(range(1, 100))
    app = mk_events_app(verify=_verifier(monkeypatch), events=store, clock=lambda: next(clock))
    challenge = {"type": "url_verification", "challenge": "example-challenge"}
    assert _deliver(app, challenge) == (200, {"challenge": "example-challenge"})
    assert _deliver(app, _callback(_message("1760000000.000100"))) == (200, None)
    assert _deliver(app, _callback({"type": "reaction_added"}, "Ev0002"))[0] == 200
    assert _deliver(app, _callback(_message("1760000001.000100"), "Ev0003"), secret="wrong")[0] == 401
    assert _deliver(app, _callback(_message("1760000001.000100"), "Ev0003"), stamp=NOW_S - 600)[0] == 401
    assert list(store) == [received_key(1, "Ev0001")]
    assert store[received_key(1, "Ev0001")]["event"]["text"] == "hello"


def test_the_receiver_refuses_malformed_requests_and_stores_a_retry_once(monkeypatch):
    store = {}
    app = mk_events_app(verify=_verifier(monkeypatch), events=store)
    assert _deliver(app, body=b"not json")[0] == 400
    assert _deliver(app, {}, method="GET")[0] == 405
    small = mk_events_app(verify=_verifier(monkeypatch), events=store, max_body_bytes=10)
    assert _deliver(small, _callback(_message("1760000000.000100")))[0] == 413
    event = _callback(_message("1760000000.000100"))
    assert _deliver(app, event)[0] == 200
    assert _deliver(app, event, retry=1)[0] == 200
    assert len(store) == 1


def test_the_receiver_without_a_secret_refuses_rather_than_raising(monkeypatch):
    monkeypatch.delenv("CORRESPOND_SLACK_SIGNING_SECRET", raising=False)
    app = mk_events_app(verify=Slack(keychain=lambda s: "", run=lambda *a, **k: None).verify, events={})
    assert _deliver(app, _callback(_message("1760000000.000100")))[0] in (401, 503)


def test_prune_and_serve_bind_this_machine():
    store = {received_key(1, "Ev1"): {}, received_key(10**12, "Ev2"): {}}
    assert prune_events(store, now_ns=10**12, retention_ns=10) == 1
    calls = []
    serve_events(app=object(), run=lambda app, **kw: calls.append(kw))
    assert calls[0]["host"] == "127.0.0.1"
    with pytest.raises(correspond.ChannelError):
        serve_events(host="0.0.0.0", allow_remote=False, app=object(), run=calls.append)


def test_events_delivery_lists_created_edited_and_deleted_messages(monkeypatch):
    monkeypatch.setenv("CORRESPOND_SLACK_TOKEN", TOKEN)
    monkeypatch.setenv("CORRESPOND_SLACK_DELIVERY", "events")
    stamp = NOW_S * 10**9 - 1000
    first = _message("1760000000.000100", "first")
    store = {
        received_key(stamp + 1, "Ev1"): {"channel": CHANNEL, "event": first},
        received_key(stamp + 2, "Ev2"): {"channel": "C0OTHER01", "event": _message("1760000001.000100")},
        received_key(stamp + 3, "Ev3"): {
            "channel": CHANNEL,
            "event": {
                "type": "message",
                "subtype": "message_changed",
                "message": {**first, "text": "first, edited", "edited": {"ts": "1760000005.000000"}},
            },
        },
        received_key(stamp + 4, "Ev4"): {
            "channel": CHANNEL,
            "event": {"type": "message", "subtype": "message_deleted", "deleted_ts": "1760000000.000100"},
        },
        received_key(stamp + 5, "Ev5"): {
            "channel": CHANNEL,
            "event": _message("1760000006.000100", "inside a thread", thread_ts="1760000000.000100"),
        },
    }

    def http(*args, **kwargs):  # users.info only; anything else is a test failure
        from correspond.channels._http import Response

        return Response(200, {}, json.dumps({"ok": False, "error": "user_not_found"}).encode())

    registry = {"slack": Slack(http=http, keychain=lambda s: "", clock=lambda: NOW_S, events=store)}
    cursors = {}
    got = list(correspond.listen(f"slack:{CHANNEL}", cursors=cursors, registry=registry))
    assert [e.kind for e in got] == ["message.created", "message.updated", "message.deleted"]
    assert got[1].message.text == "first, edited"
    assert got[2].payload["message_id"] == "1760000000.000100"
    assert cursors[f"slack:{CHANNEL}"] == f"e{stamp + 5}"
    thread = list(
        correspond.listen(
            f"slack:{CHANNEL}/1760000000.000100", cursors={}, registry=registry
        )
    )
    assert "inside a thread" in [e.message.text for e in thread if e.message]
    assert list(correspond.listen(f"slack:{CHANNEL}", cursors=cursors, registry=registry)) == []


def test_an_unknown_delivery_is_refused(monkeypatch):
    monkeypatch.setenv("CORRESPOND_SLACK_TOKEN", TOKEN)
    monkeypatch.setenv("CORRESPOND_SLACK_DELIVERY", "carrier-pigeon")
    registry = {"slack": Slack(keychain=lambda s: "", events={})}
    with pytest.raises(correspond.ChannelError):
        list(correspond.listen(f"slack:{CHANNEL}", cursors={}, registry=registry))
