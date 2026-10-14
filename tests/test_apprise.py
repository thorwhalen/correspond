"""Apprise: send only, aliases never URLs, credentials never in a plan, honest failures (thorwhalen/correspond#5).

The ``apprise`` package is replaced by a fake module, so these run without the extra.
"""

import importlib
import importlib.util
import json
import types

import pytest

import correspond
from correspond import registry
from correspond.channels.apprise import Apprise, alias_env
from correspond.errors import ChannelError, InvalidRef, MissingRequirement

SECRET_URL = "discord://1234567890/example-secret-token"


class _Book:
    def __init__(self, log, readable=True):
        self.log, self.readable = log, readable

    def add(self, path):
        self.log.append(("config", path))
        return self.readable


class _Notifier:
    """Apprise 2.x's answers: ``add`` a bool, ``find`` the matching services, ``notify`` a bool."""

    def __init__(self, log, *, accepts=True, outcome=True, tags=("ops",)):
        self.log, self.accepts, self.outcome, self.tags = log, accepts, outcome, tags

    def find(self, tag, match_always=True):
        self.log.append(("find", tag, match_always))
        return iter([tag] if tag in self.tags else [])

    def add(self, target):
        self.log.append(("add", target if isinstance(target, str) else "config-book"))
        return self.accepts

    def notify(self, *, body, title, tag, match_always=True):
        import logging

        assert logging.getLogger("apprise").disabled, "apprise logs URLs; it must be silenced"
        self.log.append(("notify", body, title, tag, match_always))
        return self.outcome


def _fake_apprise(log, **notifier_kw):
    module = types.SimpleNamespace(
        Apprise=lambda: _Notifier(log, **notifier_kw),
        AppriseConfig=lambda: _Book(log),
    )
    return lambda: module


def _channel(log, **kw):
    return {"apprise": Apprise(apprise=_fake_apprise(log, **kw))}


def test_a_reference_is_an_alias_never_a_url():
    assert Apprise().parse_ref("team-alerts").id == "team-alerts"
    assert Apprise().parse_ref("Team-Alerts").id == "team-alerts"  # case folded
    for bad in ("", "discord://123/token", "a b", "x" * 65, "a_b", "all", "ALWAYS"):
        with pytest.raises(InvalidRef):
            Apprise().parse_ref(bad)


def test_sending_to_an_alias_from_the_environment(monkeypatch):
    monkeypatch.setenv(alias_env("team-alerts"), SECRET_URL)
    log = []
    result = correspond.send(
        "apprise:team-alerts", "backup finished", title="Backups", registry=_channel(log)
    )
    assert result.ok and not result.dry_run
    assert log == [("add", SECRET_URL), ("notify", "backup finished", "Backups", None, False)]
    assert result.plan["service"] == "discord" and result.plan["source"] == "env"
    assert "example-secret-token" not in json.dumps(result.to_dict())


def test_a_dry_run_names_the_service_never_the_url(monkeypatch):
    monkeypatch.setenv(alias_env("team-alerts"), SECRET_URL)
    log = []
    planned = correspond.send("apprise:team-alerts", "hi", dry_run=True, registry=_channel(log))
    assert planned.ok and planned.dry_run and log == []
    as_text = json.dumps(planned.to_dict())
    assert "discord" in as_text and "example-secret-token" not in as_text and "1234567890" not in as_text


def test_an_alias_is_a_tag_in_the_apprise_config_file(monkeypatch, tmp_path):
    book = tmp_path / "apprise.yml"
    book.write_text("urls:\n  - json://localhost:\n      tag: ops\n")
    monkeypatch.setenv("CORRESPOND_APPRISE_CONFIG", str(book))
    log = []
    result = correspond.send("apprise:ops", "disk at 95%", registry=_channel(log))
    assert result.ok and result.plan["tag"] == "ops" and result.plan["source"] == "config"
    assert log == [
        ("config", str(book)),
        ("add", "config-book"),
        ("find", "ops", False),
        ("notify", "disk at 95%", "", "ops", False),
    ]


def test_a_tag_nothing_carries_is_said_so_and_nothing_is_sent(monkeypatch, tmp_path):
    """Apprise answers an unmatched tag with False, like a failed delivery; and an
    ``always``-tagged URL must not catch a typo (find/notify with match_always=False)."""
    book = tmp_path / "apprise.yml"
    book.write_text("urls: []\n")
    monkeypatch.setenv("CORRESPOND_APPRISE_CONFIG", str(book))
    log = []
    result = correspond.send("apprise:nobody", "hi", registry=_channel(log, outcome=False))
    assert not result.ok and "no URL in" in str(result.error) and "nobody" in str(result.error)
    assert not any(entry[0] == "notify" for entry in log)


def test_an_alias_with_nothing_behind_it_says_how_to_fix_it():
    result = correspond.send("apprise:team-alerts", "hi", registry=_channel([]))
    assert not result.ok
    assert alias_env("team-alerts") in str(result.error)


def test_an_unrecognised_url_is_refused_without_echoing_it(monkeypatch):
    monkeypatch.setenv(alias_env("team-alerts"), SECRET_URL)
    with pytest.raises(ChannelError) as caught:
        Apprise(apprise=_fake_apprise([], accepts=False)).send(
            Apprise().parse_ref("team-alerts"), correspond.Draft(text="hi")
        )
    assert caught.value.kind == "validation" and "example-secret-token" not in str(caught.value)


def test_a_partial_failure_is_unconfirmed_not_retryable(monkeypatch):
    """Apprise says False when ANY service failed, so others may have it already."""
    monkeypatch.setenv(alias_env("team-alerts"), SECRET_URL)
    with pytest.raises(ChannelError) as caught:
        Apprise(apprise=_fake_apprise([], outcome=False)).send(
            Apprise().parse_ref("team-alerts"), correspond.Draft(text="hi")
        )
    assert caught.value.kind == "unconfirmed" and not caught.value.retryable


def test_a_keychain_alias_is_looked_up_when_sending_not_in_a_dry_run():
    asked = []
    channel = Apprise(
        apprise=_fake_apprise([]),
        keychain=lambda service: asked.append(service) or SECRET_URL,
        keychain_available=lambda: True,
    )
    ref = channel.parse_ref("team-alerts")
    planned = channel.send(ref, correspond.Draft(text="hi"), dry_run=True)
    assert asked == [] and planned.plan["looked_up_when_sending"] == ["url"]
    sent = channel.send(ref, correspond.Draft(text="hi"))
    assert asked == ["correspond-apprise-url-team-alerts"] and sent.plan["source"] == "keychain"


def test_without_the_extra_the_error_says_which_extra(monkeypatch):
    real = importlib.import_module

    def no_apprise(name, *a, **k):
        if name == "apprise":
            raise ImportError("No module named 'apprise'")
        return real(name, *a, **k)

    monkeypatch.setattr(importlib, "import_module", no_apprise)
    monkeypatch.setenv(alias_env("team-alerts"), SECRET_URL)
    with pytest.raises(MissingRequirement, match=r'correspond\[apprise\]'):
        Apprise().send(Apprise().parse_ref("team-alerts"), correspond.Draft(text="hi"))


def test_check_requirements_names_the_extra_when_apprise_is_missing(monkeypatch):
    real = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util, "find_spec", lambda name, *a: None if name == "apprise" else real(name, *a)
    )
    try:
        found = registry.check_requirements("apprise")
    finally:
        registry.channels.cache_clear()  # it was built while apprise was hidden
    assert not found["ok"] and any('correspond[apprise]' in p for p in found["problems"])


def test_capabilities_say_send_only_honestly():
    caps = Apprise().capabilities
    assert caps.operations == ("send", "audience")
    assert any("send only" in note for note in caps.notes)


def test_the_audience_is_public_and_incomplete():
    found = Apprise().audience(Apprise().parse_ref("team-alerts"))
    assert found.scope == "public" and found.complete is False and found.external is None


def test_the_default_install_does_not_pull_apprise_in():
    import tomllib
    from pathlib import Path

    project = tomllib.loads((Path(__file__).resolve().parent.parent / "pyproject.toml").read_text())["project"]
    assert not any(dep.startswith("apprise") for dep in project["dependencies"])
    assert any(dep.startswith("apprise") for dep in project["optional-dependencies"]["apprise"])


# --- review of #45 ----------------------------------------------------------------


def test_one_alias_holds_one_url(monkeypatch):
    """Apprise splits on commas: two services would get it while the plan named one."""
    monkeypatch.setenv(alias_env("team"), "json://a/, mailto://b@example.org")
    result = correspond.send("apprise:team", "hi", dry_run=True, registry=_channel([]))
    assert not result.ok and "exactly one Apprise URL" in str(result.error)


def test_the_config_setting_is_a_local_path_never_a_url(monkeypatch):
    monkeypatch.setenv("CORRESPOND_APPRISE_CONFIG", "https://host.example/cfg?token=example-secret")
    result = correspond.send("apprise:ops", "hi", dry_run=True, registry=_channel([]))
    assert not result.ok and "local file path" in str(result.error)
    assert "example-secret" not in json.dumps(result.to_dict())


def test_a_missing_config_file_is_said_so_in_a_dry_run_too(monkeypatch, tmp_path):
    monkeypatch.setenv("CORRESPOND_APPRISE_CONFIG", str(tmp_path / "nope.yml"))
    result = correspond.send("apprise:ops", "hi", dry_run=True, registry=_channel([]))
    assert not result.ok and "does not exist" in str(result.error)


def test_a_dry_run_with_a_keychain_says_the_keychain_comes_first(monkeypatch, tmp_path):
    book = tmp_path / "apprise.yml"
    book.write_text("urls: []\n")
    monkeypatch.setenv("CORRESPOND_APPRISE_CONFIG", str(book))
    channel = Apprise(apprise=_fake_apprise([]), keychain=lambda s: "", keychain_available=lambda: True)
    planned = channel.send(channel.parse_ref("ops"), correspond.Draft(text="hi"), dry_run=True)
    assert planned.plan["source"].startswith("looked up when sending, in the Keychain")
    assert planned.plan["source"].endswith("else the tag in the config file")
    assert planned.plan["looked_up_when_sending"] == ["url"] and planned.plan["tag"] == "ops"
