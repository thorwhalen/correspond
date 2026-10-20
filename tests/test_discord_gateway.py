"""Discord's gateway window: resume, identify only as a fallback, heartbeats, fatal closes. Scripted, offline."""

import json

import pytest

from correspond.channels._discord_gateway import (
    DEFAULT_INTENTS,
    GATEWAY_URL,
    gateway_window,
)
from correspond.errors import ChannelError

TOKEN = "example-token"
RESUME_URL = "wss://gateway-us-east1-b.discord.gg"


class Script:
    """A connection that plays ``frames`` (dicts, or exceptions to raise) and records what was sent."""

    def __init__(self, *frames):
        self.frames = list(frames)
        self.sent, self.urls, self.closed = [], [], False
        self.now = 0.0

    def clock(self):
        return self.now

    def __call__(self, url, *, timeout):
        self.urls.append(url)
        return self

    def recv(self, timeout=None):
        if not self.frames or self.frames[0] == "quiet":
            if self.frames:
                self.frames.pop(0)
            self.now += timeout  # silence costs the whole timeout
            raise TimeoutError
        frame = self.frames[0]
        if isinstance(frame, tuple):  # ("after", seconds, frame): arrives that late
            _, delay, late = frame
            if timeout is not None and delay > timeout:
                self.frames[0] = ("after", delay - timeout, late)
                self.now += timeout
                raise TimeoutError
            self.now += delay
            self.frames[0] = late
        frame = self.frames.pop(0)
        if isinstance(frame, Exception):
            raise frame
        return json.dumps(frame)

    def send(self, text):
        self.sent.append(json.loads(text))

    def close(self):
        self.closed = True

    def ops(self):
        return [m["op"] for m in self.sent]


HELLO = {"op": 10, "d": {"heartbeat_interval": 41250}}
READY = {"op": 0, "t": "READY", "s": 1, "d": {"session_id": "s1", "resume_gateway_url": RESUME_URL}}


def _created(seq, message_id="1001"):
    return {"op": 0, "t": "MESSAGE_CREATE", "s": seq, "d": {"id": message_id, "channel_id": "42"}}


def test_a_first_window_identifies_and_returns_resumable_state():
    script = Script(HELLO, READY, _created(2), {"op": 0, "t": "TYPING_START", "s": 3, "d": {}})
    dispatches, state = gateway_window(TOKEN, None, wait=1, connect=script, clock=script.clock)
    assert script.urls[0].startswith(GATEWAY_URL)
    assert script.ops() == [2]
    assert script.sent[0]["d"]["intents"] == DEFAULT_INTENTS
    assert [d["t"] for d in dispatches] == ["MESSAGE_CREATE"]  # typing is dropped
    assert state == {
        "session_id": "s1",
        "resume_gateway_url": RESUME_URL,
        "seq": 3,
        "identifies": 1,
        "identified_at": [state["identified_at"][0]],
    }
    assert script.closed


def test_a_later_window_resumes_without_identifying():
    state = {"session_id": "s1", "resume_gateway_url": RESUME_URL, "seq": 3, "identifies": 1}
    script = Script(HELLO, _created(4, "1002"), {"op": 0, "t": "RESUMED", "s": 5, "d": None})
    dispatches, after = gateway_window(TOKEN, state, wait=1, connect=script, clock=script.clock)
    assert script.urls[0].startswith(RESUME_URL)
    assert script.sent == [{"op": 6, "d": {"token": TOKEN, "session_id": "s1", "seq": 3}}]
    assert [d["d"]["id"] for d in dispatches] == ["1002"]
    assert after["seq"] == 5 and after["identifies"] == 1


def test_a_resumable_invalid_session_keeps_the_state_for_the_next_window():
    state = {"session_id": "s1", "resume_gateway_url": RESUME_URL, "seq": 3}
    script = Script(HELLO, {"op": 9, "d": True})
    _, after = gateway_window(TOKEN, state, wait=1, connect=script, clock=script.clock)
    assert script.ops() == [6] and after["session_id"] == "s1"


def test_a_refused_resume_falls_back_to_identify_once():
    state = {"session_id": "old", "resume_gateway_url": RESUME_URL, "seq": 9}
    script = Script(HELLO, {"op": 9, "d": False}, READY)
    _, after = gateway_window(TOKEN, state, wait=1, connect=script, clock=script.clock)
    assert script.ops() == [6, 2]
    assert after["session_id"] == "s1" and after["identifies"] == 1


def test_heartbeats_answer_requests_and_keep_time_through_silence():
    script = Script(HELLO, {"op": 1, "d": None}, READY)
    gateway_window(TOKEN, None, wait=100, connect=script, clock=script.clock)
    # one answered request, then scheduled beats every 41.25s through 100 quiet seconds
    assert script.ops().count(1) >= 3 and script.now >= 100


def test_an_identify_waits_for_ready_beyond_a_short_window():
    script = Script(HELLO, ("after", 8, READY))  # READY later than wait=1
    _, state = gateway_window(TOKEN, None, wait=1, connect=script, clock=script.clock)
    assert state["session_id"] == "s1" and state["identifies"] == 1


def test_a_bad_resume_url_in_ready_falls_back_to_the_main_gateway():
    bad = {**READY, "d": {"session_id": "s1", "resume_gateway_url": "wss://collector.example.org"}}
    script = Script(HELLO, bad)
    _, state = gateway_window(TOKEN, None, wait=1, connect=script, clock=script.clock)
    assert state["resume_gateway_url"] == GATEWAY_URL
    again = Script(HELLO)
    gateway_window(TOKEN, state, wait=1, connect=again, clock=again.clock)
    assert again.ops() == [6]  # resumed, not identified


def test_identifies_are_capped_once_per_window_and_per_day():
    script = Script(HELLO, {"op": 9, "d": False}, {"op": 9, "d": False})
    _, state = gateway_window(TOKEN, None, wait=1, connect=script, clock=script.clock)
    assert script.ops() == [2] and state["identifies"] == 1
    state = {"identified_at": [1000.0] * 3}
    with pytest.raises(ChannelError) as raised:
        gateway_window(TOKEN, state, wait=1, connect=Script(), wall=lambda: 2000.0, max_identifies_per_day=3)
    assert raised.value.kind == "rate_limited"
    fresh = Script(HELLO, READY)
    _, later = gateway_window(TOKEN, state, wait=1, connect=fresh, clock=fresh.clock, wall=lambda: 1000.0 + 86_401, max_identifies_per_day=3)
    assert later["identified_at"] == [1000.0 + 86_401]


def test_reconnect_and_a_plain_close_end_the_window_quietly():
    for ending in ({"op": 7, "d": None}, ConnectionError(1000)):
        script = Script(HELLO, READY, ending, _created(9))
        dispatches, state = gateway_window(TOKEN, None, wait=1, connect=script, clock=script.clock)
        assert dispatches == [] and state["session_id"] == "s1"


@pytest.mark.parametrize("code, kind", [(4004, "auth"), (4014, "permission")])
def test_a_fatal_close_is_an_error_not_a_retry(code, kind):
    script = Script(HELLO, ConnectionError(code))
    with pytest.raises(ChannelError) as raised:
        gateway_window(TOKEN, None, wait=1, connect=script, clock=script.clock)
    assert raised.value.kind == kind and TOKEN not in str(raised.value)
    assert script.closed


def test_a_tampered_resume_url_never_receives_the_token():
    state = {"session_id": "s1", "resume_gateway_url": "wss://collector.example.org", "seq": 3}
    script = Script(HELLO, READY)
    gateway_window(TOKEN, state, wait=1, connect=script, clock=script.clock)
    assert script.urls[0].startswith(GATEWAY_URL)
    assert script.ops() == [2]  # identified on Discord's own gateway, no resume elsewhere
