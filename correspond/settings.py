"""Where correspond keeps state and reads configuration, and how it finds a secret.

- **The data root**: the ``data_dir`` argument, else ``$CORRESPOND_DATA_DIR``, else
  ``data_dir`` in the config file, else ``~/.local/share/correspond``
  (``%LOCALAPPDATA%\\correspond`` on Windows). Each kind of state has its own folder under
  it (``cursors/``, ``webinbox/``, ``telegram/``); nothing is written into a repository.
- **The config file**: ``$CORRESPOND_CONFIG``, else ``~/.config/correspond/config.toml``
  (``$XDG_CONFIG_HOME``; ``%APPDATA%`` on Windows). One table per channel::

      [ntfy]
      url = "https://ntfy.example.org"
      topic_keychain_service = "my-ntfy-topic"

- **A value** comes from its environment variable, else the channel's table in the config
  file, else its default. **A secret** is never read from the config file: it comes from
  its environment variable, else (on macOS) a Keychain item whose service name is
  ``<key>_keychain_service`` in the config (default ``correspond-<channel>-<key>``).

Nothing here prints or logs a value; :func:`mask` is for showing that one is set.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from correspond.errors import CorrespondError

__all__ = [
    "APP_NAME",
    "CONFIG_ENVVAR",
    "DATA_DIR_ENVVAR",
    "channel_config",
    "config_path",
    "data_dir",
    "keychain_available",
    "keychain_get",
    "keychain_service",
    "load_config",
    "mask",
]

APP_NAME = "correspond"
DATA_DIR_ENVVAR = "CORRESPOND_DATA_DIR"
CONFIG_ENVVAR = "CORRESPOND_CONFIG"
#: How long a Keychain lookup may take before it counts as absent.
KEYCHAIN_TIMEOUT_S = 10


def _xdg_dir(envvar: str, posix_default: str, windows_envvar: str) -> Path:
    if sys.platform == "win32" and os.environ.get(windows_envvar):
        return Path(os.environ[windows_envvar])
    return Path(os.environ.get(envvar) or Path.home() / posix_default)


def config_path() -> Path:
    """``$CORRESPOND_CONFIG``, else ``~/.config/correspond/config.toml``."""
    if os.environ.get(CONFIG_ENVVAR):
        return Path(os.path.expanduser(os.environ[CONFIG_ENVVAR]))
    return _xdg_dir("XDG_CONFIG_HOME", ".config", "APPDATA") / APP_NAME / "config.toml"


def load_config(path: str | os.PathLike | None = None) -> dict:
    """The parsed config file, or ``{}`` when there is none. A broken file raises, naming it."""
    path = Path(path) if path is not None else config_path()
    if not path.is_file():
        return {}
    try:
        with open(path, "rb") as stream:
            return tomllib.load(stream)
    except tomllib.TOMLDecodeError as error:
        raise CorrespondError(f"config file {path} does not parse: {error}") from None


def channel_config(channel: str, *, config: Mapping[str, Any] | None = None) -> dict:
    """The ``[channel]`` table of the config file (``{}`` when absent)."""
    table = (load_config() if config is None else config).get(channel, {})
    if not isinstance(table, dict):
        raise CorrespondError(f"[{channel}] in the config file must be a table")
    return table


def data_dir(data_dir: str | os.PathLike | None = None) -> Path:
    """The data root: the argument, else ``$CORRESPOND_DATA_DIR``, else ``data_dir`` in the config, else ``~/.local/share/correspond``.

    The environment variable and the config value must be absolute paths: a relative one
    would put state wherever the current directory happens to be (a repository, say).

    >>> data_dir("state").is_absolute()
    True
    """
    if data_dir:
        return Path(os.path.abspath(os.path.expanduser(str(data_dir))))
    configured, origin = os.environ.get(DATA_DIR_ENVVAR), f"${DATA_DIR_ENVVAR}"
    if not configured:
        configured, origin = load_config().get("data_dir"), f"data_dir in {config_path()}"
    if configured:
        path = Path(os.path.expanduser(str(configured)))
        if not path.is_absolute():
            raise CorrespondError(
                f"{origin} must be an absolute path, got {configured!r}"
            )
        return path
    return _xdg_dir("XDG_DATA_HOME", ".local/share", "LOCALAPPDATA") / APP_NAME


def keychain_service(
    channel: str, key: str, *, config: Mapping[str, Any] | None = None
) -> str:
    """The macOS Keychain service a secret is looked up under.

    >>> keychain_service("telegram", "token", config={})
    'correspond-telegram-token'
    """
    table = channel_config(channel, config=config)
    return str(table.get(f"{key}_keychain_service") or f"correspond-{channel}-{key}")


def keychain_get(service: str, *, run: Callable[..., Any] = subprocess.run) -> str:
    """A generic password from the macOS Keychain, or ``""`` when absent, not on macOS, or slow."""
    if not keychain_available():
        return ""
    try:
        proc = run(
            ["security", "find-generic-password", "-s", service, "-w"],
            capture_output=True,
            text=True,
            timeout=KEYCHAIN_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def keychain_available() -> bool:
    """Whether a Keychain lookup can happen here (macOS with the ``security`` tool)."""
    return sys.platform == "darwin" and shutil.which("security") is not None


def mask(value: str | None) -> str:
    """Enough of a secret to recognise it, never enough to use it.

    >>> mask("example-topic-3f9a"), mask("short"), mask(None)
    ('ex…9a', '…', '(not set)')
    """
    if not value:
        return "(not set)"
    return f"{value[:2]}…{value[-2:]}" if len(value) >= 12 else "…"
