"""The registry: lazy, gated on optional modules; settings resolved in order; requirements explained without secrets."""

import json
import shutil
import sys

import pytest

from correspond import registry
from correspond.errors import MissingRequirement, UnknownChannel
from correspond.registry import (
    ChannelInfo,
    Setting,
    build_registry,
    check_requirements,
    require,
    value,
)
from correspond.testing import FakeChannel

SECRET = "123456:example-token-value-for-tests-only"


def test_every_built_channel_registers_lazily_and_planned_ones_do_not():
    built = build_registry()
    assert set(built) == {c.name for c in registry.CHANNELS if not c.planned}
    assert {"discord", "slack", "signal", "apprise"}.isdisjoint(built)


def test_a_channel_registers_only_when_its_optional_modules_import():
    infos = [
        ChannelInfo(name="present", summary="", factory="correspond.testing:FakeChannel"),
        ChannelInfo(
            name="absent",
            summary="",
            factory="nowhere:Adapter",
            modules=("a_module_that_is_not_installed_xyz",),
            extra="absent",
        ),
        ChannelInfo(
            name="broken",
            summary="",
            factory="a_module_that_is_not_installed_xyz:Adapter",
        ),
    ]
    built = build_registry(infos)
    assert set(built) == {"present", "broken"}
    assert isinstance(built["present"], FakeChannel)
    with pytest.raises(ModuleNotFoundError):
        built["broken"]  # lazy: the factory is imported only when the channel is used


def test_planned_and_unknown_channels_explain_themselves():
    discord = check_requirements("discord")
    assert not discord["ok"] and discord["planned"].endswith("/issues/2")
    unknown = check_requirements("carrier-pigeon")
    assert not unknown["ok"] and "built in:" in unknown["summary"]
    with pytest.raises(UnknownChannel):
        registry.info("carrier-pigeon")


def test_requirements_name_the_variable_and_where_to_get_it_and_never_show_a_secret(
    monkeypatch,
):
    missing = check_requirements("telegram")
    assert not missing["ok"]
    assert any(
        "TELEGRAM_BOT_TOKEN" in p and "core.telegram.org" in p
        for p in missing["problems"]
    )
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", SECRET)
    ready = check_requirements("telegram")
    assert ready["ok"], ready["problems"]
    token = next(row for row in ready["settings"] if row["key"] == "token")
    assert token["source"] == "env" and token["secret"]
    assert SECRET not in json.dumps(ready)


def test_values_come_from_env_then_config_then_default(monkeypatch, config_file):
    assert value("ntfy", "url") == "https://ntfy.sh"
    config_file('[ntfy]\nurl = "https://ntfy.example.org"\n')
    assert value("ntfy", "url") == "https://ntfy.example.org"
    monkeypatch.setenv("NTFY_URL", "https://push.example.org")
    assert value("ntfy", "url") == "https://push.example.org"


def test_a_secret_is_never_read_from_the_config_file(config_file):
    config_file(f'[telegram]\ntoken = "{SECRET}"\n')
    assert value("telegram", "token") is None
    with pytest.raises(MissingRequirement) as caught:
        require("telegram", "token")
    assert caught.value.kind == "auth" and "TELEGRAM_BOT_TOKEN" in str(caught.value)
    report = check_requirements("telegram")
    assert any("ignored" in w for w in report["warnings"])
    assert SECRET not in json.dumps(report)


def test_a_secret_can_come_from_the_keychain_under_a_configurable_service(
    monkeypatch, config_file
):
    from correspond import settings

    asked = []
    monkeypatch.setattr(
        settings,
        "keychain_get",
        lambda service, **kw: asked.append(service) or "kc-topic-value",
    )
    assert registry.resolve("ntfy", registry.info("ntfy").setting("topic")) == (
        "kc-topic-value",
        "keychain",
    )
    config_file('[ntfy]\ntopic_keychain_service = "my-ntfy-topic"\n')
    registry.resolve("ntfy", registry.info("ntfy").setting("topic"))
    assert asked == ["correspond-ntfy-topic", "my-ntfy-topic"]


def test_missing_modules_binaries_and_platforms_are_reported(monkeypatch):
    info = ChannelInfo(
        name="exotic",
        summary="needs everything",
        factory="nowhere:X",
        modules=("a_module_that_is_not_installed_xyz",),
        extra="exotic",
        binaries=(("a-binary-that-is-not-there", "neither-is-this"),),
        platforms=("plan9",),
        settings=(Setting(key="key", env="EXOTIC_KEY", what="a key", required=True),),
    )
    monkeypatch.setitem(registry._INFO, "exotic", info)
    report = check_requirements("exotic", registry={})
    assert report["install"] == 'pip install "correspond[exotic]"'
    text = "\n".join(report["problems"])
    assert (
        "a-binary-that-is-not-there or neither-is-this" in text
        and "plan9" in text
        and "EXOTIC_KEY" in text
    )


def test_macos_requirements_on_another_platform(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    report = check_requirements("macos")
    assert not report["ok"] and any("darwin" in p for p in report["problems"])


def test_registering_a_channel_refuses_a_second_one_unless_replacing():
    built = build_registry([])
    registry.register_channel(FakeChannel(), registry=built)
    with pytest.raises(Exception, match="already registered"):
        registry.register_channel(FakeChannel(), registry=built)
    replacement = FakeChannel()
    assert (
        registry.register_channel(replacement, replace=True, registry=built)
        is replacement
    )
    plain = {}
    registry.register_channel(FakeChannel("other"), registry=plain)
    assert list(plain) == ["other"]
    registry.unregister_channel("other", registry=plain)
    assert plain == {}


def test_skipping_the_keychain_runs_nothing(monkeypatch):
    from correspond import settings

    def refuse(service, **kwargs):
        raise AssertionError(f"looked in the Keychain for {service}")

    monkeypatch.setattr(settings, "keychain_get", refuse)
    assert value("telegram", "token", keychain=False) is None
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", SECRET)
    assert value("telegram", "token", keychain=False) == SECRET
