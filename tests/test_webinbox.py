"""The web inbox: the ASGI collector's identity, origin, limit and storage rules, and the channel that reads what it stored."""

import asyncio
import base64
import hashlib
import json

import pytest

import correspond
from correspond.channels.webinbox import (
    Site,
    WebInbox,
    app_from_env,
    identity_payload,
    mk_app,
    sign_identity,
)
from correspond.errors import MissingRequirement, NotSupported

SITE = "example-site"
ORIGIN = "https://app.example.org"
SECRET = "an-example-shared-secret"
NOW = 1_757_592_000.0
PNG = b"\x89PNG\r\n\x1a\nexample"


def call(
    app,
    *,
    method="POST",
    path=f"/{SITE}/reports",
    payload=None,
    body=None,
    headers=None,
    client=("192.0.2.10", 5000),
):
    """Drive the ASGI app once; return (status, headers, JSON body)."""
    raw = (
        body
        if body is not None
        else (json.dumps(payload).encode() if payload is not None else b"")
    )
    base = {"origin": ORIGIN, "content-type": "application/json"}
    merged = {k: v for k, v in {**base, **(headers or {})}.items() if v is not None}
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": [(k.encode(), v.encode()) for k, v in merged.items()],
        "client": client,
    }
    incoming = [{"type": "http.request", "body": raw, "more_body": False}]
    sent = []

    async def receive():
        return incoming.pop(0) if incoming else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    start = sent[0]
    content = b"".join(m.get("body", b"") for m in sent[1:])
    return (
        start["status"],
        {k.decode(): v.decode() for k, v in start["headers"]},
        json.loads(content) if content else None,
    )


@pytest.fixture
def stores():
    return {}, {}


def _app(stores, **kwargs):
    site = kwargs.pop("site", Site(name=SITE, origins=(ORIGIN,), secret=SECRET))
    return mk_app(
        [site],
        store=stores[0],
        blobs=stores[1],
        clock=kwargs.pop("clock", lambda: NOW),
        **kwargs,
    )


def _inbox(stores):
    return {"webinbox": WebInbox(store=stores[0], blobs=stores[1])}


def test_an_unsigned_report_is_stored_as_claimed_and_read_back(stores):
    status, headers, reply = call(
        _app(stores),
        payload={
            "text": "The export drops the last row.",
            "name": "Ada",
            "email": "ada@example.org",
            "page": f"{ORIGIN}/export",
        },
    )
    assert status == 201 and headers["access-control-allow-origin"] == ORIGIN
    [key] = stores[0]
    assert key == f"{SITE}/{reply['id']}.json"
    [message] = correspond.read(f"webinbox:{SITE}", registry=_inbox(stores))
    assert (
        message.authenticity.grade.value == "claimed"
        and message.author.display_name == "Ada"
    )
    assert message.native["contact"] == {
        "name": "Ada",
        "email": "ada@example.org",
        "signed": False,
    }
    assert message.native["page"] == f"{ORIGIN}/export"


def test_a_signed_identity_makes_the_report_bound(stores):
    identity = sign_identity(SECRET, SITE, "u-42", name="Ada", issued_at=NOW - 60)
    status, _, _ = call(_app(stores), payload={"text": "hi", "identity": identity})
    assert status == 201
    [message] = correspond.read(f"webinbox:{SITE}", registry=_inbox(stores))
    assert (
        message.authenticity.grade.value == "bound" and message.author.native_id == "u-42"
    )
    assert message.authenticity.evidence["method"] == "hmac-sha256"


@pytest.mark.parametrize(
    "identity, reason",
    [
        (
            {
                **sign_identity(SECRET, SITE, "u-42", name="Ada", issued_at=NOW),
                "name": "Grace",
            },
            "does not match",
        ),
        (
            sign_identity("a-different-secret", SITE, "u-42", issued_at=NOW),
            "does not match",
        ),
        (sign_identity(SECRET, "another-site", "u-42", issued_at=NOW), "does not match"),
        (sign_identity(SECRET, SITE, "u-42", issued_at=NOW - 2 * 86_400), "expired"),
        (sign_identity(SECRET, SITE, "u-42", issued_at=NOW + 3_600), "future"),
        ({"user": "u-42", "issued_at": NOW}, "malformed"),
        ({"user": "u-42", "issued_at": "soon", "sig": "00"}, "malformed"),
        ({"user": "u-42", "issued_at": NOW, "sig": "é"}, "does not match"),
    ],
)
def test_a_bad_signature_is_refused_and_nothing_is_stored(stores, identity, reason):
    status, _, reply = call(_app(stores), payload={"text": "hi", "identity": identity})
    assert status == 401 and reason in reply["error"] and stores == ({}, {})


def test_an_inbox_without_a_secret_refuses_signed_identities(stores):
    app = _app(stores, site=Site(name=SITE, origins=(ORIGIN,)))
    status, _, reply = call(
        app,
        payload={
            "text": "hi",
            "identity": sign_identity(SECRET, SITE, "u-42", issued_at=NOW),
        },
    )
    assert status == 401 and "no secret" in reply["error"]
    assert call(app, payload={"text": "hi"})[0] == 201


def test_origins_and_preflight(stores):
    app = _app(stores)
    assert (
        call(
            app,
            payload={"text": "hi"},
            headers={"origin": "https://elsewhere.example.org"},
        )[0]
        == 403
    )
    assert call(app, payload={"text": "hi"}, headers={"origin": None})[0] == 403
    status, headers, _ = call(app, method="OPTIONS")
    assert status == 204 and "POST" in headers["access-control-allow-methods"]
    assert (
        call(app, method="OPTIONS", headers={"origin": "https://elsewhere.example.org"})[
            0
        ]
        == 403
    )
    server_to_server = _app(stores, site=Site(name=SITE, allow_no_origin=True))
    assert (
        call(server_to_server, payload={"text": "hi"}, headers={"origin": None})[0] == 201
    )
    assert len(stores[0]) == 1


def test_limits_on_rate_size_type_and_shape(stores):
    app = _app(stores, burst=2, rate_per_minute=1, max_body_bytes=2_000)
    assert [call(app, payload={"text": "hi"})[0] for _ in range(2)] == [201, 201]
    status, headers, _ = call(app, payload={"text": "hi"})
    assert status == 429 and int(headers["retry-after"]) >= 1
    assert call(app, payload={"text": "hi"}, client=("192.0.2.11", 5000))[0] == 201, (
        "the limit is per client"
    )

    roomy = _app(stores, burst=100, max_body_bytes=2_000)
    cases = [
        (dict(payload={"text": "x" * 3_000}), 413),
        (dict(payload={"text": "x" * 3_000}, headers={"content-length": None}), 413),
        (dict(payload={"text": "hi"}, headers={"content-type": "text/plain"}), 415),
        (dict(body=b"{not json"), 400),
        (dict(body=b"[1, 2]"), 400),
        (dict(payload={"name": "no text"}), 400),
        (
            dict(
                payload={
                    "text": "hi",
                    "attachments": [{"media_type": "application/x-sh", "data": "aGk="}],
                }
            ),
            415,
        ),
        (
            dict(
                payload={
                    "text": "hi",
                    "attachments": [{"media_type": "image/png", "data": "not base64!"}],
                }
            ),
            400,
        ),
        (dict(payload={"text": "hi", "attachments": "nope"}), 400),
        (dict(payload={"text": "hi", "name": 42}), 400),
        (dict(path="/unknown-site/reports", payload={"text": "hi"}), 404),
        (dict(method="GET"), 405),
    ]
    for kwargs, expected in cases:
        assert call(roomy, **kwargs)[0] == expected, kwargs
    big_context = _app(stores, burst=100)
    assert (
        call(big_context, payload={"text": "hi", "context": {"blob": "x" * 70_000}})[0]
        == 400
    )


def test_attachments_are_stored_by_digest_and_loaded_only_when_asked(stores):
    payload = {
        "text": "see the screenshot",
        "attachments": [
            {
                "name": "screen.png",
                "media_type": "image/png",
                "data": base64.b64encode(PNG).decode(),
            }
        ],
    }
    assert call(_app(stores), payload=payload)[0] == 201
    digest = hashlib.sha256(PNG).hexdigest()
    assert stores[1] == {digest: PNG}
    record = next(iter(stores[0].values()))
    assert record["attachments"][0][
        "ref"
    ] == f"blob:{digest}" and "data" not in json.dumps(record)
    [message] = correspond.read(f"webinbox:{SITE}", registry=_inbox(stores))
    assert message.attachments[0].content() == PNG


def test_listening_follows_report_order_and_the_inbox_has_no_writer(stores):
    moments = iter([NOW, NOW + 1, NOW + 2])
    app = _app(stores, clock=lambda: next(moments), burst=10)
    for text in ("first", "second", "third"):
        assert call(app, payload={"text": text})[0] == 201
    cursors = {}
    registry = _inbox(stores)
    events = correspond.listen(
        f"webinbox:{SITE}", cursors=cursors, limit=1, registry=registry
    )
    assert [e.message.text for e in events] == ["first"]
    assert [
        e.message.text
        for e in correspond.listen(f"webinbox:{SITE}", cursors=cursors, registry=registry)
    ] == ["second", "third"]
    assert (
        list(correspond.listen(f"webinbox:{SITE}", cursors=cursors, registry=registry))
        == []
    )
    with pytest.raises(NotSupported, match="another channel"):
        correspond.send(f"webinbox:{SITE}", "thanks", registry=registry)


def test_the_collector_from_the_environment(monkeypatch, isolated):
    with pytest.raises(MissingRequirement, match="CORRESPOND_WEBINBOX_SITES"):
        app_from_env()
    monkeypatch.setenv("CORRESPOND_WEBINBOX_SITES", f"{SITE}, other-site")
    monkeypatch.setenv("CORRESPOND_WEBINBOX_ORIGINS", ORIGIN)
    monkeypatch.setenv("CORRESPOND_WEBINBOX_SECRET_EXAMPLE_SITE", SECRET)
    monkeypatch.setenv("CORRESPOND_WEBINBOX_SECRET_OTHER_SITE", SECRET)
    app = app_from_env()
    identity = sign_identity(SECRET, "other-site", "u-7")
    assert (
        call(
            app, path="/other-site/reports", payload={"text": "hi", "identity": identity}
        )[0]
        == 201
    )
    stored = list((isolated / "data" / "webinbox" / "other-site").iterdir())
    assert len(stored) == 1 and stored[0].suffix == ".json"
    [message] = correspond.read("webinbox:other-site", registry={"webinbox": WebInbox()})
    assert message.authenticity.grade.value == "bound"


def test_lifespan_messages_are_answered(stores):
    app = _app(stores)
    incoming = [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}]
    sent = []

    async def receive():
        return incoming.pop(0)

    async def send(message):
        sent.append(message["type"])

    asyncio.run(app({"type": "lifespan"}, receive, send))
    assert sent == ["lifespan.startup.complete", "lifespan.shutdown.complete"]


def test_the_identity_payload_is_plain_lines_without_breaks():
    assert identity_payload(SITE, "u-42", NOW, email="ada@example.org").split(b"\n") == [
        b"v1",
        SITE.encode(),
        b"1757592000",
        b"u-42",
        b"",
        b"ada@example.org",
    ]
    with pytest.raises(ValueError):
        identity_payload(SITE, "u-42\nsomeone-else", NOW)
    with pytest.raises(ValueError):
        Site(name="Not A Site")


def test_behind_a_proxy_the_limit_applies_per_visitor_read_from_the_right(stores):
    proxy = ("127.0.0.1", 5000)

    def statuses(app, forwarded):
        return [
            call(
                app, payload={"text": "hi"}, client=proxy, headers={"x-forwarded-for": f}
            )[0]
            for f in forwarded
        ]

    visitors = ["198.51.100.7", "198.51.100.8"]
    assert statuses(_app(stores, burst=1), visitors) == [201, 429], (
        "without trusted proxies, everyone behind the proxy shares one limit"
    )
    behind_one = _app(stores, burst=1, trusted_proxies=1)
    assert statuses(behind_one, visitors) == [201, 201]
    spoofed = ["192.0.2.1, 198.51.100.9", "192.0.2.2, 198.51.100.9"]
    assert statuses(behind_one, spoofed) == [201, 429], (
        "what a client writes on the left buys no fresh limit"
    )
    for bad in (-1, True, "1"):
        with pytest.raises(ValueError):
            _app(stores, trusted_proxies=bad)


def test_several_sites_need_a_secret_each(monkeypatch):
    from correspond.channels.webinbox import site_secret_env

    monkeypatch.setenv("CORRESPOND_WEBINBOX_SITES", "site-a,site-b")
    monkeypatch.setenv("CORRESPOND_WEBINBOX_ORIGINS", ORIGIN)
    monkeypatch.setenv("CORRESPOND_WEBINBOX_SECRET", SECRET)
    with pytest.raises(MissingRequirement, match="CORRESPOND_WEBINBOX_SECRET_SITE_A"):
        app_from_env()
    monkeypatch.setenv(site_secret_env("site-a"), "secret-for-site-a")
    monkeypatch.setenv(site_secret_env("site-b"), "secret-for-site-b")
    app = app_from_env()
    own = sign_identity("secret-for-site-b", "site-b", "u-7")
    borrowed = sign_identity("secret-for-site-a", "site-b", "u-7")
    assert (
        call(app, path="/site-b/reports", payload={"text": "hi", "identity": own})[0]
        == 201
    )
    assert (
        call(app, path="/site-b/reports", payload={"text": "hi", "identity": borrowed})[0]
        == 401
    )
    monkeypatch.setenv("CORRESPOND_WEBINBOX_TRUSTED_PROXIES", "one")
    with pytest.raises(MissingRequirement, match="whole number"):
        app_from_env()


def test_the_native_fields_messages_carry_are_the_ones_capabilities_declare(stores):
    assert call(_app(stores), payload={"text": "hi", "page": f"{ORIGIN}/x"})[0] == 201
    declared = set(WebInbox().capabilities.native_fields)
    for message in correspond.read(f"webinbox:{SITE}", registry=_inbox(stores)):
        assert set(message.native) <= declared
