"""ntfy: push notifications to a phone or a desktop through an ntfy server (send only).

References: ``ntfy:<topic>``, or ``ntfy:`` for the default topic, which is resolved in this
order: ``$NTFY_TOPIC``; the macOS Keychain (service ``topic_keychain_service`` in the
``[ntfy]`` config table, default ``correspond-ntfy-topic``); an ssh host named by
``topic_remote``, asked for the ``NTFY_TOPIC=...`` line of ``topic_remote_file``. A dry run
reads only the environment and the config file (no Keychain, no remote host: those are looked
up when sending), and a plan shows a topic masked.

A topic is a bearer secret: anyone who knows an unauthenticated one can publish to it and
read it. Prefer ``ntfy:`` to writing a topic into a reference that ends up in logs.

>>> Ntfy().parse_ref("").kind
'topic'
"""

from __future__ import annotations

import re
import shlex
import subprocess
from collections.abc import Callable
from email.header import Header
from typing import Any

from correspond.channels._http import classify, urllib_http
from correspond.errors import ChannelError, InvalidRef, MissingRequirement
from correspond.model import (
    Account,
    Capabilities,
    ConversationRef,
    Draft,
    SendResult,
    Support,
)
from correspond.registry import info, resolve, value
from correspond.settings import keychain_available, keychain_service, mask

__all__ = ["PRIORITY", "Ntfy"]

NAME = "ntfy"
TOPIC_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
HOST_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._@-]{0,252}$")
#: Draft priorities as ntfy's 1-5 scale.
PRIORITY = {"low": "2", "normal": "3", "high": "4", "urgent": "5"}
MAX_TITLE_CHARS = 250
REMOTE_TIMEOUT_S = 30


def _header(text: str) -> str:
    """A header value: one line, RFC 2047-encoded when it is not ASCII."""
    flat = " ".join(text.split())
    if flat.isascii():
        return flat
    return " ".join(Header(flat, "utf-8", maxlinelen=10_000).encode().split())


class Ntfy:
    """An ntfy server: publish to a topic."""

    name = NAME

    def __init__(
        self,
        *,
        http: Callable[..., Any] = urllib_http,
        run: Callable[..., Any] = subprocess.run,
    ):
        self.http = http
        self.run = run

    @property
    def capabilities(self) -> Capabilities:
        """Send only, with priorities."""
        return Capabilities(
            channel=NAME,
            send=Support.FULL,
            initiate=Support.FULL,
            priority=Support.FULL,
            max_title_length=MAX_TITLE_CHARS,
            formats=("plain",),
            notes=(
                "send only: no read, listen or sender identity",
                "the server turns a message over 4,096 bytes into an attachment",
                "ntfy: (no topic) uses NTFY_TOPIC, the Keychain, or topic_remote",
                "a dry run reads only the environment and the config file",
            ),
        )

    def parse_ref(self, id: str) -> ConversationRef:
        """A topic, or nothing for the default topic."""
        if id and not TOPIC_RE.match(id):
            raise InvalidRef(
                f"ntfy topics are 1-64 letters, digits, '-' or '_' (or ntfy: for the default topic), not {id!r}"
            )
        return ConversationRef(channel=NAME, id=id, kind="topic")

    def _default_topic(self, *, dry_run: bool) -> tuple[str | None, str]:
        """The default topic and where it came from; a dry run reads only the environment and says where it would look next."""
        setting = info(NAME).setting("topic")
        found, source = resolve(NAME, setting, run=self.run, keychain=not dry_run)
        if found:
            return found, source
        host, path = value(NAME, "topic_remote"), value(NAME, "topic_remote_file")
        if not dry_run:
            if host and path:
                return self._remote_topic(host, path), "remote"
            return None, "missing"
        later = []
        if keychain_available():
            later.append(f"the Keychain ({keychain_service(NAME, 'topic')})")
        if host and path:
            later.append(f"the host {host}")
        if not later:
            return None, "missing"
        return None, "looked up when sending, in " + ", then ".join(later)

    def _remote_topic(self, host: str, path: str) -> str | None:
        if not HOST_RE.match(host):
            raise ChannelError(
                f"topic_remote {host!r} is not a host name", kind="validation"
            )
        command = f"grep -h '^NTFY_TOPIC=' {shlex.quote(path)}"
        try:
            proc = self.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, command],
                capture_output=True,
                text=True,
                timeout=REMOTE_TIMEOUT_S,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        for line in (proc.stdout or "").splitlines():
            if line.startswith("NTFY_TOPIC="):
                return line.split("=", 1)[1].strip().strip("'\"") or None
        return None

    def send(
        self, ref: ConversationRef, draft: Draft, *, dry_run: bool = False
    ) -> SendResult:
        """Publish ``draft.text`` (with its title and priority) to the topic."""
        server = (value(NAME, "url") or "https://ntfy.sh").rstrip("/")
        if ref.id:
            topic, source = ref.id, "reference"
        else:
            topic, source = self._default_topic(dry_run=dry_run)
        if topic is not None and not TOPIC_RE.match(topic):
            raise ChannelError(
                f"the default topic (from {source}) is not a valid ntfy topic",
                kind="validation",
            )
        if topic is None and (source == "missing" or not dry_run):
            raise MissingRequirement(
                NAME,
                "there is no topic: the reference names none and no default topic was found",
                fix="send to ntfy:<topic>, or set NTFY_TOPIC, or set topic_remote and topic_remote_file under [ntfy]",
                kind="validation",
            )
        token = value(NAME, "token", run=self.run, keychain=not dry_run)
        headers = {"Content-Type": "text/plain; charset=utf-8"}
        if draft.title:
            headers["Title"] = _header(draft.title)
        if draft.priority:
            headers["Priority"] = PRIORITY[draft.priority]
        if token:
            headers["Authorization"] = f"Bearer {token}"
        plan = {
            "action": "publish",
            "server": server,
            "topic": mask(topic) if topic else "(not resolved yet)",
            "topic_source": source,
            "title": draft.title,
            "priority": draft.priority,
            "authenticated": True
            if token
            else (
                "checked when sending (Keychain)"
                if dry_run and keychain_available()
                else False
            ),
            "text": draft.text,
        }
        if dry_run:
            return SendResult(
                ok=True, channel=NAME, conversation=ref.encoded, dry_run=True, plan=plan
            )
        response = self.http(
            "POST", f"{server}/{topic}", headers=headers, body=draft.text.encode("utf-8")
        )
        if response.status >= 300:
            data = response.json()
            detail = data.get("error") if isinstance(data, dict) else None
            raise classify(
                response.status,
                f"ntfy refused the message ({response.status}{': ' + str(detail) if detail else ''})",
                response.headers,
            )
        data = response.json() or {}
        return SendResult(
            ok=True,
            channel=NAME,
            conversation=ref.encoded,
            message_id=str(data["id"])
            if isinstance(data, dict) and data.get("id")
            else None,
            account=Account(channel=NAME, id=server, acts_as="service"),
            plan=plan,
        )
