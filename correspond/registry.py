"""Which channels exist: the built-in channel table, the registry built from it, and what each channel needs.

The table (:data:`CHANNELS`) is the one place a channel is described: its adapter's
factory, the optional modules and extra it needs, the binaries and platforms, and every
setting with its environment variable and where to get it. Adapters read settings through
:func:`value` and :func:`require`, so a variable name is written once.

The process registry (:func:`channels`) is an ``xdol.Registry`` built from the table on
first use: a channel registers only when its optional modules import, and its adapter is
constructed only when first asked for. :func:`check_requirements` explains what a channel
is missing, without printing a secret.

>>> info("telegram").setting("token").env
'TELEGRAM_BOT_TOKEN'
>>> info("discord").planned
'https://github.com/thorwhalen/correspond/issues/2'
"""

from __future__ import annotations

import functools
import importlib
import importlib.util
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping, MutableMapping
from dataclasses import dataclass
from typing import Any

from correspond import settings as _settings
from correspond.errors import MissingRequirement, UnknownChannel

__all__ = [
    "CHANNELS",
    "ChannelInfo",
    "Setting",
    "build_registry",
    "channels",
    "check_requirements",
    "info",
    "load_factory",
    "register_channel",
    "require",
    "resolve",
    "unregister_channel",
    "value",
]

ISSUES = "https://github.com/thorwhalen/correspond/issues"


@dataclass(frozen=True, kw_only=True)
class Setting:
    """One value a channel reads: its config key, its environment variable, what it is and how to get it.

    A ``secret`` is never read from the config file: it comes from ``env``, else (on macOS)
    the Keychain.
    """

    key: str
    env: str
    what: str
    where: str = ""
    secret: bool = False
    required: bool = False
    default: str | None = None


@dataclass(frozen=True, kw_only=True)
class ChannelInfo:
    """Everything correspond knows about a channel before constructing its adapter."""

    name: str
    summary: str
    factory: str = ""
    modules: tuple[str, ...] = ()
    extra: str | None = None
    binaries: tuple[tuple[str, ...], ...] = ()
    platforms: tuple[str, ...] = ()
    settings: tuple[Setting, ...] = ()
    notes: tuple[str, ...] = ()
    planned: str | None = None

    def setting(self, key: str) -> Setting:
        """The setting named ``key``."""
        for setting in self.settings:
            if setting.key == key:
                return setting
        raise KeyError(f"{self.name} has no setting {key!r}")


CHANNELS: tuple[ChannelInfo, ...] = (
    ChannelInfo(
        name="github",
        summary="GitHub issues, pull request conversations and discussions, through the gh CLI (the machine's gh login; correspond holds no token)",
        factory="correspond.channels.github:GitHub",
        binaries=(("gh",),),
        settings=(
            Setting(
                key="webhook_secret",
                env="GITHUB_WEBHOOK_SECRET",
                what="the webhook secret, used only to verify deliveries",
                where="the secret set on the repository or organization webhook (Settings, Webhooks)",
                secret=True,
            ),
        ),
        notes=(
            "log in once with `gh auth login` (https://cli.github.com/); check with `gh auth status`",
        ),
    ),
    ChannelInfo(
        name="email",
        summary="Email: IMAP to read and listen, SMTP to send, with the standard library",
        factory="correspond.channels.mail:Email",
        settings=(
            Setting(
                key="imap_host",
                env="CORRESPOND_EMAIL_IMAP_HOST",
                what="the IMAP server",
                where="your mail provider's IMAP settings",
                required=True,
            ),
            Setting(
                key="imap_port",
                env="CORRESPOND_EMAIL_IMAP_PORT",
                what="the IMAP port (TLS)",
                default="993",
            ),
            Setting(
                key="smtp_host",
                env="CORRESPOND_EMAIL_SMTP_HOST",
                what="the SMTP server",
                where="your mail provider's SMTP settings",
                required=True,
            ),
            Setting(
                key="smtp_port",
                env="CORRESPOND_EMAIL_SMTP_PORT",
                what="the SMTP port: 465 for TLS, 587 for STARTTLS",
                default="465",
            ),
            Setting(
                key="user",
                env="CORRESPOND_EMAIL_USER",
                what="the mailbox login, usually its address",
                required=True,
            ),
            Setting(
                key="password",
                env="CORRESPOND_EMAIL_PASSWORD",
                what="the mailbox password, preferably an app password",
                where="your mail provider's app-password page (for Gmail: https://myaccount.google.com/apppasswords)",
                secret=True,
                required=True,
            ),
            Setting(
                key="from_address",
                env="CORRESPOND_EMAIL_FROM",
                what="the From address of sends (default: the login)",
            ),
            Setting(
                key="folder",
                env="CORRESPOND_EMAIL_FOLDER",
                what="the folder read and listened to",
                default="INBOX",
            ),
            Setting(
                key="trusted_authserv_ids",
                env="CORRESPOND_EMAIL_TRUSTED_AUTHSERV_IDS",
                what="comma-separated authserv-ids of your own receiving server; only their Authentication-Results can make a sender `domain`",
                where="the topmost Authentication-Results header of a message your provider delivered (for Gmail: mx.google.com)",
            ),
        ),
    ),
    ChannelInfo(
        name="ntfy",
        summary="Push notifications through an ntfy server (send only)",
        factory="correspond.channels.ntfy:Ntfy",
        settings=(
            Setting(
                key="url",
                env="NTFY_URL",
                what="the ntfy server",
                default="https://ntfy.sh",
            ),
            Setting(
                key="topic",
                env="NTFY_TOPIC",
                what="the default topic, used by `ntfy:` with no topic",
                where="any long, hard-to-guess string; subscribe to it in the ntfy app (https://ntfy.sh)",
                secret=True,
            ),
            Setting(
                key="token",
                env="NTFY_TOKEN",
                what="an access token, for a server that requires one",
                where="your ntfy server's account page",
                secret=True,
            ),
            Setting(
                key="topic_remote",
                env="CORRESPOND_NTFY_TOPIC_REMOTE",
                what="an ssh host asked for the default topic when neither the environment nor the Keychain has it",
            ),
            Setting(
                key="topic_remote_file",
                env="CORRESPOND_NTFY_TOPIC_REMOTE_FILE",
                what="the file on that host holding a NTFY_TOPIC=... line",
            ),
        ),
        notes=(
            "anyone who knows an unauthenticated topic can publish to it and read it: treat it as a secret",
        ),
    ),
    ChannelInfo(
        name="macos",
        summary="Notification Centre banners on this Mac (send only)",
        factory="correspond.channels.macos:MacOS",
        binaries=(("terminal-notifier", "osascript"),),
        platforms=("darwin",),
        notes=(
            "terminal-notifier (`brew install terminal-notifier`) keeps bodies intact; osascript is the fallback",
        ),
    ),
    ChannelInfo(
        name="telegram",
        summary="A Telegram bot over the Bot API: listen with getUpdates, read what was logged, send, edit, react",
        factory="correspond.channels.telegram:Telegram",
        settings=(
            Setting(
                key="token",
                env="TELEGRAM_BOT_TOKEN",
                what="the bot token",
                where="create a bot with BotFather in Telegram (https://core.telegram.org/bots/tutorial)",
                secret=True,
                required=True,
            ),
            Setting(
                key="api_url",
                env="CORRESPOND_TELEGRAM_API_URL",
                what="the Bot API server",
                default="https://api.telegram.org",
            ),
        ),
        notes=(
            "a bot can write only to chats that wrote to it first, or groups it was added to",
            "privacy mode (on by default) hides group messages that do not address the bot",
        ),
    ),
    ChannelInfo(
        name="webinbox",
        summary="Reports posted from web pages to correspond's ASGI collector, and a reader over what it stored",
        factory="correspond.channels.webinbox:WebInbox",
        settings=(
            Setting(
                key="sites",
                env="CORRESPOND_WEBINBOX_SITES",
                what="comma-separated site names the collector accepts",
            ),
            Setting(
                key="origins",
                env="CORRESPOND_WEBINBOX_ORIGINS",
                what="comma-separated page origins allowed to post, e.g. https://app.example.org",
            ),
            Setting(
                key="secret",
                env="CORRESPOND_WEBINBOX_SECRET",
                what="the HMAC key shared with the host application's server, which signs its logged-in user (a single site; several sites need CORRESPOND_WEBINBOX_SECRET_<SITE> each)",
                where='generate one with: python -c "import secrets; print(secrets.token_hex(32))"',
                secret=True,
            ),
            Setting(
                key="max_age_s",
                env="CORRESPOND_WEBINBOX_MAX_AGE_S",
                what="how long a signed identity stays valid, in seconds",
                default="86400",
            ),
            Setting(
                key="rate_per_minute",
                env="CORRESPOND_WEBINBOX_RATE_PER_MINUTE",
                what="reports accepted per client per minute, per site",
                default="10",
            ),
            Setting(
                key="burst",
                env="CORRESPOND_WEBINBOX_BURST",
                what="reports a client may send in a burst",
                default="5",
            ),
            Setting(
                key="max_body_bytes",
                env="CORRESPOND_WEBINBOX_MAX_BODY_BYTES",
                what="the largest request accepted, attachments included",
                default="5000000",
            ),
            Setting(
                key="trusted_proxies",
                env="CORRESPOND_WEBINBOX_TRUSTED_PROXIES",
                what="how many reverse proxies of yours stand in front of the collector; the address rate-limited is read that many hops from the right of X-Forwarded-For (0: the connecting address)",
                default="0",
            ),
        ),
        notes=(
            "serve the collector on localhost behind your own server, e.g. `uvicorn --factory correspond.channels.webinbox:app_from_env --host 127.0.0.1`",
            "behind a reverse proxy, set CORRESPOND_WEBINBOX_TRUSTED_PROXIES (1 for one proxy), or every visitor shares one rate limit",
            "several sites on one collector need a secret each, CORRESPOND_WEBINBOX_SECRET_<SITE> (upper case, - as _), so no site's server can sign for another",
        ),
    ),
    ChannelInfo(
        name="discord",
        summary="Discord: read and post over REST, listen on the gateway",
        extra="discord",
        planned=f"{ISSUES}/2",
    ),
    ChannelInfo(
        name="slack",
        summary="Slack: Socket Mode or the Events API",
        extra="slack",
        planned=f"{ISSUES}/3",
    ),
    ChannelInfo(
        name="signal",
        summary="Signal through signal-cli-rest-api",
        extra="signal",
        planned=f"{ISSUES}/4",
    ),
    ChannelInfo(
        name="apprise",
        summary="Apprise as a send-only writer for about 155 notification services",
        extra="apprise",
        planned=f"{ISSUES}/5",
    ),
)

_INFO: dict[str, ChannelInfo] = {c.name: c for c in CHANNELS}


def info(channel: str) -> ChannelInfo:
    """The table entry for a built-in channel."""
    try:
        return _INFO[channel]
    except KeyError:
        raise UnknownChannel(channel, known=_INFO) from None


# ------------------------------------------------------------------------ settings


def resolve(
    channel: str,
    setting: Setting,
    *,
    config: Mapping[str, Any] | None = None,
    run: Callable[..., Any] = subprocess.run,
    keychain: bool = True,
) -> tuple[str | None, str]:
    """``(value, source)``; source is ``env``, ``keychain``, ``config``, ``default`` or ``missing``.

    ``keychain=False`` skips the Keychain, so nothing runs: what a dry run does.
    """
    from_env = os.environ.get(setting.env, "").strip()
    if from_env:
        return from_env, "env"
    if setting.secret:
        if keychain:
            service = _settings.keychain_service(channel, setting.key, config=config)
            from_keychain = _settings.keychain_get(service, run=run)
            if from_keychain:
                return from_keychain, "keychain"
    else:
        configured = _settings.channel_config(channel, config=config).get(setting.key)
        if configured not in (None, "", []):
            if isinstance(configured, list):
                return ",".join(str(v) for v in configured), "config"
            return str(configured), "config"
    if setting.default is not None:
        return setting.default, "default"
    return None, "missing"


def _fix(
    channel: str, setting: Setting, *, config: Mapping[str, Any] | None = None
) -> str:
    if setting.secret:
        how = f"set {setting.env}"
        if sys.platform == "darwin":
            service = _settings.keychain_service(channel, setting.key, config=config)
            how += f", or store it in the Keychain: security add-generic-password -s {service} -a correspond -w"
    else:
        how = f"set {setting.env}, or {setting.key} under [{channel}] in the config file"
    return f"{how}; {setting.where}" if setting.where else how


def value(
    channel: str,
    key: str,
    *,
    config: Mapping[str, Any] | None = None,
    run: Callable[..., Any] = subprocess.run,
    keychain: bool = True,
) -> str | None:
    """A channel setting's value, or ``None`` when it is not set and has no default (``keychain=False``: skip the Keychain)."""
    return resolve(
        channel, info(channel).setting(key), config=config, run=run, keychain=keychain
    )[0]


def require(
    channel: str,
    key: str,
    *,
    config: Mapping[str, Any] | None = None,
    run: Callable[..., Any] = subprocess.run,
) -> str:
    """A channel setting's value, or :class:`~correspond.errors.MissingRequirement` saying how to set it."""
    setting = info(channel).setting(key)
    found = resolve(channel, setting, config=config, run=run)[0]
    if found is None:
        raise MissingRequirement(
            channel,
            f"{setting.what} is not set",
            fix=_fix(channel, setting, config=config),
            kind="auth" if setting.secret else "unavailable",
        )
    return found


# ------------------------------------------------------------------------ registry


def load_factory(ref: str) -> Any:
    """The object a ``module:attribute`` reference names."""
    module_name, _, attribute = ref.partition(":")
    return getattr(importlib.import_module(module_name), attribute)


def _construct(ref: str) -> Any:
    return load_factory(ref)()


def _importable(info_: ChannelInfo) -> bool:
    return all(importlib.util.find_spec(module) is not None for module in info_.modules)


def build_registry(
    infos: Iterable[ChannelInfo] = CHANNELS, *, name: str = "correspond channels"
) -> MutableMapping[str, Any]:
    """A fresh ``xdol.Registry`` with a lazy entry for every built channel whose optional modules import."""
    from xdol import Registry

    registry = Registry(name=name)
    for entry in infos:
        if entry.planned or not entry.factory or not _importable(entry):
            continue
        registry.register_lazy(entry.name, functools.partial(_construct, entry.factory))
    return registry


@functools.cache
def channels() -> MutableMapping[str, Any]:
    """The process registry: built on first use, then shared."""
    return build_registry()


def register_channel(
    adapter: Any,
    *,
    name: str | None = None,
    replace: bool = False,
    registry: MutableMapping[str, Any] | None = None,
) -> Any:
    """Add an adapter (tests, or a channel defined outside correspond). ``replace`` swaps out an existing one."""
    registry = channels() if registry is None else registry
    name = name or adapter.name
    if replace and name in registry:
        del registry[name]
    if hasattr(registry, "register"):
        return registry.register(name, adapter)
    if name in registry:
        raise KeyError(f"{name!r} is already registered")
    registry[name] = adapter
    return adapter


def unregister_channel(
    name: str, *, registry: MutableMapping[str, Any] | None = None
) -> None:
    """Remove a channel from the registry."""
    registry = channels() if registry is None else registry
    del registry[name]


def check_requirements(
    channel: str,
    *,
    registry: Mapping[str, Any] | None = None,
    config: Mapping[str, Any] | None = None,
    run: Callable[..., Any] = subprocess.run,
) -> dict:
    """What a channel needs and what is missing: the install command, binaries, platform, and every setting's source. Never a secret's value."""
    registry = channels() if registry is None else registry
    if channel not in _INFO:
        registered = channel in registry
        summary = (
            f"{channel} is registered from outside correspond; it declares no requirements"
            if registered
            else f"unknown channel {channel!r}; built in: {', '.join(sorted(_INFO))}"
        )
        return {
            "ok": registered,
            "channel": channel,
            "registered": registered,
            "problems": [] if registered else [summary],
            "settings": [],
            "summary": summary,
            "text": summary,
        }
    entry = _INFO[channel]
    if entry.planned:
        summary = f"{channel} is not built yet; it is tracked at {entry.planned}"
        return {
            "ok": False,
            "channel": channel,
            "registered": False,
            "planned": entry.planned,
            "problems": [summary],
            "settings": [],
            "summary": summary,
            "text": f"{channel}: {entry.summary}\n{summary}",
        }

    problems: list[str] = []
    warnings: list[str] = []
    install = None
    missing_modules = [m for m in entry.modules if importlib.util.find_spec(m) is None]
    if missing_modules:
        install = f'pip install "correspond[{entry.extra}]"'
        problems.append(f"missing Python modules {', '.join(missing_modules)}: {install}")
    for alternatives in entry.binaries:
        if not any(shutil.which(binary) for binary in alternatives):
            problems.append(f"needs {' or '.join(alternatives)} on PATH")
    if entry.platforms and sys.platform not in entry.platforms:
        problems.append(
            f"works only on {', '.join(entry.platforms)} (this is {sys.platform})"
        )

    table = _settings.channel_config(channel, config=config)
    rows = []
    for setting in entry.settings:
        found, source = resolve(channel, setting, config=config, run=run)
        rows.append(
            {
                "key": setting.key,
                "env": setting.env,
                "what": setting.what,
                "secret": setting.secret,
                "required": setting.required,
                "source": source,
            }
        )
        if setting.secret and setting.key in table:
            warnings.append(
                f"{setting.key} under [{channel}] in the config file is ignored: a secret comes from {setting.env} or the Keychain"
            )
        if setting.required and found is None:
            problems.append(
                f"{setting.what} is not set: {_fix(channel, setting, config=config)}"
            )

    lines = [f"{channel}: {entry.summary}"]
    lines += [f"problem: {p}" for p in problems]
    lines += [f"warning: {w}" for w in warnings]
    lines += [
        f"  {r['env']:<40} {r['source']:<9} {'(required) ' if r['required'] else ''}{r['what']}"
        for r in rows
    ]
    lines += [f"note: {n}" for n in entry.notes]
    summary = (
        f"{channel} is ready"
        if not problems
        else f"{channel}: {len(problems)} problem(s)"
    )
    return {
        "ok": not problems,
        "channel": channel,
        "registered": channel in registry,
        "install": install,
        "problems": problems,
        "warnings": warnings,
        "settings": rows,
        "notes": list(entry.notes),
        "summary": summary,
        "text": "\n".join(lines),
    }
