"""Apprise: send-only notifications to the long tail of services (about 155), each named by an alias.

References: ``apprise:<alias>``. A reference never holds an Apprise URL, because the URL
carries the service's credentials (``discord://webhook_id/webhook_token``,
``tgram://bot_token/chat_id``...). The alias is resolved, in this order:

1. ``$CORRESPOND_APPRISE_URL_<ALIAS>`` (upper case, ``-`` as ``_``);
2. on macOS, the Keychain item ``correspond-apprise-url-<alias>``;
3. the Apprise configuration file named by ``config`` in the ``[apprise]`` table (or
   ``$CORRESPOND_APPRISE_CONFIG``), where the alias is an Apprise **tag**: every URL in the
   file carrying that tag is notified (see the configuration-file documentation linked from https://pypi.org/project/apprise/).

A dry run reads only the environment and the config file: a Keychain alias is looked up when
sending, and the plan says so. The plan names the service (the URL's scheme), never the URL.

Needs the ``apprise`` extra: ``pip install "correspond[apprise]"``. It is imported when a
message is sent, not when the channel is listed.

>>> Apprise().parse_ref("team-alerts").kind
'alias'
"""

from __future__ import annotations

import importlib
import os
import re
from collections.abc import Callable
from typing import Any

from correspond import settings as _settings
from correspond.errors import ChannelError, InvalidRef, MissingRequirement
from correspond.model import (
    DURABILITY,
    WIDENING,
    Audience,
    Capabilities,
    ConversationRef,
    Draft,
    Scope,
    SendResult,
    Support,
)
from correspond.registry import value

__all__ = ["Apprise", "alias_env"]

NAME = "apprise"
ALIAS_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def alias_env(alias: str) -> str:
    """The environment variable holding one alias's Apprise URL.

    >>> alias_env("team-alerts")
    'CORRESPOND_APPRISE_URL_TEAM_ALERTS'
    """
    return "CORRESPOND_APPRISE_URL_" + alias.upper().replace("-", "_")


def alias_keychain_service(alias: str) -> str:
    """The macOS Keychain item holding one alias's Apprise URL.

    >>> alias_keychain_service("team-alerts")
    'correspond-apprise-url-team-alerts'
    """
    return f"correspond-{NAME}-url-{alias}"


def _service(url: str) -> str:
    """The service an Apprise URL names: its scheme, and nothing that could be a credential.

    >>> _service("discord://1234/secret-token")
    'discord'
    >>> _service("not a url")
    '(unrecognised)'
    """
    scheme, sep, _ = url.partition("://")
    return scheme.lower() if sep and re.fullmatch(r"[A-Za-z][A-Za-z0-9+.-]*", scheme) else "(unrecognised)"


def _untagged(alias: str, config: str | None) -> MissingRequirement:
    return MissingRequirement(
        NAME,
        f"no URL in {config} is tagged {alias!r}",
        fix=f"tag a URL {alias!r} in that file, or set {alias_env(alias)}",
        kind="validation",
    )


def _import_apprise() -> Any:
    try:
        return importlib.import_module("apprise")
    except ImportError as error:
        raise MissingRequirement(
            NAME,
            "the apprise package is not installed",
            fix='pip install "correspond[apprise]"',
        ) from error


class Apprise:
    """Apprise URLs and tags, behind aliases: send only."""

    name = NAME

    def __init__(
        self,
        *,
        apprise: Callable[[], Any] = _import_apprise,
        keychain: Callable[[str], str] | None = None,
        keychain_available: Callable[[], bool] | None = None,
    ):
        self._apprise = apprise
        # Resolved when used, so the settings module stays the one place a
        # Keychain lookup is made (and can be replaced).
        self._keychain = keychain
        self._keychain_available = keychain_available

    def _keychain_get(self, service: str) -> str:
        return (self._keychain or _settings.keychain_get)(service)

    def _has_keychain(self) -> bool:
        return (self._keychain_available or _settings.keychain_available)()

    @property
    def capabilities(self) -> Capabilities:
        """Send only, with a title; who reads depends on the service behind each alias."""
        return Capabilities(
            channel=NAME,
            send=Support.FULL,
            audience=Support.PARTIAL,
            initiate=Support.FULL,
            formats=("plain",),
            notes=(
                "send only: no read, listen, edit or sender identity",
                "apprise:<alias> resolves to a URL in CORRESPOND_APPRISE_URL_<ALIAS> or the Keychain, else a tag in the Apprise config file; a reference never holds the URL, which carries credentials",
                "audience is public and incomplete: who reads depends on the service behind the alias, which correspond does not ask",
                "a draft's priority is not sent: Apprise services take one, if at all, in their URL",
                "a dry run reads only the environment and the config file",
            ),
        )

    def audience(self, ref: ConversationRef, *, draft: Draft | None = None) -> Audience:
        """Public and incomplete: Apprise reaches anything from one phone to a public channel, and nothing here can tell which.

        Nothing is resolved or asked: not the URL, not the service.
        """
        return Audience(
            ref=ref.encoded,
            scope=Scope.PUBLIC,
            complete=False,
            classes=(
                "whoever the service behind the alias delivers to",
                "whoever runs that service",
            ),
            external=None,
            retractable=False,
            durability=DURABILITY,
            widening=WIDENING,
            evidence=(
                f"alias {ref.id!r} names an Apprise target; the service behind it was not asked",
                "Apprise reaches anything from one device to a public channel, so this is treated as public",
            ),
        )

    def parse_ref(self, id: str) -> ConversationRef:
        """An alias: 1-64 letters, digits, ``-`` or ``_``."""
        if not ALIAS_RE.match(id or ""):
            raise InvalidRef(
                f"apprise references name an alias (1-64 letters, digits, '-' or '_'), never a URL, not {id!r}"
            )
        return ConversationRef(channel=NAME, id=id, kind="alias")

    def _target(self, alias: str, *, dry_run: bool) -> tuple[str | None, str | None, str]:
        """``(url, config_path, source)`` for an alias; neither found is ``(None, None, ...)``."""
        url = os.environ.get(alias_env(alias), "").strip()
        if url:
            return url, None, "env"
        keychain = self._has_keychain()
        if keychain and not dry_run:
            url = self._keychain_get(alias_keychain_service(alias)).strip()
            if url:
                return url, None, "keychain"
        config = value(NAME, "config")
        if config:
            return None, os.path.expanduser(config), "config"
        if keychain and dry_run:
            return None, None, f"looked up when sending, in the Keychain ({alias_keychain_service(alias)})"
        return None, None, "missing"

    def send(
        self, ref: ConversationRef, draft: Draft, *, dry_run: bool = False
    ) -> SendResult:
        """Notify every service behind the alias with ``draft.text`` (and its title)."""
        alias = ref.id
        url, config, source = self._target(alias, dry_run=dry_run)
        if url is None and config is None and (source == "missing" or not dry_run):
            raise MissingRequirement(
                NAME,
                f"alias {alias!r} names no Apprise URL and there is no Apprise config file to find it in as a tag",
                fix=(
                    f"set {alias_env(alias)} to the Apprise URL, or store it in the Keychain as "
                    f"{alias_keychain_service(alias)}, or set config under [apprise] to an Apprise "
                    f"config file that tags a URL {alias!r}"
                ),
                kind="validation",
            )
        plan: dict[str, Any] = {
            "action": "notify",
            "alias": alias,
            "source": source,
            "title": draft.title,
            "text": draft.text,
        }
        if url is not None:
            plan["service"] = _service(url)
        elif config is not None:
            plan["config"] = config
            plan["tag"] = alias
        else:
            plan["looked_up_when_sending"] = ["url"]
        if draft.priority:
            plan["priority_not_sent"] = draft.priority
        if dry_run:
            return SendResult(
                ok=True, channel=NAME, conversation=ref.encoded, dry_run=True, plan=plan
            )

        apprise = self._apprise()
        notifier = apprise.Apprise()
        if url is not None:
            if not notifier.add(url):
                raise ChannelError(
                    f"apprise does not recognise the URL behind alias {alias!r} (service {_service(url)}); check it with `apprise --dry-run`",
                    kind="validation",
                )
            tag = None
        else:
            book = apprise.AppriseConfig()
            if not book.add(config):
                raise ChannelError(
                    f"apprise could not read its config file {config}", kind="validation"
                )
            notifier.add(book)
            tag = alias
            # Apprise answers an unmatched tag with False, the same answer as a
            # failed delivery; ask first, so "nothing is tagged" is said as such.
            if not any(True for _ in notifier.find(tag=tag)):
                raise _untagged(alias, config)
        sent = notifier.notify(body=draft.text, title=draft.title or "", tag=tag)
        if sent is None:  # what some Apprise versions answer for an unmatched tag
            raise _untagged(alias, config)
        if not sent:
            # Apprise reports False when ANY service behind the alias failed, so
            # others may already have the message: a blind retry would repeat it.
            raise ChannelError(
                f"apprise could not deliver to every service behind alias {alias!r}; some may have received it, so check before sending again",
                kind="unconfirmed",
            )
        return SendResult(ok=True, channel=NAME, conversation=ref.encoded, plan=plan)
