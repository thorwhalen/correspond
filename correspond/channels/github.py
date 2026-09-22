"""GitHub: issues, pull request conversations and discussions, through the ``gh`` CLI.

correspond never holds a GitHub token: every call is ``gh api``, authenticated as whatever
account ``gh`` is logged in as on this machine, so writes act as that user. Request bodies
go to ``gh`` on stdin, never on its command line, so a long comment cannot hit the argument
limit and never shows in the process table.

References:

- ``github:owner/repo``: the repository. ``read`` lists recent open issues and pull
  requests, ``listen`` polls its issue and comment activity, ``send`` opens an issue (a
  title is required).
- ``github:owner/repo#N``: an issue, a pull request, or a discussion. GitHub numbers the
  three in one sequence; correspond asks which. ``label`` and ``unlabel`` work on an issue
  or pull request only: GitHub discussions have categories, not labels.

Message ids match the anchors in GitHub's own URLs: ``issue-N`` (the opening post of issue
or pull request N), ``issuecomment-ID``, ``discussion-N``, ``discussioncomment-ID``.

Messages read through the API are ``platform`` (GitHub authenticated the account), with the
author's ``author_association`` (``OWNER``, ``MEMBER``, ``CONTRIBUTOR``, ``NONE``, …) as
``authority``. A webhook delivery is graded by :meth:`GitHub.verify`: ``crypto`` when its
``X-Hub-Signature-256`` matches the configured secret, ``forged`` otherwise.

:meth:`GitHub.audience` says who can read a conversation: the repository's readership,
from its visibility, and its collaborators when the account may list them.

>>> GitHub().parse_ref("OctoCat/Hello-World#1").encoded
'github:octocat/hello-world#1'
>>> signature("a-secret", b"{}")
'sha256=...'
"""

from __future__ import annotations

import hashlib
import hmac
import itertools
import json
import re
import subprocess
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote

from correspond.channels._http import classify, retry_after_seconds
from correspond.errors import (
    ChannelError,
    CorrespondError,
    InvalidRef,
    MissingRequirement,
    NotSupported,
)
from correspond.model import (
    Account,
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
    format_time,
    parse_time,
)
from correspond.ops import window, with_final_cursor
from correspond.registry import require

__all__ = [
    "APPS_AND_WEBHOOKS",
    "BASE_PERMISSION_DEFAULT",
    "GitHub",
    "REACTIONS",
    "SECURITY_MANAGERS",
    "UNLISTED_COLLABORATORS",
    "WATCHERS",
    "signature",
]

NAME = "github"
REF_RE = re.compile(
    r"^(?P<owner>[A-Za-z0-9][A-Za-z0-9-]{0,38})/(?P<repo>[A-Za-z0-9._-]{1,100})(?:#(?P<number>[1-9][0-9]{0,9}))?$"
)
MESSAGE_ID_RE = re.compile(
    r"^(?P<kind>issue|issuecomment|discussion|discussioncomment)-(?P<id>[1-9][0-9]{0,19})$"
)
NODE_ID_RE = re.compile(r"^(?P<kind>D|DC)_[A-Za-z0-9_-]{4,}$")
REACTIONS = ("+1", "-1", "laugh", "confused", "heart", "hooray", "rocket", "eyes")
_GRAPHQL_REACTIONS = dict(
    zip(
        REACTIONS,
        (
            "THUMBS_UP",
            "THUMBS_DOWN",
            "LAUGH",
            "CONFUSED",
            "HEART",
            "HOORAY",
            "ROCKET",
            "EYES",
        ),
    )
)
MAX_BODY_CHARS = 65_536
MAX_TITLE_CHARS = 256
PAGE_SIZE = 100
MAX_PAGES = 20
READ_REPOSITORY_DEFAULT = 30
LISTEN_LOOKBACK = timedelta(hours=24)
CLOCK_SKEW = timedelta(seconds=5)
SECONDARY_LIMIT_WAIT_S = 60.0
GH_TIMEOUT_S = 120

_ACTOR = "author{__typename login ... on User{databaseId} ... on Bot{databaseId}}"
_POST = (
    "id databaseId body url createdAt lastEditedAt authorAssociation isMinimized "
    + _ACTOR
)
DISCUSSION_QUERY = (
    "query($owner:String!,$name:String!,$number:Int!,$after:String){"
    "repository(owner:$owner,name:$name){discussion(number:$number){"
    "id number title body url createdAt lastEditedAt authorAssociation locked category{name} "
    + _ACTOR
    + " comments(first:100,after:$after){pageInfo{hasNextPage endCursor} nodes{"
    + _POST
    + " replies(first:100){totalCount nodes{"
    + _POST
    + "}}}}}}}"
)
DISCUSSION_ID_QUERY = (
    "query($owner:String!,$name:String!,$number:Int!){"
    "repository(owner:$owner,name:$name){discussion(number:$number){id}}}"
)
ADD_DISCUSSION_COMMENT = (
    "mutation($discussionId:ID!,$body:String!){"
    "addDiscussionComment(input:{discussionId:$discussionId,body:$body}){comment{databaseId url}}}"
)
ADD_DISCUSSION_REPLY = (
    "mutation($discussionId:ID!,$body:String!,$replyToId:ID!){"
    "addDiscussionComment(input:{discussionId:$discussionId,body:$body,replyToId:$replyToId}){comment{databaseId url}}}"
)
UPDATE_DISCUSSION = "mutation($id:ID!,$body:String!){updateDiscussion(input:{discussionId:$id,body:$body}){discussion{url}}}"
UPDATE_DISCUSSION_COMMENT = "mutation($id:ID!,$body:String!){updateDiscussionComment(input:{commentId:$id,body:$body}){comment{url}}}"
ADD_REACTION = "mutation($id:ID!,$content:ReactionContent!){addReaction(input:{subjectId:$id,content:$content}){reaction{content}}}"
_GRAPHQL_ERRORS = {
    "NOT_FOUND": "not_found",
    "FORBIDDEN": "permission",
    "RATE_LIMITED": "rate_limited",
    "UNPROCESSABLE": "validation",
    "INTERNAL": "unavailable",
}
_PLATFORM = Authenticity(
    grade=Grade.PLATFORM, evidence={"attested_by": "the GitHub API, through gh"}
)

#: Reader classes of a repository that cannot be listed, as :meth:`GitHub.audience` words them.
WATCHERS = "watchers and participants receive the body by email"
BASE_PERMISSION_DEFAULT = "every member of the organisation through its base permission (documented default: read)"
SECURITY_MANAGERS = "members of teams with the organisation's security manager role (read access to every repository)"
UNLISTED_COLLABORATORS = "collaborators and team members the gh account cannot list"
APPS_AND_WEBHOOKS = "apps and webhooks with access to the repository, which can copy its conversations elsewhere"
#: GitHub lists a repository's collaborators only to accounts with one of these permissions.
LISTING_PERMISSIONS = ("admin", "maintain", "push")
PUBLIC_DURABILITY = (
    "indexed",
    "archived_by_others",
    "copies_pushed",
    "edit_history_visible",
)
PRIVATE_DURABILITY = ("copies_pushed", "edit_history_visible")
PUBLIC_WIDENING = ("forks", "visibility_flip")
PRIVATE_WIDENING = ("joiners_read_history", "forks", "visibility_flip")
_REFUSED_LISTING = ("permission", "not_found")


def signature(secret: str | bytes, body: bytes) -> str:
    """The ``X-Hub-Signature-256`` value GitHub sends for ``body`` under ``secret``."""
    key = secret.encode("utf-8") if isinstance(secret, str) else secret
    return "sha256=" + hmac.new(key, body, hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class _Reply:
    status: int
    headers: Mapping[str, str] = field(default_factory=dict)
    text: str = ""

    def json(self) -> Any:
        try:
            return json.loads(self.text) if self.text.strip() else None
        except json.JSONDecodeError:
            return None


def _parse_reply(output: str) -> _Reply | None:
    """``gh api -i`` output (a status line, headers, a blank line, the body), or ``None``."""
    if not output.startswith("HTTP/"):
        return None
    breaks = [(i, sep) for sep in ("\r\n\r\n", "\n\n") if (i := output.find(sep)) >= 0]
    if breaks:
        index, separator = min(breaks)
        head, body = output[:index], output[index + len(separator) :]
    else:
        head, body = output, ""
    lines = head.splitlines()
    try:
        status = int(lines[0].split()[1])
    except (IndexError, ValueError):
        return None
    headers = {}
    for line in lines[1:]:
        key, colon, rest = line.partition(":")
        if colon:
            headers[key.strip().lower()] = rest.strip()
    return _Reply(status, headers, body)


def _gh_error(stderr: str, returncode: int) -> ChannelError:
    text = stderr.strip() or f"gh exited with status {returncode}"
    lowered = text.lower()
    if (
        "gh auth login" in lowered
        or "not logged in" in lowered
        or "authentication" in lowered
    ):
        return ChannelError(
            f"gh is not logged in ({text}); run `gh auth login`", kind="auth"
        )
    if any(
        word in lowered
        for word in (
            "could not resolve",
            "connection",
            "timed out",
            "timeout",
            "network",
            "no such host",
            "tls",
        )
    ):
        return ChannelError(
            f"could not reach GitHub: {text}", kind="network", retryable=True
        )
    return ChannelError(f"gh failed: {text}", kind="unavailable")


def _status_error(reply: _Reply, what: str) -> ChannelError:
    data = reply.json()
    message = (
        str(data["message"])
        if isinstance(data, dict) and data.get("message")
        else reply.text.strip()[:300]
    )
    headers = reply.headers
    remaining = headers.get("x-ratelimit-remaining")
    if reply.status == 429 or (
        reply.status == 403 and (remaining == "0" or "rate limit" in message.lower())
    ):
        wait = retry_after_seconds(headers)
        reset = headers.get("x-ratelimit-reset", "")
        if wait is None and remaining == "0" and reset.isdigit():
            wait = max(0.0, int(reset) - time.time())
        return ChannelError(
            f"GitHub rate limit on {what}: {message}",
            kind="rate_limited",
            retryable=True,
            retry_after=SECONDARY_LIMIT_WAIT_S if wait is None else wait,
        )
    if reply.status == 401:
        return ChannelError(
            f"GitHub refused gh's credentials on {what}: {message}; run `gh auth login`",
            kind="auth",
        )
    return classify(
        reply.status, f"GitHub answered {reply.status} to {what}: {message}", headers
    )


def _http_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        moment = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _not_found(owner: str, repo: str, number: int) -> ChannelError:
    return ChannelError(
        f"no issue, pull request or discussion #{number} in {owner}/{repo} that the gh account can see",
        kind="not_found",
    )


def _decode_cursor(
    cursor: str | None, default: datetime
) -> tuple[datetime, datetime, frozenset[str]]:
    """``(position, openings_from, seen)`` from a listen cursor (JSON, or a bare ISO time)."""
    if not cursor:
        return default, default, frozenset()
    try:
        if cursor.lstrip().startswith("{"):
            data = json.loads(cursor)
            position = parse_time(data["t"])
            openings_from = parse_time(data.get("l") or data["t"])
            return (
                position,
                min(openings_from, position),
                frozenset(str(key) for key in data.get("seen") or ()),
            )
        moment = parse_time(cursor)
        return moment, moment, frozenset()
    except (CorrespondError, ValueError, KeyError, TypeError, AttributeError):
        raise ChannelError(
            f"{cursor!r} is not a GitHub listen cursor", kind="validation"
        ) from None


def _encode_cursor(position: datetime, openings_from: datetime, seen: set[str]) -> str:
    return json.dumps(
        {
            "t": format_time(position),
            "l": format_time(openings_from),
            "seen": sorted(seen),
        },
        separators=(",", ":"),
    )


def _reader(user: Mapping[str, Any]) -> ChannelIdentity:
    """An account that can read a repository, as GitHub lists it; no ``is_self``, which would depend on who asks."""
    login = str(user.get("login") or "")
    return ChannelIdentity(
        channel=NAME,
        native_id=str(user.get("id") or ""),
        handle=login or None,
        is_bot=user.get("type") == "Bot" or login.endswith("[bot]"),
    )


class GitHub:
    """GitHub through ``gh``: read, listen, send, edit, react, verify webhook deliveries, and say who reads a conversation."""

    name = NAME

    def __init__(self, *, run: Callable[..., Any] = subprocess.run, gh: str = "gh"):
        self.run = run
        self.gh = gh
        self._login: str | None = None

    @property
    def capabilities(self) -> Capabilities:
        """Issues, pull request conversations and discussions."""
        return Capabilities(
            channel=NAME,
            read=Support.FULL,
            listen=Support.PARTIAL,
            send=Support.FULL,
            edit=Support.FULL,
            react=Support.FULL,
            verify=Support.FULL,
            audience=Support.FULL,
            label=Support.FULL,
            unlabel=Support.FULL,
            initiate=Support.FULL,
            reply=Support.PARTIAL,
            history_depth=HistoryDepth.FULL,
            listen_modes=("poll",),
            grades=(Grade.PLATFORM, Grade.CRYPTO, Grade.FORGED),
            max_text_length=MAX_BODY_CHARS,
            max_title_length=MAX_TITLE_CHARS,
            reactions=REACTIONS,
            formats=("markdown",),
            native_fields=(
                "title",
                "state",
                "labels",
                "number",
                "kind",
                "locked",
                "category",
                "node_id",
                "minimized",
                "replies_truncated",
            ),
            rate_limits=(
                "80 content-creating requests per minute and 500 per hour (secondary limits)",
                "5,000 API requests per hour for a user token",
            ),
            notes=(
                "listen polls issue and comment activity of a repository or of one issue; discussions are read but not listened to",
                "reply_to works in discussions only; issue and pull request comments are flat",
                "pull request review comments on diffs are not read",
                "listen can miss a change only when more than 2,000 issues or comments change within the same second",
                "audience is the repository's readership and never complete (installed apps, webhooks and security managers cannot be listed); a 404 or 403 resolves to public",
                "label and unlabel apply to issues and pull requests, not discussions (GitHub discussions have categories, not labels); listen does not yet surface who applied a label",
            ),
        )

    def parse_ref(self, id: str) -> ConversationRef:
        """``owner/repo`` or ``owner/repo#N``, lower-cased (GitHub names are case-insensitive)."""
        match = REF_RE.match(id or "")
        if not match:
            raise InvalidRef(
                f"github references are github:owner/repo or github:owner/repo#number, not github:{id}"
            )
        repository = ConversationRef(
            channel=NAME,
            id=f"{match['owner']}/{match['repo']}".lower(),
            kind="repository",
        )
        if match["number"] is None:
            return repository
        return ConversationRef(
            channel=NAME, id=f"{repository.id}#{int(match['number'])}", parent=repository
        )

    @staticmethod
    def _parts(ref: ConversationRef) -> tuple[str, str, int | None]:
        repository, _, number = ref.id.partition("#")
        owner, _, repo = repository.partition("/")
        return owner, repo, int(number) if number else None

    # ------------------------------------------------------------------ plumbing

    def _gh(self, args: list[str], stdin: str | None = None) -> Any:
        try:
            return self.run(
                [self.gh, *args],
                input=stdin,
                capture_output=True,
                text=True,
                timeout=GH_TIMEOUT_S,
            )
        except FileNotFoundError:
            raise MissingRequirement(
                NAME,
                f"the gh CLI ({self.gh}) is not on PATH",
                fix="install it from https://cli.github.com/ and run `gh auth login`",
            ) from None
        except subprocess.TimeoutExpired:
            raise ChannelError(
                f"gh did not answer within {GH_TIMEOUT_S} s",
                kind="network",
                retryable=True,
            ) from None

    def _api(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: Any = None,
        allow: Iterable[int] = (),
    ) -> _Reply:
        args = ["api", "-i", "--method", method, path]
        if payload is not None:
            args += ["--input", "-"]
        proc = self._gh(args, None if payload is None else json.dumps(payload))
        reply = _parse_reply(proc.stdout or "")
        if reply is None:
            raise _gh_error(proc.stderr or "", proc.returncode)
        if reply.status >= 400 and reply.status not in set(allow):
            raise _status_error(reply, f"{method} {path.split('?', 1)[0]}")
        return reply

    def _graphql(self, query: str, variables: Mapping[str, Any]) -> dict:
        data = (
            self._api(
                "graphql",
                method="POST",
                payload={"query": query, "variables": dict(variables)},
            ).json()
            or {}
        )
        errors = data.get("errors") or []
        if errors:
            first = errors[0] if isinstance(errors[0], dict) else {}
            kind = _GRAPHQL_ERRORS.get(first.get("type"), "validation")
            raise ChannelError(
                f"GitHub GraphQL: {first.get('message', 'error')}",
                kind=kind,
                retryable=kind == "rate_limited",
                retry_after=SECONDARY_LIMIT_WAIT_S if kind == "rate_limited" else None,
            )
        return data.get("data") or {}

    def _collect(
        self, path: str, *, bounded: bool = True
    ) -> tuple[list[Any], bool, str | None]:
        """Every item of a paginated list: ``(items, truncated, the server's Date on the last page)``; ``bounded`` stops after ``MAX_PAGES``."""
        separator = "&" if "?" in path else "?"
        items: list[Any] = []
        date = None
        for page in range(1, MAX_PAGES + 1) if bounded else itertools.count(1):
            reply = self._api(f"{path}{separator}per_page={PAGE_SIZE}&page={page}")
            date = reply.headers.get("date", date)
            batch = reply.json() or []
            items.extend(batch)
            if len(batch) < PAGE_SIZE:
                return items, False, date
        return items, True, date

    def _whoami(self) -> str | None:
        if self._login is None:
            try:
                self._login = str((self._api("user").json() or {}).get("login") or "")
            except ChannelError:
                self._login = ""
        return self._login or None

    def _account(self) -> Account | None:
        login = self._whoami()
        return Account(channel=NAME, id=login, acts_as="user") if login else None

    def _identity(
        self, user: Mapping[str, Any] | None, association: str | None
    ) -> ChannelIdentity:
        if not user:
            return ChannelIdentity(
                channel=NAME,
                native_id="",
                handle="ghost",
                display_name="a deleted account",
                authority=association,
            )
        login = str(user.get("login") or "")
        return ChannelIdentity(
            channel=NAME,
            native_id=str(user.get("id") or user.get("databaseId") or ""),
            handle=login or None,
            is_bot=user.get("type") == "Bot"
            or user.get("__typename") == "Bot"
            or login.endswith("[bot]"),
            is_self=bool(self._login) and login.lower() == str(self._login).lower(),
            authority=association,
        )

    # ------------------------------------------------------------------- reading

    def read(
        self,
        ref: ConversationRef,
        *,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[Message]:
        """An issue, pull request or discussion with its comments; for a repository, recent open issues and pull requests."""
        owner, repo, number = self._parts(ref)
        self._whoami()
        if number is None:
            return self._read_repository(owner, repo, since, limit)
        issue = self._api(f"repos/{owner}/{repo}/issues/{number}", allow=(404, 410))
        if issue.status < 400:
            messages = self._issue_messages(
                owner, repo, number, issue.json() or {}, since
            )
        else:
            messages = self._discussion_messages(owner, repo, number)
        return window(messages, since=since, limit=limit)

    def _issue_conversation(
        self, owner: str, repo: str, issue: Mapping[str, Any]
    ) -> ConversationRef:
        repository = ConversationRef(
            channel=NAME, id=f"{owner}/{repo}", kind="repository"
        )
        return ConversationRef(
            channel=NAME,
            id=f"{owner}/{repo}#{issue['number']}",
            kind="pull_request" if issue.get("pull_request") else "issue",
            parent=repository,
        )

    def _opening(
        self, conversation: ConversationRef, issue: Mapping[str, Any]
    ) -> Message:
        return Message(
            id=f"issue-{issue['number']}",
            conversation=conversation,
            author=self._identity(issue.get("user"), issue.get("author_association")),
            authenticity=_PLATFORM,
            sent_at=parse_time(issue["created_at"]),
            text=issue.get("body") or "",
            body=issue.get("body") or "",
            body_format="markdown",
            url=issue.get("html_url"),
            native={
                "title": issue.get("title"),
                "state": issue.get("state"),
                "labels": [
                    label.get("name") if isinstance(label, dict) else str(label)
                    for label in issue.get("labels") or []
                ],
                "number": issue["number"],
                "kind": conversation.kind,
                "locked": bool(issue.get("locked")),
            },
            raw=issue,
        )

    def _comment(
        self, conversation: ConversationRef, comment: Mapping[str, Any]
    ) -> Message:
        created = parse_time(comment["created_at"])
        updated = parse_time(comment.get("updated_at"))
        return Message(
            id=f"issuecomment-{comment['id']}",
            conversation=conversation,
            author=self._identity(comment.get("user"), comment.get("author_association")),
            authenticity=_PLATFORM,
            sent_at=created,
            text=comment.get("body") or "",
            body=comment.get("body") or "",
            body_format="markdown",
            edited_at=updated if updated and updated != created else None,
            url=comment.get("html_url"),
            raw=comment,
        )

    def _issue_messages(
        self,
        owner: str,
        repo: str,
        number: int,
        issue: Mapping[str, Any],
        since: datetime | None,
    ) -> list[Message]:
        conversation = self._issue_conversation(owner, repo, issue)
        path = f"repos/{owner}/{repo}/issues/{number}/comments"
        if since is not None:
            path += f"?since={format_time(since)}"
        comments, _, _ = self._collect(path, bounded=False)
        return [
            self._opening(conversation, issue),
            *(self._comment(conversation, c) for c in comments),
        ]

    def _read_repository(
        self, owner: str, repo: str, since: datetime | None, limit: int | None
    ) -> list[Message]:
        count = max(1, min(PAGE_SIZE, limit or READ_REPOSITORY_DEFAULT))
        path = f"repos/{owner}/{repo}/issues?state=open&sort=created&direction=desc&per_page={count}"
        if since is not None:
            path += f"&since={format_time(since)}"
        issues = self._api(path).json() or []
        return window(
            (self._opening(self._issue_conversation(owner, repo, i), i) for i in issues),
            since=since,
            limit=limit,
        )

    def _discussion(self, owner: str, repo: str, number: int) -> tuple[dict, list[dict]]:
        comments: list[dict] = []
        after = None
        discussion: dict = {}
        for _ in range(MAX_PAGES):
            try:
                data = self._graphql(
                    DISCUSSION_QUERY,
                    {"owner": owner, "name": repo, "number": number, "after": after},
                )
            except ChannelError as error:
                if error.kind != "not_found":
                    raise
                data = {}
            discussion = (data.get("repository") or {}).get("discussion") or {}
            if not discussion:
                raise _not_found(owner, repo, number)
            page = discussion.get("comments") or {}
            comments.extend(page.get("nodes") or [])
            page_info = page.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                break
            after = page_info.get("endCursor")
        return discussion, comments

    def _discussion_messages(self, owner: str, repo: str, number: int) -> list[Message]:
        discussion, comments = self._discussion(owner, repo, number)
        repository = ConversationRef(
            channel=NAME, id=f"{owner}/{repo}", kind="repository"
        )
        conversation = ConversationRef(
            channel=NAME,
            id=f"{owner}/{repo}#{number}",
            kind="discussion",
            parent=repository,
        )
        messages = [
            Message(
                id=f"discussion-{number}",
                conversation=conversation,
                author=self._identity(
                    discussion.get("author"), discussion.get("authorAssociation")
                ),
                authenticity=_PLATFORM,
                sent_at=parse_time(discussion["createdAt"]),
                text=discussion.get("body") or "",
                body=discussion.get("body") or "",
                body_format="markdown",
                edited_at=parse_time(discussion.get("lastEditedAt")),
                url=discussion.get("url"),
                native={
                    "title": discussion.get("title"),
                    "category": (discussion.get("category") or {}).get("name"),
                    "locked": bool(discussion.get("locked")),
                    "number": number,
                    "kind": "discussion",
                    "node_id": discussion.get("id"),
                },
                raw=discussion,
            )
        ]
        for comment in comments:
            root = f"discussioncomment-{comment['databaseId']}"
            replies = comment.get("replies") or {}
            nodes = replies.get("nodes") or []
            messages.append(
                self._discussion_post(
                    conversation,
                    comment,
                    replies_truncated=(replies.get("totalCount") or 0) > len(nodes),
                )
            )
            messages.extend(
                self._discussion_post(conversation, r, reply_to=root) for r in nodes
            )
        return messages

    def _discussion_post(
        self,
        conversation: ConversationRef,
        post: Mapping[str, Any],
        *,
        reply_to: str | None = None,
        replies_truncated: bool = False,
    ) -> Message:
        native: dict[str, Any] = {
            "node_id": post.get("id"),
            "minimized": bool(post.get("isMinimized")),
        }
        if replies_truncated:
            native["replies_truncated"] = True
        return Message(
            id=f"discussioncomment-{post['databaseId']}",
            conversation=conversation,
            author=self._identity(post.get("author"), post.get("authorAssociation")),
            authenticity=_PLATFORM,
            sent_at=parse_time(post["createdAt"]),
            text=post.get("body") or "",
            body=post.get("body") or "",
            body_format="markdown",
            edited_at=parse_time(post.get("lastEditedAt")),
            url=post.get("url"),
            reply_to=reply_to,
            thread_root=reply_to,
            native=native,
            raw=post,
        )

    # ------------------------------------------------------------------ audience

    def audience(self, ref: ConversationRef, *, draft: Draft | None = None) -> Audience:
        """Who can read a repository's issues, pull requests and discussions, asked of GitHub now.

        Every conversation in a repository has the repository's readership. A draft changes
        nothing: a mention decides who is notified, not who can read. One
        ``gh api repos/{owner}/{repo}`` call gives the visibility and the owner's type:

        - ``public``: scope ``public``; readers are not listed.
        - ``private``: scope ``named`` when a user owns it, ``org`` otherwise.
        - ``internal``: scope ``org``, with every member of the enterprise as a class.
        - a 404 or a 403: ``public``, defaulted. GitHub answers 404 both for a repository
          that does not exist and for one the account cannot see.

        For a private or internal repository, collaborators (team members, members reading
        through the base permission, and owners included) are listed only when the account
        can write to it, since GitHub lists them to no one else. The list is a lower bound
        and the audience is never ``complete``: installed apps and webhooks read the
        repository without being listable through ``gh``, and an organisation's
        security-manager teams read every repository. An organisation's base permission is
        read when the account may see it (owners only), and otherwise assumed to be the
        documented default, read; ``read``, ``write`` and ``admin`` reach the same readers
        and give the same class. The ``gh`` account itself is never consulted, so the answer,
        and its hash, do not depend on who asks. Every repository emails bodies to watchers
        and participants, and nothing sent is retractable. Nothing is cached. A rate limit or
        a transport failure raises; :func:`correspond.ops.audience` resolves it to public.
        """
        owner, repo, _ = self._parts(ref)
        path = f"repos/{owner}/{repo}"
        reply = self._api(path, allow=(403, 404))
        if reply.status >= 400:
            error = _status_error(reply, f"GET {path}")
            if error.kind == "rate_limited":
                raise error
            why = (
                "GitHub answers 404 for a repository that does not exist and for one the gh account cannot see"
                if reply.status == 404
                else "the gh account may not read this repository"
            )
            return Audience.unknown(ref.encoded, str(error), why)
        data = reply.json()
        if not isinstance(data, dict):
            return Audience.unknown(
                ref.encoded, f"GET {path} answered without a repository"
            )
        visibility = data.get("visibility")
        if visibility is None and isinstance(data.get("private"), bool):
            visibility = "private" if data["private"] else "public"
        owner_data = data.get("owner") or {}
        evidence = [
            f"GET {path}: visibility {visibility}, owned by "
            f"{owner_data.get('login') or owner} ({owner_data.get('type') or 'owner type not given'})"
        ]
        if visibility == "public":
            return Audience(
                ref=ref.encoded,
                scope=Scope.PUBLIC,
                classes=(WATCHERS,),
                external=True,
                durability=PUBLIC_DURABILITY,
                widening=PUBLIC_WIDENING,
                evidence=(
                    *evidence,
                    "anyone can read a public repository, so its readers are not listed",
                ),
            )
        if visibility not in ("private", "internal"):
            return Audience.unknown(
                ref.encoded,
                *evidence,
                f"visibility {visibility!r} is not public, private or internal",
            )
        return self._restricted_audience(
            ref, data, visibility=visibility, evidence=evidence
        )

    def _restricted_audience(
        self,
        ref: ConversationRef,
        data: Mapping[str, Any],
        *,
        visibility: str,
        evidence: list[str],
    ) -> Audience:
        owner, repo, _ = self._parts(ref)
        path = f"repos/{owner}/{repo}"
        owner_data = data.get("owner") or {}
        by_user = visibility == "private" and owner_data.get("type") == "User"
        readers = [_reader(owner_data)] if by_user else []
        classes = [WATCHERS, APPS_AND_WEBHOOKS]
        listed = False
        permissions = data.get("permissions") or {}
        if any(permissions.get(p) for p in LISTING_PERMISSIONS):
            collaborators, listed, note = self._collaborators(path)
            readers += collaborators
            evidence.append(note)
            if not by_user:
                evidence.append(self._teams(path))
        else:
            evidence.append(
                "the gh account cannot write to the repository, and GitHub lists collaborators only to accounts that can"
            )
        if not listed:
            classes.append(UNLISTED_COLLABORATORS)
        if not by_user:
            classes += self._organisation_classes(owner, evidence)
        if visibility == "internal":
            classes.append(
                f"every member of every organisation in the enterprise that owns {owner} (internal visibility)"
            )
        # Two accounts reading a user's repository mean one of them is not the operator,
        # whichever it is; in an organisation they may all be the operator's colleagues.
        external = True if by_user and len(set(readers)) > 1 else None
        return Audience(
            ref=ref.encoded,
            scope=Scope.NAMED if by_user else Scope.ORG,
            readers=readers,
            complete=False,
            classes=classes,
            external=external,
            durability=PRIVATE_DURABILITY,
            widening=PRIVATE_WIDENING,
            evidence=evidence,
        )

    def _collaborators(self, path: str) -> tuple[list[ChannelIdentity], bool, str]:
        """``(identities, whether that is all of them, evidence)``; a refusal lists none."""
        try:
            people, truncated, _ = self._collect(f"{path}/collaborators")
        except ChannelError as error:
            if error.kind not in _REFUSED_LISTING:
                raise
            return [], False, f"collaborators could not be listed: {error}"
        note = f"GET {path}/collaborators: {len(people)} collaborator(s)"
        if truncated:
            note += f", cut at {MAX_PAGES * PAGE_SIZE}, so the list is a lower bound"
        return [_reader(p) for p in people], not truncated, note

    def _teams(self, path: str) -> str:
        try:
            teams, truncated, _ = self._collect(f"{path}/teams")
        except ChannelError as error:
            if error.kind not in _REFUSED_LISTING:
                raise
            return f"teams could not be listed: {error}"
        names = ", ".join(
            f"{t.get('slug') or t.get('name')} ({t.get('permission')})" for t in teams
        )
        return f"GET {path}/teams: {names or 'none'}" + (" (cut)" if truncated else "")

    def _organisation_classes(self, owner: str, evidence: list[str]) -> list[str]:
        path = f"orgs/{owner}"
        reply = self._api(path, allow=(403, 404))
        if reply.status == 403:
            error = _status_error(reply, f"GET {path}")
            if error.kind == "rate_limited":
                raise error
        found = reply.json() if reply.status < 400 else None
        base = (
            found.get("default_repository_permission")
            if isinstance(found, dict)
            else None
        )
        if base == "none":
            evidence.append(
                f"GET {path}: base permission none, so membership alone reads nothing"
            )
            return [SECURITY_MANAGERS]
        if base is None:
            evidence.append(
                f"GET {path}: the base permission is visible only to organisation owners; assumed the documented default, read"
            )
        else:
            # read, write and admin reach the same readers: one class, so one hash.
            evidence.append(f"GET {path}: base permission {base}")
        return [SECURITY_MANAGERS, BASE_PERMISSION_DEFAULT]

    # ----------------------------------------------------------------- listening

    def poll(
        self, ref: ConversationRef, *, cursor: str | None = None, limit: int | None = None
    ):
        """Issue and comment activity since ``cursor``; a first poll looks back a day.

        The cursor is opaque JSON: the update time the next poll starts from (``t``; GitHub's
        ``since`` includes it), the creation time from which an issue still counts as new
        (``l``, which stays behind while the issues list is being read in parts), and the
        events already delivered at ``t`` (``seen``). So neither ``limit`` nor several changes
        within one second can make a poll repeat itself, and a list cut short (more than
        ``MAX_PAGES`` pages changed) is resumed from where it was cut. A bare ISO time is also
        accepted as a cursor.
        """
        owner, repo, number = self._parts(ref)
        position, openings_from, seen = _decode_cursor(
            cursor, (datetime.now(timezone.utc) - LISTEN_LOOKBACK).replace(microsecond=0)
        )
        stamp = format_time(position)
        self._whoami()
        repository = ConversationRef(
            channel=NAME, id=f"{owner}/{repo}", kind="repository"
        )
        if number is None:
            comments, comments_cut, date = self._collect(
                f"repos/{owner}/{repo}/issues/comments?sort=updated&direction=asc&since={stamp}"
            )
            issues, issues_cut, _ = self._collect(
                f"repos/{owner}/{repo}/issues?state=all&sort=updated&direction=asc&since={stamp}"
            )
        else:
            probe = self._api(f"repos/{owner}/{repo}/issues/{number}", allow=(404, 410))
            if probe.status >= 400:
                self._discussion_id(
                    owner, repo, number
                )  # raises not_found when it is not one
                raise NotSupported(
                    "listen to a discussion",
                    NAME,
                    alternatives=(f"read github:{owner}/{repo}#{number} again later",),
                )
            # One issue's comments come ordered by id, not by update, so a cut list would not be
            # complete up to any time: read every page (an issue holds a bounded number).
            comments, comments_cut, date = self._collect(
                f"repos/{owner}/{repo}/issues/{number}/comments?since={stamp}",
                bounded=False,
            )
            issues, issues_cut = [], False

        candidates: list[tuple[datetime, str, str, Message]] = []
        for comment in comments:
            issue_number = (
                str(comment.get("issue_url", "")).rstrip("/").rsplit("/", 1)[-1]
            )
            conversation = ConversationRef(
                channel=NAME, id=f"{owner}/{repo}#{issue_number}", parent=repository
            )
            changed = comment.get("updated_at") or comment["created_at"]
            is_new = parse_time(comment["created_at"]) >= openings_from
            candidates.append(
                (
                    parse_time(changed),
                    f"issuecomment-{comment['id']}@{changed}",
                    "message.created" if is_new else "message.updated",
                    self._comment(conversation, comment),
                )
            )
        for issue in issues:
            if parse_time(issue["created_at"]) < openings_from:
                continue  # an older issue that changed: its comments arrive as their own events
            candidates.append(
                (
                    parse_time(issue["created_at"]),
                    f"issue-{issue['number']}@{issue['created_at']}",
                    "message.created",
                    self._opening(self._issue_conversation(owner, repo, issue), issue),
                )
            )
        candidates = [c for c in candidates if not (c[0] <= position and c[1] in seen)]
        # A list cut short is complete only up to its last item: later events wait for the
        # next poll, which starts from there.
        horizons = [
            parse_time(items[-1].get("updated_at") or items[-1]["created_at"])
            for items, cut in ((comments, comments_cut), (issues, issues_cut))
            if cut and items
        ]
        horizon = min(horizons) if horizons else None
        if horizon is not None:
            candidates = [c for c in candidates if c[0] <= horizon]
        candidates.sort(key=lambda c: (c[0], c[1]))
        limited = bool(limit) and len(candidates) > limit
        if limit:
            candidates = candidates[:limit]

        events = []
        delivered = set(seen)
        for moment, key, kind, message in candidates:
            if moment > position:
                position, delivered = moment, set()
            delivered.add(key)
            events.append(
                Event(
                    kind=kind,
                    channel=NAME,
                    delivery_id=f"github:{owner}/{repo}:{key}",
                    cursor=_encode_cursor(position, openings_from, delivered),
                    message=message,
                )
            )
        final = None  # when limited, the last event's cursor is where to resume
        if not limited and horizon is not None:
            if horizon > position:
                position, delivered = horizon, set()
            # While the issues list is read in parts, an issue created before the cut may still
            # be unread, so issues keep counting as new from where they did.
            final = _encode_cursor(
                position, openings_from if issues_cut else position, delivered
            )
        elif not limited:
            server_now = _http_date(date)
            if server_now is not None and server_now - CLOCK_SKEW > position:
                position, delivered = server_now - CLOCK_SKEW, set()
            final = _encode_cursor(position, position, delivered)
        return with_final_cursor(events, final)

    # ------------------------------------------------------------------- writing

    def _planned(self, ref: ConversationRef, operation: str, plan: dict) -> SendResult:
        return SendResult(
            ok=True,
            channel=NAME,
            conversation=ref.encoded,
            operation=operation,
            dry_run=True,
            plan=plan,
        )

    def _done(
        self,
        conversation: str,
        operation: str,
        message_id: str,
        url: str | None,
        plan: dict,
    ):
        return SendResult(
            ok=True,
            channel=NAME,
            conversation=conversation,
            operation=operation,
            message_id=message_id,
            url=url,
            account=self._account(),
            plan=plan,
        )

    def send(
        self, ref: ConversationRef, draft: Draft, *, dry_run: bool = False
    ) -> SendResult:
        """Comment on an issue, pull request or discussion; on a repository, open an issue (``title`` required)."""
        owner, repo, number = self._parts(ref)
        if number is None:
            if not (draft.title or "").strip():
                raise ChannelError(
                    f"opening an issue in {owner}/{repo} needs a title", kind="validation"
                )
            if draft.reply_to:
                raise NotSupported(
                    "reply",
                    NAME,
                    alternatives=("comment on the issue: github:owner/repo#N",),
                )
            plan = {
                "action": "open an issue",
                "request": f"POST repos/{owner}/{repo}/issues",
                "title": draft.title,
                "body": draft.text,
            }
            if dry_run:
                return self._planned(ref, "send", plan)
            data = (
                self._api(
                    f"repos/{owner}/{repo}/issues",
                    method="POST",
                    payload={"title": draft.title, "body": draft.text},
                ).json()
                or {}
            )
            return self._done(
                f"github:{owner}/{repo}#{data.get('number')}",
                "send",
                f"issue-{data.get('number')}",
                data.get("html_url"),
                plan,
            )
        if draft.title:
            raise ChannelError("a comment has no title; leave it out", kind="validation")
        target = f"{owner}/{repo}#{number}"
        if dry_run:
            return self._planned(
                ref,
                "send",
                {
                    "action": "comment",
                    "target": target,
                    "request": f"POST repos/{owner}/{repo}/issues/{number}/comments, or GraphQL addDiscussionComment if #{number} is a discussion (checked when sending)",
                    "reply_to": draft.reply_to,
                    "body": draft.text,
                },
            )
        probe = self._api(f"repos/{owner}/{repo}/issues/{number}", allow=(404, 410))
        if probe.status < 400:
            if draft.reply_to:
                raise NotSupported(
                    "reply",
                    NAME,
                    alternatives=(
                        "issue and pull request comments are flat: quote the message you answer",
                    ),
                )
            plan = {
                "action": "comment",
                "target": target,
                "request": f"POST repos/{owner}/{repo}/issues/{number}/comments",
                "body": draft.text,
            }
            data = (
                self._api(
                    f"repos/{owner}/{repo}/issues/{number}/comments",
                    method="POST",
                    payload={"body": draft.text},
                ).json()
                or {}
            )
            return self._done(
                ref.encoded,
                "send",
                f"issuecomment-{data.get('id')}",
                data.get("html_url"),
                plan,
            )
        variables = {
            "discussionId": self._discussion_id(owner, repo, number),
            "body": draft.text,
        }
        query = ADD_DISCUSSION_COMMENT
        if draft.reply_to:
            variables["replyToId"] = self._node_id(owner, repo, number, draft.reply_to)
            query = ADD_DISCUSSION_REPLY
        plan = {
            "action": "comment on a discussion",
            "target": target,
            "request": "GraphQL addDiscussionComment",
            "reply_to": draft.reply_to,
            "body": draft.text,
        }
        comment = (self._graphql(query, variables).get("addDiscussionComment") or {}).get(
            "comment"
        ) or {}
        return self._done(
            ref.encoded,
            "send",
            f"discussioncomment-{comment.get('databaseId')}",
            comment.get("url"),
            plan,
        )

    def _discussion_id(self, owner: str, repo: str, number: int) -> str:
        try:
            data = self._graphql(
                DISCUSSION_ID_QUERY, {"owner": owner, "name": repo, "number": number}
            )
        except ChannelError as error:
            if error.kind != "not_found":
                raise
            data = {}
        found = ((data.get("repository") or {}).get("discussion") or {}).get("id")
        if not found:
            raise _not_found(owner, repo, number)
        return found

    @staticmethod
    def _message_kind(message_id: str) -> tuple[str, str]:
        node = NODE_ID_RE.match(message_id)
        if node:
            return (
                "discussion-node" if node["kind"] == "D" else "discussioncomment-node"
            ), message_id
        match = MESSAGE_ID_RE.match(message_id)
        if not match:
            raise ChannelError(
                f"GitHub message ids look like issue-12, issuecomment-345, discussion-6 or discussioncomment-789, not {message_id!r}",
                kind="validation",
            )
        return match["kind"], match["id"]

    def _node_id(self, owner: str, repo: str, number: int | None, message_id: str) -> str:
        kind, ident = self._message_kind(message_id)
        if kind.endswith("-node"):
            return ident
        if kind not in ("discussion", "discussioncomment"):
            raise ChannelError(
                f"{message_id} is not a discussion message", kind="validation"
            )
        if number is None:
            raise ChannelError(
                f"name the discussion {message_id} belongs to: github:{owner}/{repo}#N",
                kind="validation",
            )
        if kind == "discussion":
            if int(ident) != number:
                raise ChannelError(
                    f"{message_id} is not discussion #{number}", kind="validation"
                )
            return self._discussion_id(owner, repo, number)
        _, comments = self._discussion(owner, repo, number)
        for comment in comments:
            for post in (comment, *((comment.get("replies") or {}).get("nodes") or [])):
                if str(post.get("databaseId")) == ident:
                    return post["id"]
        raise ChannelError(
            f"{message_id} is not in discussion #{number}", kind="not_found"
        )

    def edit(
        self,
        ref: ConversationRef,
        message_id: str,
        draft: Draft,
        *,
        dry_run: bool = False,
    ) -> SendResult:
        """Replace the body of an issue, a comment, a discussion or a discussion comment."""
        owner, repo, number = self._parts(ref)
        kind, ident = self._message_kind(message_id)
        rest = {
            "issue": f"repos/{owner}/{repo}/issues/{ident}",
            "issuecomment": f"repos/{owner}/{repo}/issues/comments/{ident}",
        }.get(kind)
        mutation = (
            UPDATE_DISCUSSION
            if kind in ("discussion", "discussion-node")
            else UPDATE_DISCUSSION_COMMENT
        )
        plan = {
            "action": "edit",
            "message": message_id,
            "request": f"PATCH {rest}"
            if rest
            else f"GraphQL {'updateDiscussion' if mutation is UPDATE_DISCUSSION else 'updateDiscussionComment'}",
            "body": draft.text,
        }
        if dry_run:
            return self._planned(ref, "edit", plan)
        if rest:
            url = (
                self._api(rest, method="PATCH", payload={"body": draft.text}).json() or {}
            ).get("html_url")
        else:
            data = self._graphql(
                mutation,
                {
                    "id": self._node_id(owner, repo, number, message_id),
                    "body": draft.text,
                },
            )
            result = (
                data.get("updateDiscussion") or data.get("updateDiscussionComment") or {}
            )
            url = (result.get("discussion") or result.get("comment") or {}).get("url")
        return self._done(ref.encoded, "edit", message_id, url, plan)

    def react(
        self,
        ref: ConversationRef,
        message_id: str,
        reaction: str,
        *,
        dry_run: bool = False,
    ) -> SendResult:
        """Add one of :data:`REACTIONS` to an issue, a comment, a discussion or a discussion comment."""
        owner, repo, number = self._parts(ref)
        kind, ident = self._message_kind(message_id)
        rest = {
            "issue": f"repos/{owner}/{repo}/issues/{ident}/reactions",
            "issuecomment": f"repos/{owner}/{repo}/issues/comments/{ident}/reactions",
        }.get(kind)
        plan = {
            "action": "react",
            "message": message_id,
            "reaction": reaction,
            "request": f"POST {rest}" if rest else "GraphQL addReaction",
        }
        if dry_run:
            return self._planned(ref, "react", plan)
        if rest:
            self._api(rest, method="POST", payload={"content": reaction})
        else:
            self._graphql(
                ADD_REACTION,
                {
                    "id": self._node_id(owner, repo, number, message_id),
                    "content": _GRAPHQL_REACTIONS[reaction],
                },
            )
        return self._done(ref.encoded, "react", message_id, None, plan)

    def _require_issue(self, owner: str, repo: str, number: int, operation: str) -> None:
        """Raise unless ``#number`` is an issue or pull request: GitHub discussions have categories, not labels."""
        probe = self._api(f"repos/{owner}/{repo}/issues/{number}", allow=(404, 410))
        if probe.status < 400:
            return
        self._discussion_id(
            owner, repo, number
        )  # raises not_found when there is nothing at all
        raise NotSupported(
            operation,
            NAME,
            alternatives=("labels apply to issues and pull requests, not discussions",),
        )

    @staticmethod
    def _labels(labels: Iterable[str]) -> list[str]:
        names = [str(name).strip() for name in labels]
        if not all(names):
            raise ChannelError("a label name is empty", kind="validation")
        return list(dict.fromkeys(names))  # de-duplicated, order kept

    def label(
        self, ref: ConversationRef, labels: Iterable[str], *, dry_run: bool = False
    ) -> SendResult:
        """Add labels to an issue or pull request (``POST .../labels``).

        GitHub creates a label that does not already exist in the repository rather than
        rejecting it, so ``landed`` in the plan (the response's own label list) is mostly a
        confirmation; it is still read back and reported, the way an assignee is not: a
        login that is not a member of the repository is silently dropped, a label name
        never is.
        """
        owner, repo, number = self._parts(ref)
        if number is None:
            raise ChannelError(
                f"labels apply to an issue or pull request: github:{owner}/{repo}#N",
                kind="validation",
            )
        names = self._labels(labels)
        target = f"{owner}/{repo}#{number}"
        path = f"repos/{owner}/{repo}/issues/{number}/labels"
        plan = {
            "action": "add labels",
            "target": target,
            "request": f"POST {path}",
            "labels": names,
        }
        if dry_run:
            return self._planned(ref, "label", plan)
        self._require_issue(owner, repo, number, "label")
        data = self._api(path, method="POST", payload={"labels": names}).json() or []
        landed = [
            item.get("name")
            for item in data
            if isinstance(item, dict) and item.get("name")
        ]
        plan["landed"] = landed
        missing = [name for name in names if name not in landed]
        if missing:
            plan["not_applied"] = missing
        return self._done(ref.encoded, "label", f"issue-{number}", None, plan)

    def unlabel(
        self, ref: ConversationRef, labels: Iterable[str], *, dry_run: bool = False
    ) -> SendResult:
        """Remove labels from an issue or pull request.

        GitHub has no bulk removal: one ``DELETE .../labels/{name}`` per label. A label
        already absent from the issue answers 404 the same way a missing issue does, so the
        issue's existence is checked first (:meth:`_require_issue`) and every 404 after that
        is read as "was not on the issue", not as an error.
        """
        owner, repo, number = self._parts(ref)
        if number is None:
            raise ChannelError(
                f"labels apply to an issue or pull request: github:{owner}/{repo}#N",
                kind="validation",
            )
        names = self._labels(labels)
        target = f"{owner}/{repo}#{number}"
        plan = {
            "action": "remove labels",
            "target": target,
            "request": f"DELETE repos/{owner}/{repo}/issues/{number}/labels/{{label}}, one call per label",
            "labels": names,
        }
        if dry_run:
            return self._planned(ref, "unlabel", plan)
        self._require_issue(owner, repo, number, "unlabel")
        removed: list[str] = []
        already_absent: list[str] = []
        for name in names:
            path = f"repos/{owner}/{repo}/issues/{number}/labels/{quote(name, safe='')}"
            reply = self._api(path, method="DELETE", allow=(404,))
            (already_absent if reply.status == 404 else removed).append(name)
        plan["removed"] = removed
        if already_absent:
            plan["already_absent"] = already_absent
        return self._done(ref.encoded, "unlabel", f"issue-{number}", None, plan)

    # ----------------------------------------------------------------- verifying

    def verify(self, headers: Mapping[str, str], body: bytes) -> Authenticity:
        """``crypto`` when ``X-Hub-Signature-256`` matches the webhook secret, ``forged`` otherwise."""
        secret = require(NAME, "webhook_secret", run=self.run)
        lowered = {str(k).lower(): str(v) for k, v in headers.items()}
        evidence = {
            "delivery": lowered.get("x-github-delivery", ""),
            "event": lowered.get("x-github-event", ""),
        }
        received = lowered.get("x-hub-signature-256", "")
        if (
            received.isascii()
            and received
            and hmac.compare_digest(received, signature(secret, body))
        ):
            return Authenticity(
                grade=Grade.CRYPTO, evidence={**evidence, "signature": "valid"}
            )
        return Authenticity(
            grade=Grade.FORGED,
            evidence={**evidence, "signature": "mismatch" if received else "missing"},
        )
