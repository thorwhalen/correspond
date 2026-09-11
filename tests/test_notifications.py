"""ntfy and macOS: headers, topic resolution without leaking the topic, argv-only notifier calls, honest failures."""

import json
import shutil
import subprocess
import sys

import pytest

import correspond
from correspond.channels import macos as macos_module
from correspond.channels._http import Response
from correspond.channels.macos import MacOS
from correspond.channels.ntfy import Ntfy
from correspond.errors import ChannelError


class FakeHttp:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, *, headers=None, body=None, timeout=None):
        self.calls.append(
            {"method": method, "url": url, "headers": dict(headers or {}), "body": body}
        )
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _ok(message_id="msg-1"):
    return Response(200, {}, json.dumps({"id": message_id}).encode())


def _ntfy(http, run=None):
    return {"ntfy": Ntfy(http=http, run=run or _no_run)}


def _no_run(argv, **kwargs):
    raise AssertionError(f"nothing should run here: {argv}")


def test_publishing_to_a_named_topic():
    http = FakeHttp(_ok())
    result = correspond.send(
        "ntfy:example-topic",
        "backup finished",
        title="Backups",
        priority="urgent",
        registry=_ntfy(http),
    )
    assert (
        result.ok and result.message_id == "msg-1" and result.account.acts_as == "service"
    )
    call = http.calls[0]
    assert (
        call["url"] == "https://ntfy.sh/example-topic"
        and call["body"] == "backup finished".encode()
    )
    assert call["headers"]["Title"] == "Backups" and call["headers"]["Priority"] == "5"


def test_a_non_ascii_title_is_encoded_on_one_line():
    http = FakeHttp(_ok())
    correspond.send(
        "ntfy:example-topic",
        "done",
        title="Sauvegarde terminée\nnext",
        registry=_ntfy(http),
    )
    title = http.calls[0]["headers"]["Title"]
    assert title.startswith("=?utf-8?") and "\n" not in title


def test_the_default_topic_comes_from_the_environment_and_is_masked_in_plans(monkeypatch):
    monkeypatch.setenv("NTFY_TOPIC", "example-secret-topic-3f9a")
    monkeypatch.setenv("NTFY_TOKEN", "example-token-value")
    http = FakeHttp(_ok())
    planned = correspond.send("ntfy:", "hello", dry_run=True, registry=_ntfy(http))
    assert planned.ok and http.calls == []
    as_text = json.dumps(planned.to_dict())
    assert (
        "example-secret-topic-3f9a" not in as_text
        and "example-token-value" not in as_text
    )
    assert planned.plan["topic_source"] == "env" and planned.plan["authenticated"] is True
    sent = correspond.send("ntfy:", "hello", registry=_ntfy(http))
    assert sent.ok and http.calls[0]["url"].endswith("/example-secret-topic-3f9a")
    assert http.calls[0]["headers"]["Authorization"] == "Bearer example-token-value"


def test_the_default_topic_can_be_asked_of_a_remote_host_but_never_on_a_dry_run(
    config_file,
):
    config_file(
        '[ntfy]\ntopic_remote = "example-host"\ntopic_remote_file = "/srv/conf/monitor env"\n'
    )
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(
            argv, 0, stdout="NTFY_TOPIC=remote-topic-1234\n", stderr=""
        )

    http = FakeHttp(_ok())
    planned = correspond.send("ntfy:", "hello", dry_run=True, registry=_ntfy(http, run))
    assert planned.ok and calls == [] and "example-host" in planned.plan["topic_source"]
    assert correspond.send("ntfy:", "hello", registry=_ntfy(http, run)).ok
    assert (
        calls[0][:3] == ["ssh", "-o", "BatchMode=yes"] and calls[0][-2] == "example-host"
    )
    assert "'/srv/conf/monitor env'" in calls[0][-1]
    assert http.calls[0]["url"].endswith("/remote-topic-1234")


def test_a_remote_host_that_looks_like_an_option_is_refused(config_file):
    config_file(
        '[ntfy]\ntopic_remote = "-oProxyCommand=something"\ntopic_remote_file = "f"\n'
    )
    result = correspond.send("ntfy:", "hello", registry=_ntfy(FakeHttp(), _no_run))
    assert (result.ok, result.error_kind) == (False, "validation")


def test_no_topic_anywhere_is_a_failed_send_that_says_how_to_set_one():
    for dry_run in (True, False):
        result = correspond.send(
            "ntfy:", "hello", dry_run=dry_run, registry=_ntfy(FakeHttp())
        )
        assert (result.ok, result.error_kind) == (
            False,
            "validation",
        ) and "NTFY_TOPIC" in result.error


def test_ntfy_failures_are_classified():
    limited = FakeHttp(Response(429, {"retry-after": "3"}, b'{"error": "limit reached"}'))
    result = correspond.send("ntfy:example-topic", "x", registry=_ntfy(limited))
    assert (result.error_kind, result.retryable, result.retry_after) == (
        "rate_limited",
        True,
        3.0,
    )
    offline = FakeHttp(
        ChannelError("could not reach ntfy.sh: timed out", kind="network", retryable=True)
    )
    result = correspond.send("ntfy:example-topic", "x", registry=_ntfy(offline))
    assert (result.error_kind, result.retryable) == (
        "network",
        True,
    ) and "example-topic" not in result.error


def test_ntfy_is_send_only():
    with pytest.raises(correspond.NotSupported, match="read"):
        correspond.read("ntfy:example-topic", registry=_ntfy(FakeHttp()))
    with pytest.raises(correspond.InvalidRef):
        Ntfy().parse_ref("not a topic!")


@pytest.fixture
def on_a_mac(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")

    def use(**binaries):
        monkeypatch.setattr(
            shutil, "which", lambda name: binaries.get(name.replace("-", "_"))
        )

    return use


def _recording(returncode=0):
    calls = []

    def run(argv, **kwargs):
        calls.append({"argv": argv, "input": kwargs.get("input")})
        return subprocess.CompletedProcess(
            argv, returncode, stdout="", stderr="boom" if returncode else ""
        )

    return calls, run


def test_terminal_notifier_gets_the_text_as_arguments(on_a_mac):
    on_a_mac(
        terminal_notifier="/opt/bin/terminal-notifier", osascript="/usr/bin/osascript"
    )
    calls, run = _recording()
    result = correspond.send(
        "macos:", 'Done "quoted"', title="Backups", registry={"macos": MacOS(run=run)}
    )
    assert result.ok and calls[0]["argv"] == [
        "/opt/bin/terminal-notifier",
        "-title",
        "Backups",
        "-message",
        'Done "quoted"',
    ]


def test_osascript_receives_a_fixed_script_and_the_text_as_arguments(on_a_mac):
    on_a_mac(osascript="/usr/bin/osascript")
    calls, run = _recording()
    text = '"; do shell script "echo nope'
    assert correspond.send("macos:", text, registry={"macos": MacOS(run=run)}).ok
    assert calls[0]["argv"] == ["/usr/bin/osascript", "-", text, "correspond"]
    assert calls[0]["input"] == macos_module.SCRIPT and text not in calls[0]["input"]


def test_macos_failures_are_unavailable_results(on_a_mac, monkeypatch):
    on_a_mac()
    result = correspond.send("macos:", "hi", registry={"macos": MacOS(run=_no_run)})
    assert result.error_kind == "unavailable" and "terminal-notifier" in result.error
    on_a_mac(osascript="/usr/bin/osascript")
    calls, failing = _recording(returncode=1)
    assert (
        correspond.send("macos:", "hi", registry={"macos": MacOS(run=failing)}).error_kind
        == "unavailable"
    )
    monkeypatch.setattr(sys, "platform", "linux")
    for dry_run in (True, False):
        result = correspond.send(
            "macos:", "hi", dry_run=dry_run, registry={"macos": MacOS(run=_no_run)}
        )
        assert result.error_kind == "unavailable" and "macOS" in result.error
    with pytest.raises(correspond.InvalidRef):
        MacOS().parse_ref("another-mac")
