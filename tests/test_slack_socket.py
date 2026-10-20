"""Slack Socket Mode (correspond#95): a short window over an injected websocket. Offline: no Slack, no network."""

import json

import pytest

import correspond
from correspond.channels._http import Response
from correspond.channels._slack_socket import (
    CONNECTIONS_OPEN_URL,
    socket_url,
    socket_window,
)
from correspond.channels.slack import Slack, received_key
from correspond.errors import ChannelError, MissingRequirement

APP_TOKEN = "xapp-fake"  # fictional
TOKEN = "xoxb-fake"  # fictional
CHANNEL = "C024BE91L"
OTHER = "C0OTHER01"
SOCKET = "wss://wss-primary.slack.com/link/?ticket=example"
NOW_S = 1760003640


class FakeSocket:
    """A scripted Socket Mode connection: hands out ``frames`` in order, then is quiet."""

    def __init__(self, frames):
        self.frames = [f if isinstance(f, str) else json.dumps(f) for f in frames]
        self.sent, self.closed, self.timeouts = [], False, []

    def __call__(self, url, *, timeout):
        self.url = url
        return self

    def send(self, text):
        self.sent.append(json.loads(text))

    def recv(self, timeout=None):
        self.timeouts.append(timeout)
        if not self.frames:
            raise TimeoutError
        frame = self.frames.pop(0)
        if frame == "closed":
            raise ConnectionError(1000)
        return frame

    def close(self):
        self.closed = True


def _opener(answer=None, status=200):
    asked = []

    def http(method, url, *, headers=None, body=None, timeout=None):
        asked.append((method, url, headers))
        data = answer if answer is not None else {"ok": True, "url": SOCKET}
        return Response(status, {}, json.dumps(data).encode())

    http.asked = asked
    return http


def _envelope(event, *, event_id="Ev0001", envelope_id="env-1", channel=CHANNEL):
    return {
        "envelope_id": envelope_id,
        "type": "events_api",
        "accepts_response_payload": False,
        "payload": {
            "type": "event_callback",
            "team_id": "T0EXAMPLE",
            "event_id": event_id,
            "event": {"channel": channel, **event},
        },
    }


def _message(ts, text="hello", **extra):
    return {"type": "message", "user": "U0ADA", "text": text, "ts": ts, **extra}


HELLO = {"type": "hello", "num_connections": 1}


def test_a_window_acknowledges_every_envelope_and_keeps_the_events():
    socket = FakeSocket(
        [
            HELLO,
            _envelope(_message("1760000000.000100")),
            {"envelope_id": "env-2", "type": "slash_commands", "payload": {"command": "/x"}},
            "not json",
        ]
    )
    http = _opener()
    payloads = socket_window(APP_TOKEN, wait=2, connect=socket, http=http)
    assert [p["event_id"] for p in payloads] == ["Ev0001"]
    assert socket.sent == [{"envelope_id": "env-1"}, {"envelope_id": "env-2"}]
    assert socket.closed and socket.url == SOCKET
    method, url, headers = http.asked[0]
    assert (method, url, headers["Authorization"]) == ("POST", CONNECTIONS_OPEN_URL, f"Bearer {APP_TOKEN}")
    assert socket.timeouts[-1] == 2  # quiet for the wait: the window closes


def test_a_window_without_hello_is_a_network_error():
    socket = FakeSocket([])
    with pytest.raises(ChannelError) as caught:
        socket_window(APP_TOKEN, wait=1, connect=socket, http=_opener())
    assert caught.value.kind == "network" and socket.closed


def test_disconnect_or_a_closed_connection_ends_the_window():
    later = _envelope(_message("1760000001.000100"), event_id="Ev0002", envelope_id="env-2")
    for ending in ({"type": "disconnect", "reason": "refresh_requested"}, "closed"):
        socket = FakeSocket([HELLO, _envelope(_message("1760000000.000100")), ending, later])
        payloads = socket_window(APP_TOKEN, wait=1, connect=socket, http=_opener())
        assert [p["event_id"] for p in payloads] == ["Ev0001"] and socket.closed


def test_a_busy_socket_still_closes_after_the_longest_window():
    frames = [HELLO] + [
        _envelope(_message(f"17600000{i:02d}.000100"), event_id=f"Ev{i:04d}", envelope_id=f"e{i}")
        for i in range(50)
    ]
    socket = FakeSocket(frames)
    clock = iter(range(0, 10_000, 10))  # every reading ten seconds later
    payloads = socket_window(
        APP_TOKEN, wait=5, connect=socket, http=_opener(), clock=lambda: next(clock), max_window_s=60
    )
    assert 0 < len(payloads) < 50


@pytest.mark.parametrize(
    "answer, kind",
    [
        ({"ok": False, "error": "invalid_auth"}, "auth"),
        ({"ok": False, "error": "not_allowed_token_type"}, "validation"),
        ({"ok": True, "url": "wss://slack.com.example.org/link"}, "validation"),
        ({"ok": True, "url": "ws://wss-primary.slack.com/link"}, "validation"),
    ],
)
def test_opening_a_connection_refuses_bad_answers(answer, kind):
    with pytest.raises(ChannelError) as caught:
        socket_url(APP_TOKEN, http=_opener(answer))
    assert caught.value.kind == kind and APP_TOKEN not in str(caught.value)


def test_a_transport_failure_never_shows_the_app_token():
    def http(*args, **kwargs):
        raise ChannelError(f"cannot reach it with {APP_TOKEN}", kind="network")

    with pytest.raises(ChannelError) as caught:
        socket_url(APP_TOKEN, http=http)
    assert APP_TOKEN not in str(caught.value) and caught.value.kind == "network"


def _users_unknown(*args, **kwargs):
    return Response(200, {}, json.dumps({"ok": False, "error": "user_not_found"}).encode())


@pytest.fixture
def socket_mode(monkeypatch):
    monkeypatch.setenv("CORRESPOND_SLACK_TOKEN", TOKEN)
    monkeypatch.setenv("CORRESPOND_SLACK_APP_TOKEN", APP_TOKEN)
    monkeypatch.setenv("CORRESPOND_SLACK_DELIVERY", "socket")


def test_socket_delivery_stores_every_channel_and_reports_this_ones(socket_mode):
    windows = [
        [
            _envelope(_message("1760000000.000100", "first"))["payload"],
            _envelope(_message("1760000001.000100", "elsewhere"), event_id="Ev0002", channel=OTHER)["payload"],
            _envelope(_message("1760000000.000100", "first"))["payload"],  # Slack's retry
            {"type": "event_callback", "event_id": "Ev0003", "event": {"type": "reaction_added"}},
        ],
        [],
    ]
    asked = []

    def socket(app_token, *, wait, sink):
        asked.append((app_token, wait))
        payloads = windows.pop(0) if windows else []
        for payload in payloads:
            sink(payload)
        return payloads

    store, clock = {}, [NOW_S]
    registry = {
        "slack": Slack(
            http=_users_unknown, keychain=lambda s: "", clock=lambda: clock[0], events=store, socket=socket
        )
    }
    cursors = {}
    got = list(correspond.listen(f"slack:{CHANNEL}", cursors=cursors, registry=registry))
    assert [(e.kind, e.message.text) for e in got] == [("message.created", "first")]
    assert len(store) == 2 and asked == [(APP_TOKEN, 5.0)]
    other = list(correspond.listen(f"slack:{OTHER}", cursors=cursors, registry=registry))
    assert [e.message.text for e in other] == ["elsewhere"]  # kept for its own listen
    assert len(asked) == 1, "a listen right after a window reads the store, no second window"
    clock[0] += 10
    assert list(correspond.listen(f"slack:{CHANNEL}", cursors=cursors, registry=registry)) == []
    assert len(asked) == 2


def test_socket_delivery_stamps_after_what_is_stored(socket_mode):
    ahead = NOW_S * 10**9 + 5_000  # stored by a clock running ahead
    store = {received_key(ahead, "Ev0009"): {"channel": OTHER, "event": _message("1760000000.000100")}}
    payload = _envelope(_message("1760000002.000100"))["payload"]
    slack = Slack(
        http=_users_unknown, keychain=lambda s: "", clock=lambda: NOW_S, events=store,
        socket=lambda token, *, wait, sink: sink(payload),
    )
    assert slack._receive_socket() == 1
    assert max(store) == received_key(ahead + 1, "Ev0001")


def test_socket_delivery_needs_an_app_level_token(monkeypatch, socket_mode):
    called = []
    slack = Slack(keychain=lambda s: "", events={}, socket=lambda *a, **k: called.append(1) or [])
    monkeypatch.setenv("CORRESPOND_SLACK_APP_TOKEN", TOKEN)
    with pytest.raises(ChannelError) as caught:
        slack._receive_socket()
    assert caught.value.kind == "validation" and "xapp-" in str(caught.value)
    assert TOKEN not in str(caught.value)
    monkeypatch.delenv("CORRESPOND_SLACK_APP_TOKEN")
    with pytest.raises(MissingRequirement, match="CORRESPOND_SLACK_APP_TOKEN"):
        slack._receive_socket()
    assert called == []


@pytest.mark.parametrize("wait", ["0", "-1", "61", "soon"])
def test_a_wait_out_of_range_is_refused(monkeypatch, socket_mode, wait):
    monkeypatch.setenv("CORRESPOND_SLACK_LISTEN_WAIT_S", wait)
    slack = Slack(keychain=lambda s: "", events={}, socket=lambda *a, **k: [])
    with pytest.raises(ChannelError) as caught:
        slack._receive_socket()
    assert caught.value.kind == "validation"


def test_without_websocket_client_the_error_names_the_extra(monkeypatch):
    import sys

    from correspond.channels import _discord_gateway, _slack_socket

    monkeypatch.setitem(sys.modules, "websocket", None)
    with pytest.raises(MissingRequirement, match=r"correspond\[slack\]"):
        _slack_socket.websocket_connect(SOCKET, timeout=1)
    with pytest.raises(MissingRequirement, match=r"correspond\[discord\]"):
        _discord_gateway.websocket_connect("wss://gateway.discord.gg", timeout=1)


def test_the_settings_and_capabilities_describe_socket_mode():
    from correspond.registry import CHANNELS

    info = next(c for c in CHANNELS if c.name == "slack")
    app_token = next(s for s in info.settings if s.key == "app_token")
    assert app_token.secret and app_token.env == "CORRESPOND_SLACK_APP_TOKEN"
    assert any("delivery = socket" in note for note in Slack().capabilities.notes)


def test_an_event_is_acknowledged_only_after_it_is_kept():
    first = _envelope(_message("1760000000.000100"))
    second = _envelope(_message("1760000001.000100"), event_id="Ev0002", envelope_id="env-2")
    socket = FakeSocket([HELLO, first, second])
    kept = []

    def sink(payload):
        if payload["event_id"] == "Ev0001":
            raise OSError("the store is full")
        kept.append(payload["event_id"])

    payloads = socket_window(APP_TOKEN, wait=1, sink=sink, connect=socket, http=_opener())
    assert kept == ["Ev0002"] and [p["event_id"] for p in payloads] == ["Ev0002"]
    assert socket.sent == [{"envelope_id": "env-2"}], "the unkept one is left for Slack to resend"


def test_a_failed_acknowledgement_ends_the_window_keeping_what_was_kept():
    socket = FakeSocket([HELLO, _envelope(_message("1760000000.000100")), _envelope(_message("1760000001.000100"), event_id="Ev0002", envelope_id="env-2")])

    def broken(text):
        raise ConnectionError("gone")

    socket.send = broken
    kept = []
    payloads = socket_window(APP_TOKEN, wait=1, sink=lambda p: kept.append(p["event_id"]), connect=socket, http=_opener())
    assert kept == ["Ev0001"] and len(payloads) == 1 and socket.closed


@pytest.mark.parametrize(
    "frames, kind, says",
    [
        ([{"type": "disconnect", "reason": "link_disabled"}], "validation", "Socket Mode is off"),
        (["closed"], "network", "before it said hello"),
    ],
)
def test_a_socket_refused_before_hello_is_an_error_not_a_quiet_window(frames, kind, says):
    socket = FakeSocket(frames)
    with pytest.raises(ChannelError, match=says) as caught:
        socket_window(APP_TOKEN, wait=1, connect=socket, http=_opener())
    assert caught.value.kind == kind and socket.closed


def test_pings_do_not_count_as_events():
    socket = FakeSocket([HELLO, "", _envelope(_message("1760000000.000100"))])
    assert len(socket_window(APP_TOKEN, wait=1, connect=socket, http=_opener())) == 1


def test_a_rate_limited_connection_request_says_how_long_to_wait():
    def http(*args, **kwargs):
        return Response(429, {"retry-after": "30"}, b"")

    with pytest.raises(ChannelError) as caught:
        socket_url(APP_TOKEN, http=http)
    assert caught.value.kind == "rate_limited" and caught.value.retry_after == 30


def test_without_websocket_client_no_connection_is_requested(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "websocket", None)
    http = _opener()
    with pytest.raises(MissingRequirement, match=r"correspond\[slack\]"):
        socket_window(APP_TOKEN, wait=1, http=http)
    assert http.asked == []


def test_the_websocket_wrapper_answers_pings_and_normalises_failures(monkeypatch):
    import sys
    import types

    from correspond.channels import _slack_socket

    fake = types.ModuleType("websocket")

    class WebSocketException(Exception):
        pass

    class WebSocketTimeoutException(WebSocketException):
        pass

    class WebSocketConnectionClosedException(WebSocketException):
        pass

    fake.WebSocketException = WebSocketException
    fake.WebSocketTimeoutException = WebSocketTimeoutException
    fake.WebSocketConnectionClosedException = WebSocketConnectionClosedException
    fake.ABNF = types.SimpleNamespace(OPCODE_TEXT=1, OPCODE_BINARY=2, OPCODE_CLOSE=8, OPCODE_PING=9)
    script = [(9, b"ping"), (1, '{"type": "hello"}'), WebSocketTimeoutException(), (8, b""), OSError("tls")]

    class FakeWs:
        def settimeout(self, t):
            pass

        def recv_data(self, control_frame=False):
            step = script.pop(0)
            if isinstance(step, Exception):
                raise step
            return step

        def send(self, text):
            raise WebSocketConnectionClosedException()

        def close(self):
            pass

    fake.create_connection = lambda url, timeout: FakeWs()
    monkeypatch.setitem(sys.modules, "websocket", fake)
    connection = _slack_socket.websocket_connect(SOCKET, timeout=1)
    assert connection.recv(timeout=1) == ""  # the ping
    assert connection.recv(timeout=1) == '{"type": "hello"}'
    with pytest.raises(TimeoutError):
        connection.recv(timeout=1)
    with pytest.raises(ConnectionError):
        connection.recv(timeout=1)  # a close frame
    with pytest.raises(ConnectionError):
        connection.recv(timeout=1)  # any other failure
    with pytest.raises(ConnectionError):
        connection.send("{}")
