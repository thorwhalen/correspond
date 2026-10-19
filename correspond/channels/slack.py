"""Slack: messages formatted as Slack writes them, posted through an incoming webhook.

Reference: ``slack:<name>``, a name for one Slack channel (``#`` optional, lower-cased;
1-80 of ``a-z``, ``0-9``, ``-``, ``_`` and ``.``). What a name posts to is an **incoming
webhook**, which Slack ties to one channel when it is created:
``$CORRESPOND_SLACK_WEBHOOK_<NAME>`` (upper case, ``-`` and ``.`` as ``_``), else on macOS
the Keychain item ``correspond-slack-webhook-<name>``. The webhook URL is a credential: it
never appears in a reference, a plan or an error.

Two uses, one channel:

- **A draft the operator sends.** ``send(..., dry_run=True)`` needs nothing configured: the
  plan holds the text rendered as Slack ``mrkdwn`` (:func:`to_mrkdwn`), its length against
  Slack's limit, and says nothing was sent. That is the whole interaction when an agent
  writes the reply and a person pastes it, including on a workspace where no app will
  ever be installed.
- **A post.** With a webhook for the name, ``send`` posts the same text to its channel.

Slack's ``mrkdwn`` is not Markdown: ``*bold*``, ``_italic_``, ``~strike~``, ``<url|text>``
links, no headings, no tables, and ``&``, ``<`` and ``>`` escaped. :func:`to_mrkdwn`
converts the common Markdown into it, so a draft written as GitHub Markdown does not paste
as literal asterisks and pipes. Because every ``<`` in the text is escaped, a draft cannot
carry a Slack mention (``<!channel>``, ``<@U...>``) that would notify people.

Reading and listening need a Slack app and token (``slack-sdk``), and are not built yet.

>>> to_mrkdwn("**Fixed** in [#12](https://example.org/12) & shipped")
'*Fixed* in <https://example.org/12|#12> &amp; shipped'
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from typing import Any

from correspond import settings as _settings
from correspond.channels._http import urllib_http
from correspond.errors import ChannelError, InvalidRef, MissingRequirement, NotSupported
from correspond.model import (
    Audience,
    Capabilities,
    ConversationRef,
    Draft,
    Scope,
    SendResult,
    Support,
)

__all__ = ["MAX_TEXT_CHARS", "Slack", "to_mrkdwn", "webhook_env"]

NAME = "slack"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")
#: Slack truncates a message's text past this many characters.
MAX_TEXT_CHARS = 40_000
WEBHOOK_PREFIX = "https://hooks.slack.com/"
#: What Slack answers a webhook with, as correspond's error kinds.
_WEBHOOK_ERRORS = {
    400: "validation",
    403: "permission",
    404: "not_found",
    410: "not_found",
    429: "rate_limited",
}

_FENCE_RE = re.compile(r"^(```|~~~)[^\n]*\n(.*?)^\1[ \t]*$", re.M | re.S)
_CODE_RE = re.compile(r"`([^`\n]+)`")
_IMAGE_OR_LINK_RE = re.compile(r"!?\[([^\]\n]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_AUTOLINK_RE = re.compile(r"<(https?://[^>\s]+)>")
_HEADING_RE = re.compile(r"^#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$", re.M)
_BOLD_RE = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1")
_ITALIC_RE = re.compile(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])")
_STRIKE_RE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")
_BULLET_RE = re.compile(r"^([ \t]*)[-*+][ \t]+", re.M)
_RULE_RE = re.compile(r"^[ \t]*(?:-{3,}|\*{3,}|_{3,})[ \t]*$", re.M)
_TABLE_ROW_RE = re.compile(r"^[ \t]*\|.*\|[ \t]*$")
_SLOT = "\x00{}\x00"
_SLOT_RE = re.compile("\x00(\\d+)\x00")


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _tables_as_code(text: str) -> str:
    """Wrap each run of Markdown table rows in a code block: Slack has no tables."""
    out, table = [], []

    def flush():
        if table:
            out.extend(["```", *table, "```"])
            table.clear()

    for line in text.split("\n"):
        if _TABLE_ROW_RE.match(line):
            table.append(line)
        else:
            flush()
            out.append(line)
    flush()
    return "\n".join(out)


def to_mrkdwn(markdown: str) -> str:
    """Common Markdown as Slack ``mrkdwn``: bold, italic, strike, links, headings, lists, code, quotes, tables as code.

    >>> to_mrkdwn("# Release\\n- **new**: _faster_ export\\n- ~~old~~ gone")
    '*Release*\\n• *new*: _faster_ export\\n• ~old~ gone'
    >>> to_mrkdwn("> quoted <b> & `a < b`")
    '> quoted &lt;b&gt; &amp; `a &lt; b`'
    """
    slots: list[str] = []

    def keep(text: str) -> str:
        slots.append(text)
        return _SLOT.format(len(slots) - 1)

    text = _tables_as_code(markdown.replace("\r\n", "\n"))
    text = _FENCE_RE.sub(lambda m: keep(f"```\n{_escape(m.group(2))}```"), text)
    text = _CODE_RE.sub(lambda m: keep(f"`{_escape(m.group(1))}`"), text)
    text = _AUTOLINK_RE.sub(lambda m: keep(f"<{m.group(1)}>"), text)
    text = _IMAGE_OR_LINK_RE.sub(
        lambda m: keep(
            f"<{m.group(2)}|{_escape(m.group(1))}>" if m.group(1) else f"<{m.group(2)}>"
        ),
        text,
    )
    quotes = re.findall(r"^[ \t]*>", text, re.M)  # Slack quotes a line starting with ">"
    text = _escape(text)
    if quotes:
        text = re.sub(r"^([ \t]*)&gt;", r"\1>", text, flags=re.M)
    text = _RULE_RE.sub("──────────", text)
    text = _HEADING_RE.sub(lambda m: keep(f"*{m.group(1).strip('*_ ')}*"), text)
    text = _BOLD_RE.sub(lambda m: keep(f"*{m.group(2)}*"), text)
    text = _STRIKE_RE.sub(lambda m: keep(f"~{m.group(1)}~"), text)
    text = _ITALIC_RE.sub(lambda m: f"_{m.group(1)}_", text)
    text = _BULLET_RE.sub(r"\1• ", text)
    while _SLOT_RE.search(text):
        text = _SLOT_RE.sub(lambda m: slots[int(m.group(1))], text)
    return text


def webhook_env(name: str) -> str:
    """The environment variable holding one name's webhook URL.

    >>> webhook_env("team-alerts")
    'CORRESPOND_SLACK_WEBHOOK_TEAM_ALERTS'
    """
    return "CORRESPOND_SLACK_WEBHOOK_" + re.sub(r"[-.]", "_", name.upper())


def webhook_keychain_service(name: str) -> str:
    """The macOS Keychain item holding one name's webhook URL.

    >>> webhook_keychain_service("team-alerts")
    'correspond-slack-webhook-team-alerts'
    """
    return f"correspond-{NAME}-webhook-{name}"


class Slack:
    """Slack: a draft in Slack's own formatting, and a post through an incoming webhook."""

    name = NAME

    def __init__(
        self,
        *,
        http: Callable[..., Any] = urllib_http,
        keychain: Callable[[str], str] | None = None,
    ):
        self.http = http
        self._keychain = keychain

    @property
    def capabilities(self) -> Capabilities:
        """A dry run always; a post where a webhook is configured."""
        return Capabilities(
            channel=NAME,
            send=Support.PARTIAL,
            audience=Support.PARTIAL,
            initiate=Support.FULL,
            reply=Support.NONE,
            max_text_length=MAX_TEXT_CHARS,
            formats=("mrkdwn",),
            rate_limits=("about one message per second per webhook",),
            notes=(
                "a dry run renders the text as Slack mrkdwn (bold *x*, italic _x_, links <url|text>, no headings or tables) and needs nothing configured: it is the draft the operator can paste",
                "a real send posts through the incoming webhook configured for the name (CORRESPOND_SLACK_WEBHOOK_<NAME>, or the Keychain); a webhook posts to the one channel it was made for, as the app that owns it",
                "the text cannot mention anyone: every < is escaped, so <!channel> or <@U...> arrive as text",
                "a title is prepended as a bold line; threads and replies are not supported",
                "read and listen need a Slack app token (slack-sdk) and are not built",
            ),
        )

    def parse_ref(self, id: str) -> ConversationRef:
        """A channel's name, ``#`` optional, lower-cased."""
        name = (id or "").strip().lstrip("#").lower()
        if not NAME_RE.match(name):
            raise InvalidRef(
                f"slack references are slack:<name> (1-80 of a-z, 0-9, -, _ and .), never a webhook URL, not slack:{id}"
            )
        return ConversationRef(channel=NAME, id=name, kind="channel")

    def _webhook(self, name: str) -> str | None:
        url = os.environ.get(webhook_env(name), "").strip()
        if not url:
            url = (self._keychain or _settings.keychain_get)(
                webhook_keychain_service(name)
            ).strip()
        if url and not url.startswith(WEBHOOK_PREFIX):
            raise ChannelError(
                f"the webhook for slack:{name} is not a Slack incoming webhook (it must start with {WEBHOOK_PREFIX})",
                kind="validation",
            )
        return url or None

    def audience(self, ref: ConversationRef, *, draft: Draft | None = None) -> Audience:
        """The members of the webhook's channel, never listed; a shared channel reaches other workspaces."""
        return Audience(
            ref=ref.encoded,
            scope=Scope.GROUP,
            complete=False,
            classes=(
                "the members of the channel the webhook posts to (anyone in the workspace, for a public channel)",
                "guests and members of other organisations, when the channel is shared (Slack Connect)",
            ),
            durability=("copies_pushed", "archived_by_others", "edit_history_visible"),
            widening=("joiners_read_history", "forwarding", "visibility_flip"),
            evidence=(
                "a webhook posts to one channel, whose members correspond does not list",
            ),
        )

    def send(
        self, ref: ConversationRef, draft: Draft, *, dry_run: bool = False
    ) -> SendResult:
        """Render ``draft`` as mrkdwn; post it through the name's webhook unless ``dry_run``."""
        if draft.reply_to:
            raise NotSupported(
                "reply",
                NAME,
                alternatives=(
                    "a webhook cannot thread: post and quote the message you answer",
                ),
            )
        if draft.notify:
            raise NotSupported(
                "notify",
                NAME,
                alternatives=(
                    "a webhook's text cannot mention anyone; name them in the text",
                ),
            )
        body = to_mrkdwn(draft.text)
        text = (
            f"*{to_mrkdwn(' '.join(draft.title.split()))}*\n{body}"
            if draft.title
            else body
        )
        if len(text) > MAX_TEXT_CHARS:
            raise ChannelError(
                f"the rendered text is {len(text)} characters; Slack keeps {MAX_TEXT_CHARS}",
                kind="validation",
            )
        configured = bool(os.environ.get(webhook_env(ref.id), "").strip())
        plan: dict[str, Any] = {
            "action": "post through the channel's incoming webhook",
            "name": ref.id,
            "format": "mrkdwn",
            "text": text,
            "characters": len(text),
            "max_characters": MAX_TEXT_CHARS,
            "webhook": "configured (environment)"
            if configured
            else f"looked up when sending: {webhook_env(ref.id)}, else the Keychain item {webhook_keychain_service(ref.id)}",
        }
        if dry_run:
            return SendResult(
                ok=True, channel=NAME, conversation=ref.encoded, dry_run=True, plan=plan
            )
        url = self._webhook(ref.id)
        if url is None:
            raise MissingRequirement(
                NAME,
                f"no incoming webhook for slack:{ref.id}",
                fix=(
                    f"create one for the channel (Slack app settings, Incoming Webhooks) and set "
                    f"{webhook_env(ref.id)}, or store it in the Keychain as {webhook_keychain_service(ref.id)}; "
                    "without one, a dry run gives the text to paste"
                ),
                kind="validation",
            )
        reply = self.http(
            "POST",
            url,
            headers={"Content-Type": "application/json"},
            body=json.dumps({"text": text}).encode("utf-8"),
        )
        if reply.status != 200:
            answer = reply.body.decode("utf-8", "replace").strip()[:200]
            kind = _WEBHOOK_ERRORS.get(
                reply.status, "unavailable" if reply.status >= 500 else "validation"
            )
            raise ChannelError(
                f"Slack refused the post to slack:{ref.id} ({reply.status}: {answer or 'no reason given'})",
                kind=kind,
                retryable=kind == "rate_limited" or reply.status >= 500,
            )
        return SendResult(
            ok=True,
            channel=NAME,
            conversation=ref.encoded,
            plan={**plan, "webhook": "used"},
        )
