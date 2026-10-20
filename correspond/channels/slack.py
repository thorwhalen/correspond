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
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
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
    "user_not_found": "not_found",
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
#: The polling walk: the first window's width, the narrowest, the widest, and how many
#: history requests one poll makes at most. A window comes back whole (narrowing when one
#: is full), so each poll moves the cursor forward whatever the rate limit.
WINDOW_START_S = 3600.0
WINDOW_MIN_S = 1.0
WINDOW_MAX_S = float(LISTEN_LOOKBACK_S)
MAX_REQUESTS_PER_POLL = 24
#: How long a poll waits out a rate limit met partway through its pages, and how many times.
RATE_WAIT_MAX_S = 60
RATE_WAIT_TRIES = 3
#: Hosts a file's link may name for the token to go with it.
FILE_HOST_RE = re.compile(r"^(?:[a-z0-9-]+\.)*slack\.com$")
#: Message subtypes that are someone writing (others, like channel_join, are Slack's own notices).
MESSAGE_SUBTYPES = (None, "bot_message", "thread_broadcast", "file_share", "me_message")
#: How old a signed request may be before :meth:`Slack.verify` calls it a replay.
SIGNATURE_MAX_AGE_S = 300
#: Digits a Unix timestamp in seconds can plausibly have; longer is refused before parsing.
TIMESTAMP_MAX_DIGITS = 12
TS_RE = re.compile(r"^[0-9]{1,12}\.[0-9]{1,9}$")
#: A user's id as Slack writes it (``U...`` or ``W...``), the only ids ``users.info`` is asked about.
USER_ID_RE = re.compile(r"^[UW][A-Z0-9]{2,20}$")
#: How long a channel name's id is trusted before ``conversations.list`` is asked again (a rename shows within it).
CHANNEL_CACHE_TTL_S = 300
#: How long a user's handle and display name are trusted before ``users.info`` is asked again.
USER_CACHE_TTL_S = 3600
#: How long a failed ``users.info`` (other than a refusal or an unknown user) waits before it is asked again.
USER_RETRY_S = 60
#: Pages of ``conversations.replies`` read for one thread at most (a read past them walks back from the newest reply).
MAX_THREAD_PAGES = 20
#: The narrowest window a long thread's walk back looks at: Slack's ts resolution, so it holds one reply at most.
THREAD_TAIL_MIN_WINDOW_S = 0.000001
#: How much an empty or sparse window widens the next one as a long thread's walk goes back.
THREAD_TAIL_GROWTH = 8
#: Workspaces (tokens) one adapter keeps caches for; the least recently used is dropped past it.
MAX_WORKSPACES = 8
#: How listen learns of new messages: by polling the Web API, or from the Events API receiver.
DELIVERIES = ("poll", "events")
#: The data-root folder the Events API receiver stores events in.
EVENTS_KIND = "slack-events"
#: A stored event's key: ``<arrival ns, 20 digits>-<event id>.json``, so keys sort by arrival.
RECEIVED_KEY_RE = re.compile(
    r"^(?P<stamp>[0-9]{20})-(?P<event>Ev[A-Za-z0-9]{1,40})\.json$"
)
#: An events listen's cursor: ``e<arrival ns>`` of the last stored event it passed.
EVENTS_CURSOR = "e"
#: How far back a first events listen looks, and how long stored events are kept.
EVENTS_LOOKBACK_NS = 24 * 3600 * 10**9
EVENTS_RETENTION_NS = 7 * 24 * 3600 * 10**9
#: A listen cursor: the ts reached, and the window width the walk was using.
CURSOR_RE = re.compile(
    r"^(?P<ts>[0-9]{1,12}\.[0-9]{1,9})(?:/(?P<width>[0-9]{1,9}(?:\.[0-9]{1,6})?))?$"
)
_PLATFORM = Authenticity(
    grade=Grade.PLATFORM, evidence={"attested_by": "the Slack Web API"}
)


def received_key(stamp_ns: int, event_id: str) -> str:
    """The store key of an event that arrived at ``stamp_ns``: sorts by arrival.

    >>> received_key(5, "Ev01")
    '00000000000000000005-Ev01.json'
    """
    return f"{stamp_ns:020d}-{event_id}.json"


def _cursor(position: str, width: float) -> str:
    """A listen cursor: where the walk got to, and the width it will try next.

    >>> _cursor("1760000000.000100", 900.0)
    '1760000000.000100/900.000'
    """
    return f"{position}/{width:.3f}"


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


def _token_key(token: str) -> str:
    """The key a token's caches are kept under: a digest, so no cache holds the token itself.

    >>> _token_key("xoxb-1") == _token_key("xoxb-1") != _token_key("xoxb-2")
    True
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass
class _Workspace:
    """What one token has learned of its workspace: channel names, authors, whose token it is.

    A listing of channel names that stopped once the name it looked for appeared keeps
    the cursor it stopped at (``channels_next``), so the next name not yet seen resumes
    there instead of listing from scratch; ``None`` there means the listing is whole.
    """

    channel_ids: dict[str, str] = field(default_factory=dict)
    channels_listed_at: float | None = None
    channels_next: str | None = None
    users: dict[str, tuple[float, dict | None]] = field(default_factory=dict)  # id: (until, user)
    users_refused_until: float = 0.0
    account: Account | None = None

    def remember_user(
        self, user_id: str, entry: tuple[float, dict | None], *, now: float
    ) -> None:
        """Keep ``entry`` for ``user_id``, dropping the entries that expired (the cache only grows with live ones)."""
        for gone in [k for k, (until, _) in self.users.items() if until <= now]:
            del self.users[gone]
        self.users[user_id] = entry


def _parent_of(messages: list[dict], thread: str) -> dict | None:
    """The thread's parent among ``messages`` (ts compared as numbers: ``1.5`` is ``1.500000``).

    >>> _parent_of([{"ts": "1.500000"}, {"ts": "2.000000"}], "1.5")
    {'ts': '1.500000'}
    """
    return next((m for m in messages if float(m["ts"]) == float(thread)), None)


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
        events: MutableMapping[str, Any] | None = None,
    ):
        self.http = http
        self._events = events
        self.run = run
        self.clock = clock
        self.sleep = sleep
        self._keychain = keychain
        self._workspaces: dict[str, _Workspace] = {}  # token digest: what it learned
        self._server_now: float | None = None

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
            listen_modes=("poll", "push"),
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
                "read and listen see a channel's top-level messages, thread parents and replies also sent to the channel; the other replies are read through the thread's own reference, slack:<channel>/<thread ts> (conversations.replies), which listen polls and send replies in; reading a thread too long for 20 pages of replies (about 4,000) returns its newest ones (it walks back from the latest reply); Slack's own notices (someone joined, the topic changed) are left out; listen polls over the Web API and sees new messages, not edits",
                "an author's handle and display name come from users.info (the token needs users:read), kept an hour per token (as are channel names), so two workspaces never share them; without the scope the author is the user id alone",
                "with delivery = events, listen reads what the Events API receiver (correspond-slack-events, behind your HTTPS reverse proxy) stored instead of polling, and also reports edits (message.updated) and deletions (message.deleted); the receiver verifies every request's signature and answers Slack's url_verification; Socket Mode is not built",
                "install the app in your own workspace (a commercially distributed app outside Slack's Marketplace gets one history request a minute); listen walks forward through time windows, delivering each whole before the next, so a poll a rate limit cuts short keeps what it delivered and the next one resumes there, and a backlog drains a little each poll; the newest minute waits for the next poll (this clock may run ahead of Slack's)",
                "a file is fetched with the token only from Slack's own hosts (the token needs files:read); a file hosted elsewhere is not fetched",
                "a channel named rather than given by id is looked up with conversations.list (the token needs channels:read, and groups:read for a private one); a name is trusted five minutes, so a renamed channel answers to its new name within them",
                "verify checks a request's X-Slack-Signature with the signing secret, and refuses one older than 5 minutes",
            ),
        )

    def parse_ref(self, id: str) -> ConversationRef:
        """A channel's id as Slack writes it, or its name, ``#`` optional, lower-cased; ``/<thread ts>`` after it names one thread."""
        text = (id or "").strip()
        if "/" in text:
            channel, _, thread = text.partition("/")
            if not TS_RE.match(thread):
                raise InvalidRef(
                    f"a Slack thread is slack:<channel>/<thread ts> (the parent message's ts, such as 1700000000.000100), not slack:{id}"
                )
            parent = self.parse_ref(channel)
            return ConversationRef(
                channel=NAME, id=f"{parent.id}/{thread}", kind="thread", parent=parent
            )
        if CHANNEL_ID_RE.match(text):
            return ConversationRef(channel=NAME, id=text, kind="channel")
        name = text[1:] if text.startswith("#") else text
        name = name.lower()
        if not NAME_RE.match(name):
            raise InvalidRef(
                f"slack references are slack:<channel name> (1-80 of a-z, 0-9, - and _) or slack:<channel id>, then /<thread ts> for one thread, never a webhook URL, not slack:{id}"
            )
        return ConversationRef(channel=NAME, id=name, kind="channel")

    # ------------------------------------------------------------------ plumbing

    @property
    def events(self) -> MutableMapping[str, Any]:
        """What the Events API receiver stored (read when ``delivery`` is ``events``)."""
        if self._events is None:
            from correspond.channels._webhook import configured_store

            self._events = configured_store(NAME, EVENTS_KIND)
        return self._events

    def _delivery(self) -> str:
        delivery = (
            str(value(NAME, "delivery", run=self.run, keychain=False) or "poll")
            .strip()
            .lower()
        )
        if delivery not in DELIVERIES:
            raise ChannelError(
                f"slack delivery is one of {DELIVERIES}, not {delivery!r}",
                kind="validation",
            )
        return delivery

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
        try:  # Slack's own clock, which bounds how far a listen walks
            self._server_now = parsedate_to_datetime(reply.headers["date"]).timestamp()
        except (KeyError, TypeError, ValueError):
            pass
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

    def _workspace(self, token: str) -> _Workspace:
        """The caches of the workspace ``token`` belongs to: two tokens never share one.

        At most :data:`MAX_WORKSPACES` are kept; the least recently used goes first (a
        listener whose token rotates leaves its old ones behind).
        """
        key = _token_key(token)
        known = self._workspaces.pop(key, None) or _Workspace()
        self._workspaces[key] = known  # most recently used last
        while len(self._workspaces) > MAX_WORKSPACES:
            del self._workspaces[next(iter(self._workspaces))]
        return known

    @staticmethod
    def _where(ref: ConversationRef) -> tuple[str, str | None]:
        """The reference's channel (a name or an id) and its thread's ts, if it names one."""
        channel, _, thread = ref.id.partition("/")
        return channel, thread or None

    def _channel_id(self, ref: ConversationRef, token: str) -> str:
        """The channel's id: the reference's own, or a name looked up with ``conversations.list``.

        Names are kept per token and trusted for :data:`CHANNEL_CACHE_TTL_S` seconds; after
        that, or for a name a whole listing did not have, the list is read again from
        scratch, so a renamed channel's old name stops resolving and its new one starts.
        A listing stops at the page that has the name; a later name it has not reached
        resumes from there, and a resumed listing that fails or ends without the name
        (a channel created on a page already read) is read once more from the start.
        """
        name = self._where(ref)[0]
        if CHANNEL_ID_RE.match(name):
            return name
        known = self._workspace(token)
        now = self.clock()
        listed_at = known.channels_listed_at
        fresh = listed_at is not None and now - listed_at < CHANNEL_CACHE_TTL_S
        if not (fresh and name in known.channel_ids):
            resumed = fresh and bool(known.channels_next)
            if not resumed:
                known.channel_ids, known.channels_listed_at = {}, now
                known.channels_next = ""
            try:
                self._list_channels(known, name, token)
            except ChannelError:
                if not resumed:
                    raise
                known.channels_next = None  # a stale cursor: start over below
            if resumed and name not in known.channel_ids:
                known.channel_ids, known.channels_listed_at = {}, now
                known.channels_next = ""
                self._list_channels(known, name, token)
        if name not in known.channel_ids:
            raise ChannelError(
                f"no channel #{name} that the token can see (invite the app, or give its id)",
                kind="not_found",
            )
        return known.channel_ids[name]

    def _list_channels(self, known: _Workspace, name: str, token: str) -> None:
        """Read ``conversations.list`` pages into ``known`` from its cursor, until ``name`` shows or the list ends."""
        while known.channels_next is not None and name not in known.channel_ids:
            data = self._api(
                "conversations.list",
                {
                    "types": "public_channel,private_channel",
                    "exclude_archived": "true",
                    "limit": PAGE_SIZE,
                    "cursor": known.channels_next or None,
                },
                token=token,
            )
            for channel in data.get("channels") or ():
                if channel.get("name"):
                    known.channel_ids.setdefault(channel["name"], channel["id"])
            following = (data.get("response_metadata") or {}).get("next_cursor")
            known.channels_next = following or None

    def _user(self, user_id: str, token: str) -> dict | None:
        """``users.info`` for one user, kept :data:`USER_CACHE_TTL_S` seconds; ``None`` when Slack will not say.

        An unknown user is remembered as ``None`` for as long. A refused token (no
        ``users:read``) stops every lookup for as long, so a read costs one failed call,
        not one per author. Any other failure (a rate limit, a 5xx) is remembered for
        :data:`USER_RETRY_S` seconds (or the rate limit's own wait), so a read does not
        repeat it for every message.
        """
        if not USER_ID_RE.match(user_id):
            return None
        workspace = self._workspace(token)
        now = self.clock()
        if now < workspace.users_refused_until:
            return None
        known = workspace.users.get(user_id)
        if known and now < known[0]:
            return known[1]
        try:
            found = self._api("users.info", {"user": user_id}, token=token).get("user")
        except ChannelError as error:
            if error.kind in ("auth", "permission"):
                workspace.users_refused_until = now + USER_CACHE_TTL_S
                return None
            if error.kind != "not_found":
                wait = min(USER_CACHE_TTL_S, error.retry_after or USER_RETRY_S)
                stale = known[1] if known else None
                workspace.remember_user(user_id, (now + wait, stale), now=now)
                return stale
            found = None
        user = found if isinstance(found, dict) else None
        workspace.remember_user(user_id, (now + USER_CACHE_TTL_S, user), now=now)
        return user

    def _history(
        self,
        channel: str,
        token: str,
        *,
        oldest: str | None,
        limit: int | None,
        latest: str | None = None,
    ) -> list[dict]:
        """``conversations.history``, newest first, every page up to ``limit`` messages."""
        found: list[dict] = []
        cursor = None
        while True:
            params = {
                "channel": channel,
                "oldest": oldest,
                "latest": latest,
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

    def _replies(
        self,
        channel: str,
        thread: str,
        token: str,
        *,
        oldest: str | None,
        newest: int | None = None,
    ) -> list[dict]:
        """``conversations.replies``: the thread's parent and replies after ``oldest``, oldest first.

        A thread is read from its start, at most :data:`MAX_THREAD_PAGES` pages; a rate
        limit after the first page keeps what was read. A read that wants the ``newest``
        replies of a thread longer than those pages (as its parent's ``reply_count`` says
        on the first page, or as the pages running out show) walks back from the thread's
        latest reply instead (:meth:`_thread_tail`); a listen (no ``newest``) moves on from
        its cursor, so the oldest pages are the ones it wants.
        """
        found: list[dict] = []
        cursor = None
        unread = False
        for page in range(MAX_THREAD_PAGES):
            params = {
                "channel": channel,
                "ts": thread,
                "oldest": oldest,
                "limit": PAGE_SIZE,
                "cursor": cursor,
            }
            try:
                data = self._api("conversations.replies", params, token=token)
            except ChannelError as error:
                if error.kind != "rate_limited" or not found:
                    raise
                break
            found += list(data.get("messages") or ())
            cursor = (data.get("response_metadata") or {}).get("next_cursor")
            if not cursor or not data.get("has_more"):
                break
            if newest and page == 0 and oldest is None:
                parent = _parent_of(found, thread)
                replies = parent.get("reply_count") if parent else None
                if isinstance(replies, int) and replies >= MAX_THREAD_PAGES * PAGE_SIZE:
                    unread = True  # too long to page through: straight to its newest end
                    break
        else:
            unread = True
        found.sort(key=lambda m: float(m["ts"]))
        if unread and newest and found:
            found = self._thread_tail(channel, thread, token, head=found, count=newest)
        if oldest is not None:
            found = [m for m in found if float(m["ts"]) > float(oldest)]
        return found

    def _api_waiting(self, method: str, params: dict[str, Any], *, token: str) -> dict:
        """:meth:`_api`, waiting a rate limit out a few times (when the wait is short) before giving up."""
        for attempt in range(RATE_WAIT_TRIES + 1):
            try:
                return self._api(method, params, token=token)
            except ChannelError as error:
                wait = error.retry_after or RATE_WAIT_MAX_S
                if (
                    error.kind != "rate_limited"
                    or attempt == RATE_WAIT_TRIES
                    or wait > RATE_WAIT_MAX_S
                ):
                    raise
                self.sleep(wait)
        raise AssertionError("unreachable")  # pragma: no cover

    def _thread_tail(
        self, channel: str, thread: str, token: str, *, head: list[dict], count: int
    ) -> list[dict]:
        """The newest ``count`` replies of a thread too long to page through, oldest first.

        ``head`` is what was read from the start (oldest first). The walk goes back from the
        thread's latest reply (its parent's ``latest_reply``, else the later of Slack's clock
        and this one) one time window ``(low, high]`` at a time, the first sized from the
        thread's average pace. A window holding more than a page is narrowed (to the newer of
        its half and what its first page did not reach) and asked again; an empty or sparse
        one widens the next :data:`THREAD_TAIL_GROWTH`-fold. The walk stops with ``count``
        replies, on reaching ``head`` (the two then join without a gap), or after
        ``2 * MAX_THREAD_PAGES`` calls; a short rate limit is waited out.
        """
        floor = float(head[-1]["ts"])
        parent = _parent_of(head, thread)
        top = str(parent.get("latest_reply") or "") if parent else ""
        if not TS_RE.match(top) or float(top) <= floor:
            top = f"{max(self.clock(), self._server_now or 0.0, floor) + CLOCK_SKEW_S:.6f}"
        replies = parent.get("reply_count") if parent else None
        if isinstance(replies, int) and replies > 0:
            pace = (float(top) - float(thread)) / replies  # seconds per reply, on average
        else:
            others = [m for m in head if m is not parent]
            span = floor - float(others[0]["ts"]) if others else 0.0
            pace = span / len(others) if span > 0 else 1.0
        width = max(THREAD_TAIL_MIN_WINDOW_S, pace * min(count, PAGE_SIZE // 2))
        collected: list[dict] = []
        wanted = 0
        high = top
        for _ in range(2 * MAX_THREAD_PAGES):
            if wanted >= count or float(high) <= floor:
                break
            low = f"{max(floor, float(high) - width):.6f}"
            data = self._api_waiting(
                "conversations.replies",
                {
                    "channel": channel,
                    "ts": thread,
                    "oldest": low,
                    "latest": high,
                    "inclusive": "true",
                    "limit": PAGE_SIZE,
                },
                token=token,
            )
            page = [
                m
                for m in data.get("messages") or ()
                if float(low) < float(m["ts"]) <= float(high) and m is not parent
                and float(m["ts"]) != float(thread)
            ]
            if data.get("has_more") and page:
                # The page is the window's oldest replies: the rest lie after its last one.
                reached = max(float(m["ts"]) for m in page)
                narrower = max(reached, float(high) - width / 2)
                width = max(THREAD_TAIL_MIN_WINDOW_S, float(high) - narrower)
                continue
            collected = page + collected
            wanted += sum(1 for m in page if m.get("subtype") in MESSAGE_SUBTYPES)
            high = low
            if len(page) < PAGE_SIZE // 4:
                width *= THREAD_TAIL_GROWTH
        collected.sort(key=lambda m: float(m["ts"]))
        if float(high) <= floor:  # the walk met the head: nothing in between is missing
            return head + collected
        return ([parent] if parent else []) + collected

    def _poll_thread(
        self,
        ref: ConversationRef,
        channel: str,
        thread: str,
        token: str,
        *,
        position: str,
        wanted: int | None,
    ):
        """New replies in one thread after ``position`` (a ts), oldest first; the cursor is the last one passed.

        As in a channel's poll, the newest minute (by Slack's clock when it gave one)
        waits for the next poll, so a reply that shows up late is not passed over.
        """
        events = []
        started = position
        self._server_now = None
        found = self._replies(channel, thread, token, oldest=position)
        now = self.clock()
        if self._server_now is not None:
            now = min(now, self._server_now)
        for item in found:
            if wanted and len(events) >= wanted:
                break
            if float(item["ts"]) > now - CLOCK_SKEW_S:
                break
            position = str(item["ts"])
            if item.get("subtype") not in MESSAGE_SUBTYPES:
                continue  # Slack's own notices: passed, not reported
            events.append(
                Event(
                    kind="message.created",
                    channel=NAME,
                    delivery_id=f"slack:{channel}:{position}",
                    cursor=position,
                    message=self._message(ref, item, token),
                )
            )
        return with_final_cursor(events, position if position != started else None)

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

        user = (
            self._user(str(data["user"]), token)
            if data.get("user") and not data.get("bot_id")
            else None
        ) or {}
        profile = user.get("profile") or {}
        return Message(
            id=ts,
            conversation=conversation,
            author=ChannelIdentity(
                channel=NAME,
                native_id=str(data.get("user") or data.get("bot_id") or ""),
                handle=user.get("name") or None,
                display_name=(data.get("bot_profile") or {}).get("name")
                or profile.get("display_name")
                or profile.get("real_name")
                or user.get("real_name")
                or data.get("username")
                or None,
                is_bot=bool(data.get("bot_id") or user.get("is_bot")),
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
                    loader=(
                        loader(
                            f["url_private"],
                            f.get("mimetype") or "application/octet-stream",
                        )
                        if f.get("url_private")
                        else None
                    ),
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
        """The channel's latest top-level messages (``limit``, else 50), oldest first; for a thread, its parent and replies."""
        token = self._require_token("read a channel")
        channel = self._channel_id(ref, token)
        oldest = f"{since.timestamp():.6f}" if since is not None else None
        count = max(1, int(limit)) if limit else READ_DEFAULT
        thread = self._where(ref)[1]
        if thread:
            found = self._replies(channel, thread, token, oldest=oldest, newest=count)
        else:
            found = self._history(channel, token, oldest=oldest, limit=count)
        messages = [
            self._message(ref, data, token)
            for data in found
            if data.get("subtype") in MESSAGE_SUBTYPES
        ]
        return window(messages, since=since, limit=count)

    def poll(
        self,
        ref: ConversationRef,
        *,
        cursor: str | None = None,
        limit: int | None = None,
    ):
        """Top-level messages newer than ``cursor``, oldest first; a first poll looks back a day.

        Slack returns history newest first, so a poll walks forward through time windows
        (``oldest`` = the cursor's ``ts``, ``latest`` = it plus a width, both included):
        each window comes back whole and is delivered before the next, and the cursor moves
        to its end, never past Slack's own clock (its ``Date``) less a minute. A window
        holding a full page is narrowed, from that page's own spread, and asked again; an
        empty one widens the next. The cursor is ``<ts>/<width>``, so a poll that only
        narrowed or widened still moves forward, and a plain ``ts`` (an older cursor) is
        read too. A poll stops after :data:`MAX_REQUESTS_PER_POLL` requests, at ``limit``
        events, or at a rate limit, keeping what it delivered: the next resumes there.
        """
        token = self._require_token("listen to a channel")
        match = CURSOR_RE.match(str(cursor)) if cursor else None
        if cursor and not match and not str(cursor).startswith(EVENTS_CURSOR):
            raise ChannelError(
                f"a Slack cursor is <ts>/<width> or a message ts, not {cursor!r}",
                kind="validation",
            )
        channel = self._channel_id(ref, token)
        thread = self._where(ref)[1]
        if self._delivery() == "events":
            return self._poll_received(ref, channel, thread, token, cursor, limit)
        now = self.clock()
        position = match["ts"] if match else f"{now - LISTEN_LOOKBACK_S:.6f}"
        if thread:
            return self._poll_thread(
                ref,
                channel,
                thread,
                token,
                position=position,
                wanted=max(1, int(limit)) if limit else None,
            )
        width = float(match["width"]) if match and match["width"] else WINDOW_START_S
        width = min(WINDOW_MAX_S, max(WINDOW_MIN_S, width))
        started = (float(position), width)
        end = now - CLOCK_SKEW_S
        wanted = max(1, int(limit)) if limit else None
        requests, events = 0, []
        self._server_now = None

        def deliverable(item: Mapping[str, Any], latest: float) -> bool:
            return float(position) < float(item["ts"]) <= latest

        while float(position) < end and requests < MAX_REQUESTS_PER_POLL:
            latest = min(float(position) + width, end)
            params = {
                "channel": channel,
                "oldest": position,
                "latest": f"{latest:.6f}",
                "inclusive": "true",
                "limit": PAGE_SIZE,
            }
            try:
                data = self._api("conversations.history", params, token=token)
            except ChannelError as error:
                if error.kind != "rate_limited" or not requests:
                    raise
                break  # what was read stands; the next poll resumes after it
            requests += 1
            if (
                self._server_now is not None
            ):  # Slack's clock bounds the walk, not this one
                end = min(end, self._server_now - CLOCK_SKEW_S)
            found = list(data.get("messages") or ())
            if data.get("has_more"):
                if width > WINDOW_MIN_S:
                    # Too full: narrow to what this page suggests holds less than a page
                    # (its own messages span ``span`` seconds), so one request finds it.
                    stamps = sorted(float(m["ts"]) for m in found)
                    span = stamps[-1] - stamps[0] if len(stamps) > 1 else 0.0
                    width = max(
                        WINDOW_MIN_S, min(width / 2, span * 0.8 if span else width / 2)
                    )
                    continue
                try:  # more than a page within the narrowest window: read its pages
                    found = self._window(channel, token, params, found, data)
                except ChannelError as error:
                    if error.kind != "rate_limited":
                        raise
                    break
            found = sorted(
                (m for m in found if deliverable(m, latest)),
                key=lambda m: float(m["ts"]),
            )
            for item in found:
                if wanted and len(events) >= wanted:
                    break
                position = str(item["ts"])
                if item.get("subtype") in MESSAGE_SUBTYPES:  # Slack's own notices pass
                    events.append(
                        Event(
                            kind="message.created",
                            channel=NAME,
                            delivery_id=f"slack:{channel}:{position}",
                            cursor=_cursor(position, width),
                            message=self._message(ref, item, token),
                        )
                    )
            else:
                # The whole window was read: move to its end, never past Slack's own now.
                reached = min(latest, end)
                if reached > float(position):
                    position = f"{reached:.6f}"
                if not found:
                    width = min(WINDOW_MAX_S, width * 2)
                continue
            break  # the limit stopped it inside a window
        moved = (float(position), width) != started
        return with_final_cursor(events, _cursor(position, width) if moved else None)

    def _poll_received(
        self,
        ref: ConversationRef,
        channel: str,
        thread: str | None,
        token: str,
        cursor: str | None,
        limit: int | None,
    ):
        """What the Events API receiver stored after ``cursor`` (``e<arrival ns>``) for this channel (or thread), in arrival order.

        New messages are ``message.created``, edits ``message.updated`` and deletions
        ``message.deleted``. A first listen, or one whose cursor came from polling, looks
        back a day; stored events older than a week are deleted on the way.
        """
        now_ns = int(self.clock() * 10**9)
        text = str(cursor or "")
        if text.startswith(EVENTS_CURSOR) and text[1:].isdigit():
            after = int(text[1:])
        elif not text or CURSOR_RE.match(text):
            after = now_ns - EVENTS_LOOKBACK_NS
        else:
            raise ChannelError(
                f"a Slack events cursor is {EVENTS_CURSOR}<arrival ns>, not {cursor!r}",
                kind="validation",
            )
        wanted = max(1, int(limit)) if limit else None
        received = sorted(
            (int(match["stamp"]), key)
            for key in list(self.events)
            if (match := RECEIVED_KEY_RE.match(key))
        )
        events: list[Event] = []
        reached = None
        for stamp, key in received:
            if stamp <= after:
                if stamp < now_ns - EVENTS_RETENTION_NS:
                    self.events.pop(key, None)
                continue
            if wanted and len(events) >= wanted:
                break
            reached = stamp
            record = self.events.get(key)
            if not isinstance(record, Mapping) or record.get("channel") != channel:
                continue
            event = record.get("event")
            if not isinstance(event, Mapping):
                continue
            try:
                found = self._received_event(ref, channel, thread, event, token)
            except (AttributeError, TypeError, ValueError, KeyError):
                continue  # a record this receiver never writes (a foreign store): passed
            if found is not None:
                events.append(replace(found, cursor=f"{EVENTS_CURSOR}{stamp}"))
        return with_final_cursor(
            events, f"{EVENTS_CURSOR}{reached}" if reached is not None else None
        )

    def _received_event(
        self,
        ref: ConversationRef,
        channel: str,
        thread: str | None,
        event: Mapping[str, Any],
        token: str,
    ) -> Event | None:
        """One stored ``message`` event as listen reports it, or ``None`` when it is not for ``ref``."""
        subtype = event.get("subtype")
        if subtype == "message_deleted":
            previous = event.get("previous_message") or {}
            deleted = str(event.get("deleted_ts") or previous.get("ts") or "")
            if not deleted or (
                thread and thread not in (deleted, previous.get("thread_ts"))
            ):
                return None
            return Event(
                kind="message.deleted",
                channel=NAME,
                delivery_id=f"slack:{channel}:{deleted}:deleted",
                payload={"message_id": deleted, "conversation": ref.encoded},
            )
        edited = subtype == "message_changed"
        data = event.get("message") if edited else event
        if not isinstance(data, Mapping) or not data.get("ts"):
            return None
        if not edited and subtype not in MESSAGE_SUBTYPES:
            return None  # Slack's own notices
        if edited and data.get("subtype") not in MESSAGE_SUBTYPES:
            return None
        if thread and thread not in (str(data["ts"]), data.get("thread_ts")):
            return None
        if (
            not thread
            and data.get("thread_ts") not in (None, data["ts"])
            and (data.get("subtype") != "thread_broadcast")
        ):
            return None  # a reply inside a thread: its thread's reference reports it
        ts = str(data["ts"])
        edit_ts = (data.get("edited") or {}).get("ts") or event.get("event_ts")
        return Event(
            kind="message.updated" if edited else "message.created",
            channel=NAME,
            delivery_id=(
                f"slack:{channel}:{ts}:edited:{edit_ts}"
                if edited
                else f"slack:{channel}:{ts}"
            ),
            message=self._message(ref, dict(data), token),
        )

    def _window(
        self, channel: str, token: str, params: dict, first: list, data: dict
    ) -> list[dict]:
        """Every page of one window (newest first, as Slack pages), without waiting out a rate limit."""
        found = list(first)
        while data.get("has_more"):
            page_cursor = (data.get("response_metadata") or {}).get("next_cursor")
            if not page_cursor:
                break
            data = self._api(
                "conversations.history", {**params, "cursor": page_cursor}, token=token
            )
            found += list(data.get("messages") or ())
        return found

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
        """``crypto`` when ``X-Slack-Signature`` matches the signing secret over a fresh timestamp, ``forged`` otherwise.

        Raises :class:`~correspond.errors.MissingRequirement` when the signing secret is
        unset (a configuration error, never a verdict on the request). Anything that is not
        a well-formed signed request (headers that are not a mapping, a body that is not
        bytes, a timestamp that is not a short run of digits) is graded ``forged``, never
        raised. Replay protection is the five-minute window only: act idempotently on an
        event's id.
        """
        secret = require(NAME, "signing_secret", run=self.run)

        def forged(reason: str) -> Authenticity:
            return Authenticity(grade=Grade.FORGED, evidence={"signature": reason})

        if not isinstance(headers, Mapping):
            return forged("no headers")
        if not isinstance(body, (bytes, bytearray, memoryview)):
            return forged("body is not bytes")
        try:
            lowered = {str(k).lower(): str(v).strip() for k, v in headers.items()}
        except Exception:  # a hostile mapping or value: never a valid request
            return forged("no headers")
        stamp = lowered.get("x-slack-request-timestamp", "")
        received = lowered.get("x-slack-signature", "")
        if not (
            stamp.isascii() and stamp.isdigit() and len(stamp) <= TIMESTAMP_MAX_DIGITS
        ):
            return forged("no timestamp")
        if not received:
            return forged("missing")
        base = b"v0:" + stamp.encode() + b":" + bytes(body)
        expected = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
        if not (received.isascii() and hmac.compare_digest(received, expected)):
            return forged("mismatch")
        # Checked after the signature, so a stale verdict is never given to a forgery.
        if abs(self.clock() - int(stamp)) > SIGNATURE_MAX_AGE_S:
            return forged("stale (a replay?)")
        return Authenticity(grade=Grade.CRYPTO, evidence={"signature": "valid"})

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
        name, thread = self._where(ref)
        if thread and draft.reply_to and draft.reply_to != thread:
            raise ChannelError(
                f"slack:{ref.id} names thread {thread}; reply_to {draft.reply_to!r} names another (leave it out, or send to the channel)",
                kind="validation",
            )
        if thread:
            draft = replace(draft, reply_to=thread)
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
        webhook_set = bool(os.environ.get(webhook_env(name), "").strip())
        # A channel's own webhook is the operator's explicit choice for it, a reply excepted.
        via_webhook = webhook_set and not draft.reply_to
        plan: dict[str, Any] = {
            "action": (
                "post through the channel's incoming webhook"
                if via_webhook
                else (
                    "post with the token (chat.postMessage)"
                    if token
                    else "post with the token if one is found when sending, else through the channel's incoming webhook"
                )
            ),
            "channel": name,
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
                f"{webhook_env(name)} (or the Keychain item {webhook_keychain_service(name)})",
            ]
        if dry_run:
            return SendResult(
                ok=True, channel=NAME, conversation=ref.encoded, dry_run=True, plan=plan
            )
        if via_webhook:
            return self._post_webhook(ref, text, plan)
        if token is None and not draft.reply_to and self._webhook(name):
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
        self,
        ref: ConversationRef,
        text: str,
        thread: str | None,
        token: str,
        plan: dict,
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
        workspace = self._workspace(token)
        if workspace.account is None:
            try:
                data = self._api("auth.test", {}, token=token)
            except ChannelError:
                return None
            workspace.account = Account(
                channel=NAME,
                id=str(data.get("user_id") or data.get("bot_id") or ""),
                acts_as="bot" if data.get("bot_id") else "user",
            )
        return workspace.account

    def _post_webhook(self, ref: ConversationRef, text: str, plan: dict) -> SendResult:
        url = self._webhook(self._where(ref)[0])
        if url is None:
            raise MissingRequirement(
                NAME,
                f"no Slack token and no incoming webhook for slack:{self._where(ref)[0]}",
                fix=(
                    f"set CORRESPOND_SLACK_TOKEN, or create an incoming webhook for the channel (Slack app settings, "
                    f"Incoming Webhooks) and set {webhook_env(self._where(ref)[0])} or the Keychain item "
                    f"{webhook_keychain_service(self._where(ref)[0])}; without either, a dry run gives the text to paste"
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
                f"could not post to slack:{self._where(ref)[0]}: {error}",
                kind=error.kind,
                retryable=error.retryable,
            ) from None
        if reply.status != 200:
            answer = reply.body.decode("utf-8", "replace").strip()[:200]
            kind = _WEBHOOK_ERRORS.get(
                reply.status, "unavailable" if reply.status >= 500 else "validation"
            )
            raise ChannelError(
                f"Slack refused the post to slack:{self._where(ref)[0]} ({reply.status}: {answer or 'no reason given'})",
                kind=kind,
                retryable=kind == "rate_limited" or reply.status >= 500,
            )
        return SendResult(
            ok=True,
            channel=NAME,
            conversation=ref.encoded,
            plan={**plan, "via": "webhook"},
        )
