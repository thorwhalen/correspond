"""The GitHub discussion_comment webhook receiver and webhook-mode listen (correspond#82). Offline: no server, no network."""

import asyncio
import json

import pytest

import correspond
from correspond.channels.github import GitHub, signature
from correspond.channels.github_webhook import (
    DELIVERY_HEADER,
    EVENT_HEADER,
    SIGNATURE_HEADER,
    mk_webhook_app,
    serve_webhook,
)
from correspond.errors import ChannelError

from test_github import (
    REPO,
    WHOAMI,
    ScriptedGh,
    _activity,
    _numbered_discussion,
    _post,
)

SECRET = "example-webhook-secret"
NOW = 1_789_120_800 * 10**9  # 2026-09-11T10:00:00Z


def _payload(number=7, repository=REPO):
    return {
        "action": "created",
        "repository": {"full_name": repository},
        "discussion": {"number": number, "title": "Example"},
        "comment": {"id": 702, "body": "a reply"},
    }


def _deliver(
    app,
    payload=None,
    *,
    secret=SECRET,
    event="discussion_comment",
    delivery="d-1",
    body=None,
    method="POST",
):
    raw = body if body is not None else json.dumps(payload).encode()
    headers = [(b"content-type", b"application/json")]
    if secret is not None:
        headers.append((SIGNATURE_HEADER.encode(), signature(secret, raw).encode()))
    if event is not None:
        headers.append((EVENT_HEADER.encode(), event.encode()))
    if delivery is not None:
        headers.append((DELIVERY_HEADER.encode(), delivery.encode()))
    scope = {"type": "http", "method": method, "path": "/github", "headers": headers}
    incoming = [{"type": "http.request", "body": raw, "more_body": False}]
    sent = []

    async def receive():
        return incoming.pop(0) if incoming else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    return sent[0]["status"]


def test_the_receiver_stores_only_signed_discussion_comments_and_no_text():
    store = {}
    app = mk_webhook_app(secret=SECRET, deliveries=store, clock=lambda: NOW)
    assert _deliver(app, _payload(), secret="wrong-secret") == 401
    assert _deliver(app, _payload(), secret=None) == 401
    assert store == {}
    assert _deliver(app, {"zen": "hi"}, event="ping") == 202
    assert _deliver(app, _payload(), event="issues") == 202
    assert store == {}
    assert _deliver(app, _payload()) == 200
    ((key, record),) = store.items()
    assert key == "d-1.json"
    assert record == {
        "event": "discussion_comment",
        "action": "created",
        "repository": REPO,
        "discussion": 7,
        "comment": 702,
        "received_ns": NOW,
    }
    assert "a reply" not in json.dumps(record)
    assert _deliver(app, _payload()) == 200  # a replay overwrites, never grows
    assert len(store) == 1


def test_the_receiver_refuses_malformed_deliveries():
    store = {}
    app = mk_webhook_app(secret=SECRET, deliveries=store, clock=lambda: NOW)
    assert _deliver(app, body=b"not json") == 400
    assert _deliver(app, _payload(), delivery=None) == 400
    assert _deliver(app, _payload(), delivery="../../etc") == 400
    assert _deliver(app, _payload(number="7")) == 400
    assert _deliver(app, _payload(repository="not a repo")) == 400
    assert _deliver(app, _payload(), method="GET") == 405
    small = mk_webhook_app(secret=SECRET, deliveries=store, max_body_bytes=10)
    assert _deliver(small, _payload()) == 413
    assert store == {}
    with pytest.raises(ValueError):
        mk_webhook_app(secret="", deliveries=store)


def test_serve_webhook_binds_this_machine_unless_allowed():
    calls = []
    serve_webhook(app=object(), run=lambda app, **kw: calls.append(kw))
    assert calls[0]["host"] == "127.0.0.1" and calls[0]["port"] == 8082
    with pytest.raises(ChannelError, match="binds to this machine only"):
        serve_webhook(host="0.0.0.0", allow_remote=False, app=object(), run=calls.append)
    serve_webhook(
        host="0.0.0.0",
        allow_remote=True,
        app=object(),
        run=lambda app, **kw: calls.append(kw),
    )
    assert calls[-1]["host"] == "0.0.0.0"


def _listen_setup(deliveries, since="2026-09-11T08:00:00Z"):
    date = {"Date": "Fri, 11 Sep 2026 12:00:00 GMT"}
    discussion = _numbered_discussion(
        7,
        "2026-08-01T00:00:00Z",
        [
            _post(
                701,
                created="2026-08-02T00:00:00Z",
                replies=[_post(702, created="2026-09-11T09:00:00Z")],
            )
        ],
    )

    def graphql(payload):
        if "discussions(" in payload["query"]:
            # 7 neither moved nor is among the recent ones: polling alone misses it
            return 200, _activity((9, "2026-08-05T00:00:00Z"))
        return 200, discussion

    gh = ScriptedGh(
        WHOAMI,
        ("GET", rf"repos/{REPO}/issues/comments\?.*", (200, [], date)),
        ("GET", rf"repos/{REPO}/issues\?.*", (200, [])),
        ("GET", rf"repos/{REPO}/pulls\?state=all&.*", (200, [])),
        ("GET", rf"repos/{REPO}/pulls/comments\?.*", (200, [])),
        ("POST", "graphql", graphql),
    )
    github = GitHub(
        run=gh, recent_discussions=0, deliveries=deliveries, clock=lambda: NOW
    )
    cursors = {f"github:{REPO}": since}
    return lambda: [
        e.message.id
        for e in correspond.listen(
            f"github:{REPO}", cursors=cursors, registry={"github": github}
        )
    ]


def test_webhook_delivery_makes_listen_read_the_discussion_it_named(monkeypatch):
    received = NOW - 30 * 10**9
    old = NOW - 8 * 24 * 3600 * 10**9
    deliveries = {
        "d-1.json": {"repository": REPO, "discussion": 7, "received_ns": received},
        "d-2.json": {
            "repository": "example/other",
            "discussion": 8,
            "received_ns": received,
        },
        "d-0.json": {"repository": REPO, "discussion": 7, "received_ns": old},
    }
    monkeypatch.setenv("CORRESPOND_GITHUB_DELIVERY", "webhook")
    listen = _listen_setup(deliveries)
    assert listen() == ["discussioncomment-702"]
    assert listen() == []
    assert "d-0.json" not in deliveries  # past the retention: deleted


def test_poll_delivery_ignores_the_receiver(monkeypatch):
    monkeypatch.delenv("CORRESPOND_GITHUB_DELIVERY", raising=False)
    received = NOW - 30 * 10**9
    listen = _listen_setup(
        {"d-1.json": {"repository": REPO, "discussion": 7, "received_ns": received}}
    )
    assert listen() == []


def test_an_unknown_delivery_is_refused(monkeypatch):
    monkeypatch.setenv("CORRESPOND_GITHUB_DELIVERY", "push")
    with pytest.raises(ChannelError, match="github delivery is one of"):
        GitHub(deliveries={})._delivery()


def test_a_delivery_arriving_after_the_cursor_passed_its_comment_still_delivers_it(
    monkeypatch,
):
    """The comment (09:00) predates the cursor (09:02); its delivery came at 09:03."""
    monkeypatch.setenv("CORRESPOND_GITHUB_DELIVERY", "webhook")
    arrived = NOW - 57 * 60 * 10**9  # 09:03
    listen = _listen_setup(
        {"d-1.json": {"repository": REPO, "discussion": 7, "received_ns": arrived}},
        since="2026-09-11T09:02:00Z",
    )
    assert listen() == ["discussioncomment-702"]
