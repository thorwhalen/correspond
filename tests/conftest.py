"""Shared fixtures: an environment with no real configuration, secrets, Keychain or data root, and the fake channel."""

import pytest

from correspond import registry, settings


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Every test runs without the machine's settings: no channel env vars, no config file, no Keychain, a temporary data root."""
    for info in registry.CHANNELS:
        for setting in info.settings:
            monkeypatch.delenv(setting.env, raising=False)
    monkeypatch.setenv(settings.CONFIG_ENVVAR, str(tmp_path / "absent-config.toml"))
    monkeypatch.setenv(settings.DATA_DIR_ENVVAR, str(tmp_path / "data"))
    monkeypatch.setattr(settings, "keychain_get", lambda service, **kwargs: "")
    return tmp_path


@pytest.fixture
def fake():
    """The demo fake channel, registered as ``fake`` in the process registry for the test."""
    from correspond.testing import demo_channel

    channel = registry.register_channel(demo_channel(), replace=True)
    yield channel
    if "fake" in registry.channels():
        registry.unregister_channel("fake")


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    """Write a config file and point correspond at it: ``config_file('[ntfy]\\nurl = "…"')``."""

    def write(text: str):
        path = tmp_path / "config.toml"
        path.write_text(text, encoding="utf-8")
        monkeypatch.setenv(settings.CONFIG_ENVVAR, str(path))
        return path

    return write
