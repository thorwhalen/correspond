"""Slack: drafts in Slack's own formatting; reading, polling and posting with a token; posting through a webhook.

Reference: ``slack:<channel>``, a Slack channel by its name (``#`` optional, lower-cased;
1-80 of ``a-z``, ``0-9``, ``-`` and ``_``) or by its id (``C024BE91L``, kept as Slack
writes it). A post goes through the channel's **incoming webhook**, which Slack ties to one
channel when it is created: ``$CORRESPOND_SLACK_WEBHOOK_<CHANNEL>`` (upper case, ``-`` as
``_`` and ``_`` as ``__``, so no two channels share a variable), else on macOS the Keychain
item ``correspond-slack-webhook-<channel>``. The webhook URL is a credential: it never
appears in a reference, a plan or an error.

Three uses, one channel, each needing a little more set up:

- **A draft the operator sends.** ``send(..., dry_run=True)`` needs nothing configured: the
  plan holds the text rendered as Slack ``mrkdwn`` (:func:`to_mrkdwn`), its length against
  Slack's limit, and says nothing was sent. That is the whole interaction when an agent
  writes the reply and a person pastes it, including on a workspace where no app will
  ever be installed.
- **A post through a webhook.** With the channel's webhook, ``send`` posts the same text
  there, as the app that owns the webhook. A webhook cannot reply in a thread.
- **The Web API, with a token** (``CORRESPOND_SLACK_TOKEN``: a bot token ``xoxb-`` acts as
  the app, a user token ``xoxp-`` as the person). ``read`` returns a channel's latest
  top-level messages (``conversations.history``), ``listen`` polls for newer ones (the
  cursor is a message ``ts``), and ``send`` posts with ``chat.postMessage``, replying in a
  thread when ``reply_to`` is a message's ``ts``. A channel given by name is looked up
  once with ``conversations.list``. Install the app in your own workspace: Slack limits a
  commercially distributed app outside its Marketplace to one history request a minute.
  :meth:`Slack.verify` checks a request Slack sends (``X-Slack-Signature``, with the
  signing secret), graded ``crypto``.

Slack's ``mrkdwn`` is not Markdown: ``*bold*``, ``_italic_``, ``~strike~``, ``<url|text>``
links, no headings, no tables, and ``&``, ``<`` and ``>`` escaped. :func:`to_mrkdwn`
converts the common Markdown into it, so a draft written as GitHub Markdown does not paste
as literal asterisks and pipes. Because every ``<`` in the text is escaped, a draft cannot
carry a Slack mention (``<!channel>``, ``<@U...>``) that would notify people.

Messages read are ``platform``: Slack authenticated the account, and correspond fetched
them itself. Neither the token nor a webhook URL appears in a plan or an error.

>>> to_mrkdwn("**Fixed** in [#12](https://example.org/12) & shipped")
'*Fixed* in <https://example.org/12|#12> &amp; shipped'
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import subprocess
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode, urlsplit

from correspond import settings as _settings
from correspond.channels._http import retry_after_seconds, urllib_http
from correspond.errors import ChannelError, InvalidRef, MissingRequirement, NotSupported
from correspond.model import (
    Account,
    Attachment,
    Audience,
    Authenticity,
    Capabilities,
    ChannelIdentity,
    ConversationRef,
    Draft,
    Event,
    Grade,
    HistoryDepth,
    Message,
    Scope,
    SendResult,
    Support,
)
from correspond.ops import window, with_final_cursor
from correspond.registry import require, value

__all__ = ["MAX_TEXT_CHARS", "Slack", "to_mrkdwn", "webhook_env"]

NAME = "slack"
#: A Slack channel name (lower case; Slack allows letters, digits, - and _).
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,79}$")
#: A Slack conversation id: a public (C), private (G) or direct (D) channel.
CHANNEL_ID_RE = re.compile(r"^[CGD][A-Z0-9]{8,20}$")
#: Slack truncates a message's text past this many characters.
MAX_TEXT_CHARS = 40_000
#: An incoming webhook: Slack's host (or GovSlack's), then a path of letters, digits, / _ and -.
WEBHOOK_RE = re.compile(r"^https://hooks\.slack(?:-gov)?\.com/[A-Za-z0-9/_-]{1,500}\Z")
#: What Slack answers a webhook with, as correspond's error kinds.
_WEBHOOK_ERRORS = {
    400: "validation",
    403: "permission",
    404: "not_found",
    410: "not_found",
    429: "rate_limited",
}

_FENCE_RE = re.compile(r"^(```|~~~)[^\n]*\n(.*?)^\1[ \t]*$", re.M | re.S)
_CODE_RE = re.compile(r"`([^`\n]{1,2000})`")
#: Link text has no brackets, so a run of ``[`` is scanned once; a URL may hold one level of parentheses.
_IMAGE_OR_LINK_RE = re.compile(
    r"!?\[([^\[\]\n]{0,1000})\]\(([^()\s<>]{1,2000}(?:\([^()\s<>]{0,200}\)[^()\s<>]{0,2000})*)(?:\s+\"[^\"\n]{0,200}\")?\)"
)
_AUTOLINK_RE = re.compile(r"<((?:https?://|mailto:)[^<>\s]{1,2000})>")
#: Link targets Slack is given as links; anything else (``!channel``, ``@U123``, ``#C1``) stays text.
_LINK_SCHEME_RE = re.compile(r"^(?:https?://|mailto:)", re.I)
_BACKSLASH_RE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!~|>])")
_HEADING_RE = re.compile(r"^#{1,6}[ \t]+([^\n]{1,1000}?)[ \t]*#*[ \t]*$", re.M)
_BOLD_ITALIC_RE = re.compile(r"\*\*\*(?=\S)([^*\n]{1,1000}?)(?<=\S)\*\*\*")
_BOLD_RE = re.compile(r"(\*\*|__)(?=\S)([^\n]{1,1000}?)(?<=\S)\1")
_ITALIC_RE = re.compile(r"(?<![\w*])\*(?=\S)([^*\n]{1,1000}?)(?<=\S)\*(?![\w*])")
_STRIKE_RE = re.compile(r"~~(?=\S)([^\n]{1,1000}?)(?<=\S)~~")
_RULE_RE = re.compile(
    r"^[ \t]*(?:(?:-[ \t]*){3,}|(?:\*[ \t]*){3,}|(?:_[ \t]*){3,})$", re.M
)
_BULLET_RE = re.compile(r"^([ \t]*)[-*+][ \t]+", re.M)
_TABLE_ROW_RE = re.compile(r"^[ \t]*\|.*\|[ \t]*$")
_TABLE_SEPARATOR_RE = re.compile(
    r"^[ \t]*\|?[ \t]*:?-{3,}:?[ \t]*(?:\|[ \t]*:?-{3,}:?[ \t]*)*\|?[ \t]*$"
)
_SLOT_RE = re.compile("\x00(\\d+)\x00")


def _escape(text: str) -> str:
    """Slack's three control characters escaped; an entity already written (``&amp;``) is read as its character first."""
    text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _url(url: str) -> str:
    """A link target safe inside ``<...|...>``: ``<``, ``>``, ``|`` and spaces percent-encoded."""
    return (
        url.replace("|", "%7C")
        .replace("<", "%3C")
        .replace(">", "%3E")
        .replace(" ", "%20")
    )


def to_mrkdwn(markdown: str) -> str:
    """Common Markdown as Slack ``mrkdwn``: bold, italic, strike, links, headings, lists, code, quotes, tables as code.

    Only ``http``, ``https`` and ``mailto`` targets become links: a Markdown link to
    ``!channel`` or ``@U123`` stays text, so no draft can notify a channel or a person.

    >>> to_mrkdwn("# Release\\n- **new**: _faster_ export\\n- ~~old~~ gone")
    '*Release*\\n• *new*: _faster_ export\\n• ~old~ gone'
    >>> to_mrkdwn("> quoted <b> & `a < b`")
    '> quoted &lt;b&gt; &amp; `a &lt; b`'
    >>> to_mrkdwn("[ping](!channel)")
    'ping (!channel)'
    """
    slots: list[str] = []

    def keep(text: str) -> str:
        slots.append(text)
        return f"\x00{len(slots) - 1}\x00"

    def resolve(text: str) -> str:
        # A slot refers only to slots made before it, so this ends.
        for _ in range(len(slots) + 1):
            if "\x00" not in text:
                return text
            text = _SLOT_RE.sub(lambda m: slots[int(m.group(1))], text)
        return text.replace("\x00", "")

    def link(m: re.Match) -> str:
        label, target = m.group(1), m.group(2)
        if not _LINK_SCHEME_RE.match(target):
            return keep(_escape(f"{label} ({target})" if label else target))
        return keep(
            f"<{_url(target)}|{_escape(label)}>" if label else f"<{_url(target)}>"
        )

    def table(lines: list[str]) -> str:
        return keep("```\n" + _escape("\n".join(lines)) + "\n```")

    text = markdown.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    text = _FENCE_RE.sub(lambda m: keep(f"```\n{_escape(m.group(2))}```"), text)
    lines, out, i = text.split("\n"), [], 0
    while i < len(lines):  # a table is a header row, a separator row, then rows
        if (
            i + 1 < len(lines)
            and _TABLE_ROW_RE.match(lines[i])
            and _TABLE_SEPARATOR_RE.match(lines[i + 1])
        ):
            j = i + 2
            while j < len(lines) and _TABLE_ROW_RE.match(lines[j]):
                j += 1
            out.append(table(lines[i:j]))
            i = j
        else:
            out.append(lines[i])
            i += 1
    text = "\n".join(out)
    text = _CODE_RE.sub(lambda m: keep(f"`{_escape(m.group(1))}`"), text)
    text = _BACKSLASH_RE.sub(lambda m: keep(_escape(m.group(1))), text)
    text = _AUTOLINK_RE.sub(lambda m: keep(f"<{_url(m.group(1))}>"), text)
    text = _IMAGE_OR_LINK_RE.sub(link, text)
    quoted = bool(
        re.search(r"^[ \t]*>", text, re.M)
    )  # Slack quotes a line starting with ">"
    text = _escape(text)
    if quoted:
        text = re.sub(r"^([ \t]*)&gt;", r"\1>", text, flags=re.M)
    text = _RULE_RE.sub(lambda m: keep("──────────"), text)

    def heading(m: re.Match) -> str:
        inner = _BOLD_RE.sub(lambda b: b.group(2), m.group(1))
        inner = _ITALIC_RE.sub(lambda it: f"_{it.group(1)}_", inner)
        return keep(f"*{inner}*")

    text = _HEADING_RE.sub(heading, text)
    text = _BOLD_ITALIC_RE.sub(lambda m: keep(f"*_{m.group(1)}_*"), text)
    text = _BOLD_RE.sub(lambda m: keep(f"*{m.group(2)}*"), text)
    text = _STRIKE_RE.sub(lambda m: keep(f"~{m.group(1)}~"), text)
    text = _ITALIC_RE.sub(lambda m: f"_{m.group(1)}_", text)
    text = _BULLET_RE.sub(r"\1• ", text)
    return resolve(text)


def webhook_env(name: str) -> str:
    """The environment variable holding one name's webhook URL.

    >>> webhook_env("team-alerts"), webhook_env("team_alerts")
    ('CORRESPOND_SLACK_WEBHOOK_TEAM_ALERTS', 'CORRESPOND_SLACK_WEBHOOK_TEAM__ALERTS')
    """
    return "CORRESPOND_SLACK_WEBHOOK_" + name.upper().replace("_", "__").replace("-", "_")


def webhook_keychain_service(name: str) -> str:
    """The macOS Keychain item holding one name's webhook URL.

    >>> webhook_keychain_service("team-alerts")
    'correspond-slack-webhook-team-alerts'
    """
    return f"correspond-{NAME}-webhook-{name}"


#: Slack Web API errors, as correspond's error kinds.
_API_ERRORS = {
    "invalid_auth": "auth",
    "not_authed": "auth",
    "account_inactive": "auth",
    "token_revoked": "auth",
    "token_expired": "auth",
    "missing_scope": "permission",
    "not_in_channel": "permission",
    "restricted_action": "permission",
    "is_archived": "permission",
    "channel_not_found": "not_found",
    "thread_not_found": "not_found",
    "message_not_found": "not_found",
    "ratelimited": "rate_limited",
    "msg_too_long": "validation",
    "no_text": "validation",
    "invalid_arguments": "validation",
    "invalid_cursor": "validation",
    "invalid_ts_oldest": "validation",
}
API_BASE = "https://slack.com/api"
PAGE_SIZE = 200
READ_DEFAULT = 50
LISTEN_LOOKBACK_S = 24 * 3600
#: How far behind the clock a quiet first poll sets its cursor (this clock may run ahead of Slack's).
CLOCK_SKEW_S = 60
#: How long a poll waits out a rate limit met partway through its pages, and how many times.
RATE_WAIT_MAX_S = 60
RATE_WAIT_TRIES = 3
#: Hosts a file's link may name for the token to go with it.
FILE_HOST_RE = re.compile(r"^(?:[a-z0-9-]+\.)*slack\.com$")
#: Message subtypes that are someone writing (others, like channel_join, are Slack's own notices).
MESSAGE_SUBTYPES = (None, "bot_message", "thread_broadcast", "file_share", "me_message")
#: How old a signed request may be before :meth:`Slack.verify` calls it a replay.
SIGNATURE_MAX_AGE_S = 300
TS_RE = re.compile(r"^[0-9]{1,12}\.[0-9]{1,9}$")
_PLATFORM = Authenticity(
    grade=Grade.PLATFORM, evidence={"attested_by": "the Slack Web API"}
)


def _posts_as(token: str) -> str:
    """Whose name a token posts under, as far as its shape says (a real send asks Slack).

    >>> _posts_as("xoxb-1"), _posts_as("xoxe.xoxb-1"), _posts_as("xoxp-1")
    ('the app (a bot token)', 'the app (a bot token)', 'the person whose token it is (a user token)')
    """
    if "xoxb-" in token:
        return "the app (a bot token)"
    if "xoxp-" in token:
        return "the person whose token it is (a user token)"
    return "whoever the token belongs to (checked when sending)"


def _ts_time(ts: str | None) -> datetime | None:
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc) if ts else None
    except (TypeError, ValueError, OverflowError):
        return None


class Slack:
    """Slack: drafts in Slack's own formatting; reading, polling and posting with a token; posting through a webhook."""

    name = NAME

    def __init__(
        self,
        *,
        http: Callable[..., Any] = urllib_http,
        keychain: Callable[[str], str] | None = None,
        run: Callable[..., Any] = subprocess.run,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.http = http
        self.run = run
        self.clock = clock
        self.sleep = sleep
        self._keychain = keychain
        self._channel_ids: dict[str, str] = {}
        self._whoami: dict[str, Account] = {}

    @property
    def capabilities(self) -> Capabilities:
        """With a token: read, poll, post and reply in threads; with a webhook: post; with nothing: a draft."""
        return Capabilities(
            channel=NAME,
            read=Support.PARTIAL,
            listen=Support.PARTIAL,
            send=Support.FULL,
            audience=Support.PARTIAL,
            verify=Support.FULL,
            initiate=Support.FULL,
            reply=Support.FULL,
            history_depth=HistoryDepth.FULL,
            listen_modes=("poll",),
            grades=(Grade.PLATFORM, Grade.CRYPTO, Grade.FORGED),
            max_text_length=MAX_TEXT_CHARS,
            formats=("mrkdwn",),
            native_fields=("ts", "thread_ts", "reply_count", "subtype", "bot_id"),
            rate_limits=(
                "conversations.history: about 50 requests a minute for an internal app; a commercially distributed app outside the Marketplace gets 1 a minute and 15 messages",
                "chat.postMessage: about one message per second per channel",
            ),
            notes=(
                "a dry run renders the text as Slack mrkdwn (bold *x*, italic _x_, links <url|text>, no headings or tables) and needs nothing configured: it is the draft the operator can paste, a reply included (paste it in the thread)",
                "with a token (CORRESPOND_SLACK_TOKEN) read, listen and send use the Web API, and send replies in a thread with reply_to = the message's ts; a bot token posts as the app, a user token as the person, and the plan says which",
                "a channel with its own incoming webhook (CORRESPOND_SLACK_WEBHOOK_<CHANNEL>, or the Keychain) posts through it, a reply excepted (a webhook cannot thread); a channel without one posts with the token",
                "the text cannot mention anyone: every < is escaped and only http, https and mailto targets become links, so <!channel>, <@U...> or [x](!here) arrive as text",
                "read and listen see a channel's top-level messages, thread parents and replies also sent to the channel, not the other replies inside threads; Slack's own notices (someone joined, the topic changed) are left out; listen polls over the Web API and sees new messages, not edits",
                "push delivery (the Events API, Socket Mode) is not built: listen polls; verify is ready for a receiver of Slack's signed requests",
                "install the app in your own workspace: a commercially distributed app outside Slack's Marketplace gets one history request a minute, which polling cannot keep up with; a rate limit met partway through a poll is waited out (up to a minute, three times)",
                "a file is fetched with the token only from Slack's own hosts (the token needs files:read); a file hosted elsewhere is not fetched",
                "a channel named rather than given by id is looked up with conversations.list (the token needs channels:read, and groups:read for a private one)",
                "verify checks a request's X-Slack-Signature with the signing secret, and refuses one older than 5 minutes",
            ),
        )

    def parse_ref(self, id: str) -> ConversationRef:
        """A channel's id as Slack writes it, or its name, ``#`` optional, lower-cased."""
        text = (id or "").strip()
        if CHANNEL_ID_RE.match(text):
            return ConversationRef(channel=NAME, id=text, kind="channel")
        name = text[1:] if text.startswith("#") else text
        name = name.lower()
        if not NAME_RE.match(name):
            raise InvalidRef(
                f"slack references are slack:<channel name> (1-80 of a-z, 0-9, - and _) or slack:<channel id>, never a webhook URL, not slack:{id}"
            )
        return ConversationRef(channel=NAME, id=name, kind="channel")

    # ------------------------------------------------------------------ plumbing

    def _token(self, *, dry_run: bool = False) -> str | None:
        """The Web API token, or ``None``; a dry run reads only the environment."""
        if dry_run:
            return value(NAME, "token", run=self.run, keychain=False)
        return value(NAME, "token", run=self.run)

    def _api(self, method: str, params: dict[str, Any], *, token: str) -> dict:
        """One Web API call (form-encoded); ``ok: false`` and HTTP failures as correspond's error kinds."""
        try:
            reply = self.http(
                "POST",
                f"{API_BASE}/{method}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                body=urlencode({k: v for k, v in params.items() if v is not None}).encode(
                    "utf-8"
                ),
            )
        except ChannelError as error:
            raise ChannelError(
                f"Slack {method}: {error}".replace(token, "<token>"),
                kind=error.kind,
                retryable=error.retryable,
            ) from None
        if reply.status == 429:
            wait = retry_after_seconds(reply.headers)
            raise ChannelError(
                f"Slack {method}: rate limited",
                kind="rate_limited",
                retryable=True,
                retry_after=wait,
            )
        data = reply.json() if reply.status == 200 else None
        if not isinstance(data, dict):
            raise ChannelError(
                f"Slack {method}: HTTP {reply.status}",
                kind="unavailable" if reply.status >= 500 else "validation",
                retryable=reply.status >= 500,
            )
        if not data.get("ok"):
            error = str(data.get("error") or "unknown_error")
            kind = _API_ERRORS.get(error, "unavailable")
            raise ChannelError(
                f"Slack {method}: {error}"
                + (f" (needed: {data['needed']})" if data.get("needed") else ""),
                kind=kind,
                retryable=kind == "rate_limited",
            )
        return data

    def _require_token(self, what: str) -> str:
        token = self._token()
        if not token:
            raise MissingRequirement(
                NAME,
                f"no Slack token to {what}",
                fix=(
                    "create a Slack app installed in your workspace (internal, not distributed), give its bot "
                    "channels:history, channels:read and chat:write (groups:* for private channels), invite it to "
                    "the channel, and set CORRESPOND_SLACK_TOKEN to its xoxb- token (or keep it in the Keychain)"
                ),
            )
        return token

    def _channel_id(self, ref: ConversationRef, token: str) -> str:
        """The channel's id: the reference's own, or a name looked up with ``conversations.list``."""
        if CHANNEL_ID_RE.match(ref.id):
            return ref.id
        if ref.id not in self._channel_ids:
            cursor = None
            while True:
                data = self._api(
                    "conversations.list",
                    {
                        "types": "public_channel,private_channel",
                        "exclude_archived": "true",
                        "limit": PAGE_SIZE,
                        "cursor": cursor,
                    },
                    token=token,
                )
                for channel in data.get("channels") or ():
                    if channel.get("name"):
                        self._channel_ids.setdefault(channel["name"], channel["id"])
                cursor = (data.get("response_metadata") or {}).get("next_cursor")
                if ref.id in self._channel_ids or not cursor:
                    break
        if ref.id not in self._channel_ids:
            raise ChannelError(
                f"no channel #{ref.id} that the token can see (invite the app, or give its id)",
                kind="not_found",
            )
        return self._channel_ids[ref.id]

    def _history(
        self, channel: str, token: str, *, oldest: str | None, limit: int | None
    ) -> list[dict]:
        """``conversations.history``, newest first, every page up to ``limit`` messages."""
        found: list[dict] = []
        cursor = None
        while True:
            params = {
                "channel": channel,
                "oldest": oldest,
                "limit": min(PAGE_SIZE, limit) if limit else PAGE_SIZE,
                "cursor": cursor,
            }
            for attempt in range(RATE_WAIT_TRIES + 1):
                try:
                    data = self._api("conversations.history", params, token=token)
                    break
                except ChannelError as error:
                    # Pages already read would be lost: wait a rate limit out, a few times.
                    wait = error.retry_after or RATE_WAIT_MAX_S
                    if (
                        error.kind != "rate_limited"
                        or not found
                        or attempt == RATE_WAIT_TRIES
                        or wait > RATE_WAIT_MAX_S
                    ):
                        raise
                    self.sleep(wait)
            found += list(data.get("messages") or ())
            cursor = (data.get("response_metadata") or {}).get("next_cursor")
            if not cursor or not data.get("has_more") or (limit and len(found) >= limit):
                return found[:limit] if limit else found

    def _message(self, conversation: ConversationRef, data: dict, token: str) -> Message:
        ts = str(data["ts"])
        thread = data.get("thread_ts")
        parent = thread if thread and thread != ts else None

        def loader(url: str, mime: str):
            def load() -> bytes:
                parts = urlsplit(url)
                if parts.scheme != "https" or not FILE_HOST_RE.match(
                    parts.hostname or ""
                ):
                    raise ChannelError(
                        f"this file is hosted outside Slack ({parts.hostname}); open its link: the token is never sent there",
                        kind="permission",
                    )
                reply = self.http(
                    "GET", url, headers={"Authorization": f"Bearer {token}"}
                )
                if reply.status != 200:
                    raise ChannelError(
                        f"Slack answered {reply.status} for a file", kind="unavailable"
                    )
                kind = reply.headers.get("content-type", "")
                if kind.startswith("text/html") and not mime.startswith("text/html"):
                    raise ChannelError(
                        "Slack answered with a sign-in page, not the file: the token needs the files:read scope",
                        kind="permission",
                    )
                return reply.body

            return load

        return Message(
            id=ts,
            conversation=conversation,
            author=ChannelIdentity(
                channel=NAME,
                native_id=str(data.get("user") or data.get("bot_id") or ""),
                display_name=(data.get("bot_profile") or {}).get("name")
                or data.get("username")
                or None,
                is_bot=bool(data.get("bot_id")),
            ),
            authenticity=_PLATFORM,
            sent_at=_ts_time(ts) or datetime.fromtimestamp(0, tz=timezone.utc),
            edited_at=_ts_time((data.get("edited") or {}).get("ts")),
            text=data.get("text") or "",
            body_format="mrkdwn",
            reply_to=parent,
            thread_root=parent,
            attachments=tuple(
                Attachment(
                    ref=f"slack-file:{f.get('id')}",
                    media_type=f.get("mimetype") or "application/octet-stream",
                    name=f.get("name"),
                    size=f.get("size"),
                    loader=loader(
                        f["url_private"], f.get("mimetype") or "application/octet-stream"
                    )
                    if f.get("url_private")
                    else None,
                )
                for f in data.get("files") or ()
            ),
            native={
                "ts": ts,
                "thread_ts": thread,
                "reply_count": data.get("reply_count"),
                "subtype": data.get("subtype"),
                "bot_id": data.get("bot_id"),
            },
            raw=dict(data),
        )

    # ------------------------------------------------------------------ reading

    def read(
        self,
        ref: ConversationRef,
        *,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[Message]:
        """The channel's latest top-level messages (``limit``, else 50), oldest first."""
        token = self._require_token("read a channel")
        channel = self._channel_id(ref, token)
        oldest = f"{since.timestamp():.6f}" if since is not None else None
        count = max(1, int(limit)) if limit else READ_DEFAULT
        found = self._history(channel, token, oldest=oldest, limit=count)
        messages = [
            self._message(ref, data, token)
            for data in found
            if data.get("subtype") in MESSAGE_SUBTYPES
        ]
        return window(messages, since=since, limit=count)

    def poll(
        self, ref: ConversationRef, *, cursor: str | None = None, limit: int | None = None
    ):
        """Top-level messages newer than ``cursor`` (a ``ts``), oldest first; a first poll looks back a day."""
        token = self._require_token("listen to a channel")
        if cursor and not TS_RE.match(str(cursor)):
            raise ChannelError(
                f"a Slack cursor is a message ts, not {cursor!r}", kind="validation"
            )
        channel = self._channel_id(ref, token)
        now = self.clock()
        oldest = str(cursor) if cursor else f"{now - LISTEN_LOOKBACK_S:.6f}"
        found = sorted(
            self._history(channel, token, oldest=oldest, limit=None),
            key=lambda m: float(m["ts"]),
        )
        found = [m for m in found if float(m["ts"]) > float(oldest)]
        if limit:
            found = found[: max(1, int(limit))]
        events = [
            Event(
                kind="message.created",
                channel=NAME,
                delivery_id=f"slack:{channel}:{data['ts']}",
                cursor=str(data["ts"]),
                message=self._message(ref, data, token),
            )
            for data in found
            if data.get("subtype") in MESSAGE_SUBTYPES
        ]
        if found:
            final = str(found[-1]["ts"])  # past Slack's own notices too
        elif not cursor:
            # A quiet first poll starts the next from now, less this clock's lead on Slack's.
            final = f"{now - CLOCK_SKEW_S:.6f}"
        else:
            final = None
        return with_final_cursor(events, final)

    # ----------------------------------------------------------------- audience

    def audience(self, ref: ConversationRef, *, draft: Draft | None = None) -> Audience:
        """The channel's members, never listed; a shared channel reaches other workspaces."""
        return Audience(
            ref=ref.encoded,
            scope=Scope.GROUP,
            complete=False,
            classes=(
                "the members of the channel (anyone in the workspace, for a public channel)",
                "guests and members of other organisations, when the channel is shared (Slack Connect)",
                "apps and integrations installed in the channel",
            ),
            durability=("copies_pushed", "archived_by_others", "edit_history_visible"),
            widening=("joiners_read_history", "forwarding", "visibility_flip"),
            evidence=("a Slack channel's members are not listed by correspond",),
        )

    # ----------------------------------------------------------------- verifying

    def verify(self, headers: Mapping[str, str], body: bytes) -> Authenticity:
        """``crypto`` when ``X-Slack-Signature`` matches the signing secret over a fresh timestamp, ``forged`` otherwise."""
        secret = require(NAME, "signing_secret", run=self.run)
        lowered = {str(k).lower(): str(v) for k, v in headers.items()}
        stamp = lowered.get("x-slack-request-timestamp", "")
        received = lowered.get("x-slack-signature", "")
        if not stamp.isdigit():
            return Authenticity(
                grade=Grade.FORGED, evidence={"signature": "no timestamp"}
            )
        if abs(self.clock() - int(stamp)) > SIGNATURE_MAX_AGE_S:
            return Authenticity(
                grade=Grade.FORGED, evidence={"signature": "stale (a replay?)"}
            )
        base = b"v0:" + stamp.encode() + b":" + body
        expected = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
        if received.isascii() and received and hmac.compare_digest(received, expected):
            return Authenticity(grade=Grade.CRYPTO, evidence={"signature": "valid"})
        return Authenticity(
            grade=Grade.FORGED,
            evidence={"signature": "mismatch" if received else "missing"},
        )

    # ------------------------------------------------------------------ writing

    def _webhook(self, name: str) -> str | None:
        url = os.environ.get(webhook_env(name), "").strip()
        if not url:
            url = (self._keychain or _settings.keychain_get)(
                webhook_keychain_service(name)
            ).strip()
        if url and not WEBHOOK_RE.match(url):
            raise ChannelError(
                f"the webhook for slack:{name} is not a Slack incoming webhook (https://hooks.slack.com/ then its path)",
                kind="validation",
            )
        return url or None

    def send(
        self, ref: ConversationRef, draft: Draft, *, dry_run: bool = False
    ) -> SendResult:
        """Render ``draft`` as mrkdwn, then post it with the token (``chat.postMessage``), else through the channel's webhook.

        ``draft.reply_to`` (a message's ``ts``) posts in that thread with a token; through a
        webhook, which cannot thread, it renders in a dry run for the operator to paste.
        """
        if draft.notify:
            raise NotSupported(
                "notify",
                NAME,
                alternatives=("the text cannot mention anyone; name them in it",),
            )
        if draft.reply_to and not TS_RE.match(draft.reply_to):
            raise ChannelError(
                f"reply_to is a Slack message ts such as 1700000000.000100, not {draft.reply_to!r}",
                kind="validation",
            )
        if len(draft.text) > MAX_TEXT_CHARS:
            raise ChannelError(
                f"the text is {len(draft.text)} characters; Slack keeps {MAX_TEXT_CHARS}",
                kind="validation",
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
        token = self._token(dry_run=dry_run)
        webhook_set = bool(os.environ.get(webhook_env(ref.id), "").strip())
        # A channel's own webhook is the operator's explicit choice for it, a reply excepted.
        via_webhook = webhook_set and not draft.reply_to
        plan: dict[str, Any] = {
            "action": "post through the channel's incoming webhook"
            if via_webhook
            else "post with the token (chat.postMessage)"
            if token
            else "post with the token if one is found when sending, else through the channel's incoming webhook",
            "channel": ref.id,
            "format": "mrkdwn",
            "text": text,
            "characters": len(text),
            "max_characters": MAX_TEXT_CHARS,
        }
        if token and not via_webhook:
            plan["posts_as"] = _posts_as(token)
        if draft.reply_to:
            plan["in_thread_of" if token else "paste_in_thread_of"] = draft.reply_to
        if not token and not webhook_set:
            plan["looked_up_when_sending"] = [
                "CORRESPOND_SLACK_TOKEN (or the Keychain)",
                f"{webhook_env(ref.id)} (or the Keychain item {webhook_keychain_service(ref.id)})",
            ]
        if dry_run:
            return SendResult(
                ok=True, channel=NAME, conversation=ref.encoded, dry_run=True, plan=plan
            )
        if via_webhook:
            return self._post_webhook(ref, text, plan)
        if token is None and not draft.reply_to and self._webhook(ref.id):
            return self._post_webhook(ref, text, plan)
        if token:
            return self._post(ref, text, draft.reply_to, token, plan)
        if draft.reply_to:
            raise NotSupported(
                "reply",
                NAME,
                alternatives=(
                    "a webhook cannot thread: set a token, or paste the dry run's text in the thread",
                ),
            )
        return self._post_webhook(ref, text, plan)

    def _post(
        self, ref: ConversationRef, text: str, thread: str | None, token: str, plan: dict
    ) -> SendResult:
        channel = self._channel_id(ref, token)
        data = self._api(
            "chat.postMessage",
            {
                "channel": channel,
                "text": text,
                "thread_ts": thread,
                "unfurl_links": "false",
            },
            token=token,
        )
        posted = data.get("message") or {}
        return SendResult(
            ok=True,
            channel=NAME,
            conversation=ref.encoded,
            message_id=str(data.get("ts") or posted.get("ts") or "") or None,
            account=self._account(token),
            plan={**plan, "channel_id": channel},
        )

    def _account(self, token: str) -> Account | None:
        """Whose name the token posts under, asked of Slack once (``auth.test``): a bot or a person."""
        if token not in self._whoami:
            try:
                data = self._api("auth.test", {}, token=token)
            except ChannelError:
                return None
            self._whoami[token] = Account(
                channel=NAME,
                id=str(data.get("user_id") or data.get("bot_id") or ""),
                acts_as="bot" if data.get("bot_id") else "user",
            )
        return self._whoami[token]

    def _post_webhook(self, ref: ConversationRef, text: str, plan: dict) -> SendResult:
        url = self._webhook(ref.id)
        if url is None:
            raise MissingRequirement(
                NAME,
                f"no Slack token and no incoming webhook for slack:{ref.id}",
                fix=(
                    f"set CORRESPOND_SLACK_TOKEN, or create an incoming webhook for the channel (Slack app settings, "
                    f"Incoming Webhooks) and set {webhook_env(ref.id)} or the Keychain item "
                    f"{webhook_keychain_service(ref.id)}; without either, a dry run gives the text to paste"
                ),
                kind="validation",
            )
        try:
            reply = self.http(
                "POST",
                url,
                headers={"Content-Type": "application/json"},
                body=json.dumps({"text": text}).encode("utf-8"),
            )
        except ChannelError as error:  # the transport's message names only the host
            raise ChannelError(
                f"could not post to slack:{ref.id}: {error}",
                kind=error.kind,
                retryable=error.retryable,
            ) from None
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
            plan={**plan, "via": "webhook"},
        )
