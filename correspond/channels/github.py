"""GitHub: issues, pull request conversations and discussions, through the ``gh`` CLI.

correspond never holds a GitHub token: every call is ``gh api``, authenticated as whatever
account ``gh`` is logged in as on this machine, so writes act as that user. Request bodies
go to ``gh`` on stdin, never on its command line, so a long comment cannot hit the argument
limit and never shows in the process table.

References:

- ``github:owner/repo``: the repository. ``read`` lists recent open issues and pull
  requests, ``listen`` polls its issue, comment, review comment and discussion activity,
  ``send`` opens an issue (a title is required), or a discussion when ``Draft.native``
  names a ``category``.
- ``github:owner/repo#N``: an issue, a pull request, or a discussion. GitHub numbers the
  three in one sequence; correspond asks which. Reading or listening to a pull request
  includes its review comments on the diff, each threaded under the comment that opened
  its thread (``reply_to``), and a reply to one of them joins that thread.
  ``label`` and ``unlabel`` work on an issue
  or pull request only: GitHub discussions have categories, not labels. ``close`` and
  ``reopen`` work on all three: an issue or pull request through ``PATCH .../issues/N``,
  a discussion through GraphQL ``closeDiscussion`` / ``reopenDiscussion``. Each kind
  takes its own close reasons (issue: ``completed``, ``not_planned``, ``duplicate``;
  discussion: ``resolved``, ``outdated``, ``duplicate``, with ``completed`` accepted as
  ``resolved``).

Opening an issue takes ``Draft.native`` keys ``labels``, ``assignees`` and ``milestone``,
set in the creating request so watchers are notified once. GitHub silently drops an
assignee who cannot be assigned, so the plan reports what the created issue actually
carries (``landed``) and what did not stick (``not_applied``). ``Draft.native``
``category`` (a discussion category's name or slug) opens a discussion instead, through
GraphQL ``createDiscussion``; it takes none of the issue fields.

Message ids match the anchors in GitHub's own URLs: ``issue-N`` (the opening post of issue
or pull request N), ``issuecomment-ID``, ``discussion_rID`` (a review comment on a pull
request's diff), ``discussion-N``, ``discussioncomment-ID``.

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
import logging
import re
import subprocess
import time
from collections.abc import Callable, Iterable, Mapping, MutableMapping
from dataclasses import dataclass, field, replace
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
from correspond.notify import handle, missing_mentions, with_mentions
from correspond.ops import window, with_final_cursor
from correspond.registry import require, value

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
#: What each message id prefix names. GitHub's own anchors: a review comment on the diff is
#: ``discussion_rID`` (no dash), a review's summary is ``pullrequestreview-ID``.
MESSAGE_ID_KINDS = {
    "issue-": "issue",
    "issuecomment-": "issuecomment",
    "discussion-": "discussion",
    "discussioncomment-": "discussioncomment",
    "discussion_r": "reviewcomment",
    "pullrequestreview-": "review",
}
MESSAGE_ID_RE = re.compile(
    rf"^(?P<prefix>{'|'.join(map(re.escape, MESSAGE_ID_KINDS))})(?P<id>[1-9][0-9]{{0,19}})$"
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
#: A handle GitHub notifies on mention: a user (``octocat``) or a team (``org/team``).
GITHUB_HANDLE_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})(?:/[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9_])?)?$"
)
MAX_TITLE_CHARS = 256
#: The ``Draft.native`` keys opening an issue takes (``POST /repos/{owner}/{repo}/issues``).
ISSUE_FIELDS = ("labels", "assignees", "milestone")
#: The ``Draft.native`` key that opens a discussion instead: its category's name or slug.
DISCUSSION_FIELD = "category"
DRAFT_FIELDS = (*ISSUE_FIELDS, DISCUSSION_FIELD)
#: An issue's ``state_reason`` when closing it.
CLOSE_REASONS = ("completed", "not_planned", "duplicate")
#: A discussion's close reasons (GraphQL ``DiscussionCloseReason``, lower-cased here).
DISCUSSION_CLOSE_REASONS = ("resolved", "outdated", "duplicate")
#: Issue reasons a discussion close accepts under its own name.
DISCUSSION_REASON_ALIASES = {"completed": "resolved"}
PAGE_SIZE = 100
MAX_PAGES = 20
READ_REPOSITORY_DEFAULT = 30
LISTEN_LOOKBACK = timedelta(hours=24)
#: A repository listen re-reads this many most recently active discussions whole on every
#: poll, whatever their ``updatedAt``: some replies move neither it nor the activity order.
RECENT_DISCUSSIONS = 3
#: Where listen takes discussion activity from: the poll alone, or the poll plus the
#: discussions the webhook receiver (:mod:`correspond.channels.github_webhook`) saw.
DELIVERIES = ("poll", "webhook")
#: How long the receiver's deliveries are kept.
WEBHOOK_RETENTION = timedelta(days=7)
#: How long a comment's webhook delivery may trail the comment: a discussion a
#: delivery names is read from that much before the delivery arrived, so a reply
#: created before a poll whose delivery came after it is still found.
WEBHOOK_LAG = timedelta(minutes=10)
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
    + " replies(last:100){totalCount nodes{"
    + _POST
    + "}}}}}}}"
)
#: All of one discussion comment's replies, oldest first, for a comment with more than
#: :data:`DISCUSSION_QUERY` reads inline (its latest 100).
COMMENT_REPLIES_QUERY = (
    "query($id:ID!,$after:String){node(id:$id){... on DiscussionComment{"
    "replies(first:100,after:$after){pageInfo{hasNextPage endCursor} nodes{"
    + _POST
    + "}}}}}"
)
#: Discussions by latest activity, newest first: GitHub has no ``since`` for discussions.
DISCUSSION_ACTIVITY_QUERY = (
    "query($owner:String!,$name:String!,$after:String){"
    "repository(owner:$owner,name:$name){discussions(first:100,after:$after,"
    "orderBy:{field:UPDATED_AT,direction:DESC}){pageInfo{hasNextPage endCursor} nodes{number updatedAt}}}}"
)
DISCUSSION_CATEGORIES_QUERY = (
    "query($owner:String!,$name:String!){repository(owner:$owner,name:$name){"
    "id hasDiscussionsEnabled discussionCategories(first:100){nodes{id name slug}}}}"
)
CREATE_DISCUSSION = (
    "mutation($repositoryId:ID!,$categoryId:ID!,$title:String!,$body:String!){"
    "createDiscussion(input:{repositoryId:$repositoryId,categoryId:$categoryId,title:$title,body:$body})"
    "{discussion{number url category{name}}}}"
)
DISCUSSION_ID_QUERY = (
    "query($owner:String!,$name:String!,$number:Int!){"
    "repository(owner:$owner,name:$name){discussion(number:$number){id}}}"
)
DISCUSSION_STATE_QUERY = (
    "query($owner:String!,$name:String!,$number:Int!){"
    "repository(owner:$owner,name:$name){discussion(number:$number){id closed stateReason url}}}"
)
CLOSE_DISCUSSION = (
    "mutation($discussionId:ID!,$reason:DiscussionCloseReason){"
    "closeDiscussion(input:{discussionId:$discussionId,reason:$reason})"
    "{discussion{closed stateReason url}}}"
)
REOPEN_DISCUSSION = (
    "mutation($discussionId:ID!){"
    "reopenDiscussion(input:{discussionId:$discussionId}){discussion{closed stateReason url}}}"
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
_log = logging.getLogger(__name__)


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


def _discussion_state(discussion: Mapping[str, Any]) -> str | None:
    """``"open"`` / ``"closed"`` from a GraphQL discussion's ``closed`` flag, as issues say it."""
    closed = discussion.get("closed")
    if closed is None:
        return None
    return "closed" if closed else "open"


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


def _read_back(fields: Mapping[str, Any], issue: Mapping[str, Any]) -> dict:
    """What of the creation ``fields`` the created ``issue`` actually carries.

    GitHub answers 201 and silently drops an assignee it cannot assign (a login with no
    access to the repository), so a successful call is not proof. The response is the
    created issue, which is the read-back: ``landed`` per field, and ``not_applied`` for
    what was asked and is not there. Logins compare without case, as GitHub's do.

    >>> _read_back({"assignees": ["Ada", "eve"]}, {"assignees": [{"login": "ada"}]})
    {'landed': {'assignees': ['Ada']}, 'not_applied': {'assignees': ['eve']}}
    """
    if not fields:
        return {}
    present = {
        "labels": {
            str(item.get("name")).lower()
            for item in issue.get("labels") or ()
            if isinstance(item, Mapping)
        },
        "assignees": {
            str(item.get("login")).lower()
            for item in issue.get("assignees") or ()
            if isinstance(item, Mapping)
        },
    }
    landed: dict[str, Any] = {}
    missing: dict[str, Any] = {}
    for name in ("labels", "assignees"):
        if name in fields:
            landed[name] = [v for v in fields[name] if v.lower() in present[name]]
            gone = [v for v in fields[name] if v.lower() not in present[name]]
            if gone:
                missing[name] = gone
    if "milestone" in fields:
        number = (issue.get("milestone") or {}).get("number")
        if number == fields["milestone"]:
            landed["milestone"] = number
        else:
            missing["milestone"] = fields["milestone"]
    return {"landed": landed, **({"not_applied": missing} if missing else {})}


def _reader(user: Mapping[str, Any]) -> ChannelIdentity:
    """An account that can read a repository, as GitHub lists it; no ``is_self``, which would depend on who asks."""
    login = str(user.get("login") or "")
    return ChannelIdentity(
        channel=NAME,
        native_id=str(user.get("id") or ""),
        handle=login or None,
        is_bot=user.get("type") == "Bot" or login.endswith("[bot]"),
    )


@dataclass(frozen=True)
class _PollScope:
    """What ``GitHub.poll`` is listening to: a repository, or one issue/pull request/discussion."""

    owner: str
    repo: str
    number: int | None
    stamp: str  # the cursor's position as ``since``
    position: datetime
    openings_from: datetime
    repository: ConversationRef
    probe: Any = None  # the response for ``number`` as an issue, when there is a number


@dataclass(frozen=True)
class PollSource:
    """One list ``GitHub.poll`` reads: the cursor logic is written once over these.

    ``read(github, scope)`` returns ``(items, cut, server_date)``, or ``None`` when the
    source does not apply to the scope. ``candidates(github, scope, items)`` returns
    ``(moment, key, kind, message)`` tuples. ``changed(item)`` is the time a list item is
    ordered by (for a list cut short). ``counts_openings``: while this list is read in
    parts, the creation time from which an item still counts as new stays behind.
    """

    name: str
    read: Callable[..., Any]
    candidates: Callable[..., list]
    changed: Callable[[dict], str] = lambda item: (
        item.get("updated_at") or item["created_at"]
    )
    counts_openings: bool = False


def _read_issue_comments(github, scope):
    if scope.number is None:
        return github._collect(
            f"repos/{scope.owner}/{scope.repo}/issues/comments?sort=updated&direction=asc&since={scope.stamp}"
        )
    if scope.probe.status >= 400:
        return None  # not an issue: a discussion
    # One issue's comments come ordered by id, not by update, so a cut list would not be
    # complete up to any time: read every page (an issue holds a bounded number).
    return github._collect(
        f"repos/{scope.owner}/{scope.repo}/issues/{scope.number}/comments?since={scope.stamp}",
        bounded=False,
    )


def _issue_comment_candidates(github, scope, comments):
    found = []
    for comment in comments:
        issue_number = str(comment.get("issue_url", "")).rstrip("/").rsplit("/", 1)[-1]
        conversation = ConversationRef(
            channel=NAME,
            id=f"{scope.owner}/{scope.repo}#{issue_number}",
            parent=scope.repository,
        )
        changed = comment.get("updated_at") or comment["created_at"]
        is_new = parse_time(comment["created_at"]) >= scope.openings_from
        found.append(
            (
                parse_time(changed),
                f"issuecomment-{comment['id']}@{changed}",
                "message.created" if is_new else "message.updated",
                github._comment(conversation, comment),
            )
        )
    return found


def _read_issues(github, scope):
    if scope.number is not None:
        return None
    return github._collect(
        f"repos/{scope.owner}/{scope.repo}/issues?state=all&sort=updated&direction=asc&since={scope.stamp}"
    )


def _issue_candidates(github, scope, issues):
    found = []
    for issue in issues:
        if parse_time(issue["created_at"]) < scope.openings_from:
            continue  # an older issue that changed: its comments arrive as their own events
        found.append(
            (
                parse_time(issue["created_at"]),
                f"issue-{issue['number']}@{issue['created_at']}",
                "message.created",
                github._opening(
                    github._issue_conversation(scope.owner, scope.repo, issue), issue
                ),
            )
        )
    return found


def _read_review_comments(github, scope):
    if scope.number is None:
        return github._collect(
            f"repos/{scope.owner}/{scope.repo}/pulls/comments?sort=updated&direction=asc&since={scope.stamp}"
        )
    if scope.probe.status < 400 and (scope.probe.json() or {}).get("pull_request"):
        return github._collect(
            f"repos/{scope.owner}/{scope.repo}/pulls/{scope.number}/comments?since={scope.stamp}",
            bounded=False,
        )
    return None


def _review_comment_candidates(github, scope, comments):
    found = []
    for comment in comments:
        pull_number = (
            str(comment.get("pull_request_url", "")).rstrip("/").rsplit("/", 1)[-1]
        )
        conversation = ConversationRef(
            channel=NAME,
            id=f"{scope.owner}/{scope.repo}#{pull_number}",
            kind="pull_request",
            parent=scope.repository,
        )
        changed = comment.get("updated_at") or comment["created_at"]
        is_new = parse_time(comment["created_at"]) >= scope.openings_from
        found.append(
            (
                parse_time(changed),
                f"discussion_r{comment['id']}@{changed}",
                "message.created" if is_new else "message.updated",
                github._review_comment(conversation, comment),
            )
        )
    return found


def _is_review_message(review: Mapping[str, Any]) -> bool:
    """A submitted review that says something: a verdict, or a body. A pending review has no
    ``submitted_at``; a "comment" review with no body only wraps its comments on the diff."""
    if not review.get("submitted_at"):
        return False
    return bool((review.get("body") or "").strip()) or review.get("state") != "COMMENTED"


def _read_reviews(github, scope):
    """Reviews submitted since the cursor.

    The reviews list has no ``since``, so a pull request's is read whole and filtered here.
    A repository has no reviews endpoint at all: its pull requests are read by latest update
    (a review moves that), newest first, until one is older than the cursor, and each one's
    reviews are read whole.
    """
    if scope.number is not None:
        if not (
            scope.probe.status < 400 and (scope.probe.json() or {}).get("pull_request")
        ):
            return None
        pulls = [scope.number]
    else:
        pulls = github._pulls_updated_since(scope.owner, scope.repo, scope.position)
    found: list[dict] = []
    date = None
    for number in pulls:
        reviews, _, date = github._collect(
            f"repos/{scope.owner}/{scope.repo}/pulls/{number}/reviews", bounded=False
        )
        found += [
            {**r, "_pull_number": number}
            for r in reviews
            if _is_review_message(r) and parse_time(r["submitted_at"]) >= scope.position
        ]
    return found, False, date


def _review_candidates(github, scope, reviews):
    found = []
    for review in reviews:
        conversation = ConversationRef(
            channel=NAME,
            id=f"{scope.owner}/{scope.repo}#{review['_pull_number']}",
            kind="pull_request",
            parent=scope.repository,
        )
        submitted = review["submitted_at"]
        found.append(
            (
                parse_time(submitted),
                f"pullrequestreview-{review['id']}@{submitted}",
                "message.created",  # a review is delivered once, when it is submitted
                github._review(
                    conversation, {k: v for k, v in review.items() if k != "_pull_number"}
                ),
            )
        )
    return found


def _read_discussions(github, scope):
    if scope.number is not None:
        if scope.probe.status < 400:
            return None
        # Not an issue: read as a discussion, which raises not_found when it is not one.
        return [(scope.number, scope.position)], False, scope.probe.headers.get("date")
    try:
        numbers = github._active_discussions(
            scope.owner, scope.repo, scope.position, recent=github.recent_discussions
        )
    except ChannelError as error:
        if error.kind not in _REFUSED_LISTING:
            raise
        # A token that may not read discussions still listens to everything else.
        _log.warning(
            "listen %s/%s: discussions skipped: %s", scope.owner, scope.repo, error
        )
        return None
    since = dict.fromkeys(numbers, scope.position)
    if github._delivery() == "webhook":
        hinted = github._webhook_discussions(scope.owner, scope.repo, scope.position)
        for number, received in hinted.items():
            since[number] = min(since.get(number, scope.position), received - WEBHOOK_LAG)
    return list(since.items()), False, None


def _discussion_candidates(github, scope, numbers):
    found = []
    for discussion_number, since in numbers:
        try:
            found += github._discussion_activity(
                scope.owner, scope.repo, discussion_number, since
            )
        except ChannelError as error:
            if scope.number is not None or error.kind != "not_found":
                raise
            continue  # deleted between the walk and the read: nothing left to deliver
    return found


# Adding a poll source (label events, ...) is one entry here.
POLL_SOURCES: tuple[PollSource, ...] = (
    PollSource("issue comments", _read_issue_comments, _issue_comment_candidates),
    PollSource("issues", _read_issues, _issue_candidates, counts_openings=True),
    PollSource("review comments", _read_review_comments, _review_comment_candidates),
    PollSource("reviews", _read_reviews, _review_candidates),
    PollSource("discussions", _read_discussions, _discussion_candidates),
)


class GitHub:
    """GitHub through ``gh``: read, listen, send, edit, react, verify webhook deliveries, and say who reads a conversation."""

    name = NAME

    def __init__(
        self,
        *,
        run: Callable[..., Any] = subprocess.run,
        gh: str = "gh",
        recent_discussions: int = RECENT_DISCUSSIONS,
        deliveries: MutableMapping[str, Any] | None = None,
        clock: Callable[[], int] = time.time_ns,
    ):
        self.run = run
        self.gh = gh
        self.recent_discussions = recent_discussions
        self._deliveries = deliveries
        self.clock = clock
        self._login: str | None = None

    @property
    def deliveries(self) -> MutableMapping[str, Any]:
        """What the webhook receiver stored (read when ``delivery`` is ``webhook``)."""
        if self._deliveries is None:
            from correspond.channels.github_webhook import webhook_store

            self._deliveries = webhook_store()
        return self._deliveries

    def _delivery(self) -> str:
        delivery = str(value(NAME, "delivery") or "poll").strip().lower()
        if delivery not in DELIVERIES:
            raise ChannelError(
                f"github delivery is one of {DELIVERIES}, not {delivery!r}",
                kind="validation",
            )
        return delivery

    def _webhook_discussions(
        self, owner: str, repo: str, since: datetime
    ) -> dict[int, datetime]:
        """``{discussion: earliest arrival}`` of the ``owner/repo`` deliveries the receiver stored at or after ``since`` (less the clock skew).

        Deliveries older than ``WEBHOOK_RETENTION`` are deleted on the way.
        """
        from correspond.channels.github_webhook import RECEIVED_KEY_RE

        after = int((since - CLOCK_SKEW).timestamp() * 10**9)
        expired = self.clock() - int(WEBHOOK_RETENTION.total_seconds() * 10**9)
        wanted = f"{owner}/{repo}".lower()
        found: dict[int, int] = {}
        for key in list(self.deliveries):
            if not RECEIVED_KEY_RE.match(key):
                continue
            record = self.deliveries.get(key) or {}
            stamp = record.get("received_ns")
            if isinstance(stamp, bool) or not isinstance(stamp, int):
                continue
            if stamp < expired:
                self.deliveries.pop(key, None)
                continue
            number = record.get("discussion")
            if (
                stamp >= after
                and str(record.get("repository", "")).lower() == wanted
                and isinstance(number, int)
            ):
                found[number] = min(found.get(number, stamp), stamp)
        return {
            number: datetime.fromtimestamp(stamp / 10**9, tz=timezone.utc)
            for number, stamp in found.items()
        }

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
            close=Support.FULL,
            reopen=Support.FULL,
            initiate=Support.FULL,
            reply=Support.PARTIAL,
            notify=Support.PARTIAL,
            history_depth=HistoryDepth.FULL,
            listen_modes=("poll",),
            grades=(Grade.PLATFORM, Grade.CRYPTO, Grade.FORGED),
            max_text_length=MAX_BODY_CHARS,
            max_title_length=MAX_TITLE_CHARS,
            reactions=REACTIONS,
            formats=("markdown",),
            draft_fields=DRAFT_FIELDS,
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
                "path",
                "line",
                "original_line",
                "review_id",
                "diff_hunk",
            ),
            rate_limits=(
                "80 content-creating requests per minute and 500 per hour (secondary limits)",
                "5,000 API requests per hour for a user token",
            ),
            notes=(
                "listen polls the issue, comment, pull request review comment and discussion activity of a repository, or of one issue, pull request or discussion",
                f"listen is partial on a repository's discussions: GitHub has no since filter for them, so listen walks them by GitHub's latest-activity order and reads each one active since the cursor, and the {self.recent_discussions} most recently active whatever their update time (one extra call each, recent_discussions= sets how many); a post GitHub does not count as activity on its discussion (seen on some replies) is delivered only when its discussion is among those, listening to one discussion reads it whole on every poll and misses none of its posts",
                "delivery = webhook closes that gap: listen also reads whole every discussion a discussion_comment webhook delivery named since the cursor (correspond.channels.github_webhook: mk_webhook_app verifies X-Hub-Signature-256 and stores only the repository and discussion number; serve_webhook binds 127.0.0.1:8082 unless webhook_allow_remote is set); deliveries are kept a week",
                "a gh token that may not read discussions listens to a repository's issues, comments and review comments only (logged as a warning)",
                "GitHub moves a review comment's updated_at when its review is submitted and when a push outdates it, so listen reports those as message.updated with the text unchanged, and a review comment carries no edited_at",
                "reply_to works in discussions and in pull request review threads (a reply to a reply joins its thread); issue and pull request conversation comments are flat",
                "reading or listening to a pull request includes its review comments on the diff (message ids discussion_rID, with path, line, original_line, review_id and diff_hunk in native); a pull request's reviews are read too, each review's summary as a message pullrequestreview-ID with its state (APPROVED, CHANGES_REQUESTED, COMMENTED, DISMISSED) in native; a review with no body and only a COMMENTED state is left out, and listening to a repository finds them through the pull requests updated since the cursor",
                f"a discussion comment with more than 100 replies costs one extra call per 100 to read them all (up to {MAX_PAGES * 100}; past that its latest 100, with native replies_truncated)",
                "notify mentions each handle the text lacks; GitHub notifies a mentioned user or team who can see the repository, so on a private one a non-member is not reached",
                "listen can miss a change only when more than 2,000 issues or comments change within the same second",
                "audience is the repository's readership and never complete (installed apps, webhooks and security managers cannot be listed); a 404 or 403 resolves to public",
                "label and unlabel apply to issues and pull requests, not discussions (GitHub discussions have categories, not labels); listen does not yet surface who applied a label",
                "label and unlabel each cost one extra read (confirming the issue exists, to tell a label the issue never had apart from a missing issue) before their writes",
                "native draft fields (labels, assignees, milestone) apply when opening an issue; on a comment they are refused",
                "native category (a discussion category's name or slug) on github:owner/repo opens a discussion instead of an issue; it takes no other native field, and a dry run does not check that the category exists",
                "close and reopen apply to issues, pull requests and discussions; close reasons are per kind: an issue takes completed, not_planned or duplicate, a discussion resolved, outdated or duplicate (completed is accepted as resolved), a pull request none",
                "a dry run of close or reopen reads nothing, so it plans for whichever kind the number turns out to be, and a reason the kind does not take is refused only by the real call",
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
        messages = [
            self._opening(conversation, issue),
            *(self._comment(conversation, c) for c in comments),
        ]
        if issue.get("pull_request"):
            path = f"repos/{owner}/{repo}/pulls/{number}/comments"
            if since is not None:
                path += f"?since={format_time(since)}"
            comments_on_diff, _, _ = self._collect(path, bounded=False)
            messages += [self._review_comment(conversation, c) for c in comments_on_diff]
            reviews, _, _ = self._collect(
                f"repos/{owner}/{repo}/pulls/{number}/reviews", bounded=False
            )
            messages += self._reviews(conversation, reviews, since)
        return messages

    def _reviews(
        self,
        conversation: ConversationRef,
        reviews: Iterable[Mapping[str, Any]],
        since: datetime | None = None,
    ) -> list[Message]:
        """The submitted reviews worth a message, as :meth:`_review` makes them, from ``since`` on."""
        found = []
        for review in reviews:
            if not _is_review_message(review):
                continue
            if since is not None and parse_time(review["submitted_at"]) < since:
                continue
            found.append(self._review(conversation, review))
        return found

    def _review(
        self, conversation: ConversationRef, review: Mapping[str, Any]
    ) -> Message:
        """A pull request review's own summary (``pullrequestreview-ID``); its ``state`` is in native.

        The review's comments on the diff are separate messages (``discussion_rID``) that
        name it in ``review_id``. GitHub gives a review no update time, and an edit of its
        body does not move anything, so ``edited_at`` stays unset.
        """
        return Message(
            id=f"pullrequestreview-{review['id']}",
            conversation=conversation,
            author=self._identity(review.get("user"), review.get("author_association")),
            authenticity=_PLATFORM,
            sent_at=parse_time(review["submitted_at"]),
            text=review.get("body") or "",
            body=review.get("body") or "",
            body_format="markdown",
            url=review.get("html_url"),
            native={"state": review.get("state"), "review_id": review["id"]},
            raw=review,
        )

    def _review_comment(
        self, conversation: ConversationRef, comment: Mapping[str, Any]
    ) -> Message:
        """A comment on a pull request's diff; a reply names its thread's first comment.

        GitHub threads review comments one level deep: every reply's ``in_reply_to_id``
        is the comment that opened the thread, so it is both ``reply_to`` and
        ``thread_root``. ``edited_at`` stays unset: GitHub moves a review comment's
        ``updated_at`` when its review is submitted and when a push outdates it, so it does
        not say the text changed. ``line`` is ``None`` once a push outdates the comment;
        ``original_line`` is where it was written.
        """
        parent = comment.get("in_reply_to_id")
        root = f"discussion_r{parent}" if parent else None
        return Message(
            id=f"discussion_r{comment['id']}",
            conversation=conversation,
            author=self._identity(comment.get("user"), comment.get("author_association")),
            authenticity=_PLATFORM,
            sent_at=parse_time(comment["created_at"]),
            text=comment.get("body") or "",
            body=comment.get("body") or "",
            body_format="markdown",
            url=comment.get("html_url"),
            reply_to=root,
            thread_root=root,
            native={
                "path": comment.get("path"),
                "line": comment.get("line"),
                "original_line": comment.get("original_line"),
                "review_id": comment.get("pull_request_review_id"),
                "diff_hunk": comment.get("diff_hunk"),
            },
            raw=comment,
        )

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
        for comment in comments:
            replies = comment.get("replies") or {}
            if (replies.get("totalCount") or 0) > len(replies.get("nodes") or []):
                comment["replies"] = self._all_replies(comment, replies)
        return discussion, comments

    def _all_replies(self, comment: Mapping[str, Any], inline: Mapping[str, Any]) -> dict:
        """Every reply of a busy comment (one call per 100), in place of its latest 100.

        Within :data:`MAX_PAGES` pages; past that the inline latest 100 stay, and the
        comment is marked ``replies_truncated``.
        """
        nodes: list[dict] = []
        after = None
        for _ in range(MAX_PAGES):
            data = self._graphql(
                COMMENT_REPLIES_QUERY, {"id": comment["id"], "after": after}
            )
            page = ((data.get("node") or {}).get("replies")) or {}
            nodes.extend(page.get("nodes") or [])
            page_info = page.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                return {"totalCount": len(nodes), "nodes": nodes}
            after = page_info.get("endCursor")
        return dict(inline)

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
        """Issue, comment, review comment, review and discussion activity since ``cursor``; a first poll looks back a day.

        The cursor is opaque JSON: the update time the next poll starts from (``t``; GitHub's
        ``since`` includes it), the creation time from which an issue still counts as new
        (``l``, which stays behind while the issues list is being read in parts), and the
        events already delivered at ``t`` (``seen``). So neither ``limit`` nor several changes
        within one second can make a poll repeat itself, and a list cut short (more than
        ``MAX_PAGES`` pages changed) is resumed from where it was cut. A bare ISO time is also
        accepted as a cursor.

        Discussions have no ``since`` filter, so a repository's are walked newest activity
        first until a page reaches one older than the cursor (:meth:`_active_discussions`),
        and each active one is read whole; one discussion is read whole on every poll. A
        discussion counts as new when it was created at or after the cursor, and a
        discussion comment is ``message.updated`` when it was created before it.
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
        probe = None
        if number is not None:
            probe = self._api(f"repos/{owner}/{repo}/issues/{number}", allow=(404, 410))
        scope = _PollScope(
            owner, repo, number, stamp, position, openings_from, repository, probe
        )

        candidates: list[tuple[datetime, str, str, Message]] = []
        horizons: list[datetime] = []
        # The server's Date on each list read: every list is complete up to its own, so the
        # earliest is how far this poll can vouch for, whatever is read after it.
        dates: list[str | None] = []
        issues_cut = False
        for source in POLL_SOURCES:
            read = source.read(self, scope)
            if read is None:
                continue  # this source does not apply to the scope
            items, cut, date = read
            dates.append(date)
            candidates += source.candidates(self, scope, items)
            issues_cut = issues_cut or (cut and source.counts_openings)
            # A list cut short is complete only up to its last item: later events wait for
            # the next poll, which starts from there.
            if cut and items:
                horizons.append(parse_time(source.changed(items[-1])))
        candidates = [c for c in candidates if not (c[0] <= position and c[1] in seen)]
        # Nothing after the earliest list's server time (less the clock skew) is vouched for:
        # a later event would move the cursor past what that list could not yet hold.
        server_times = [t for t in map(_http_date, dates) if t is not None]
        if server_times:
            horizons.append(min(server_times) - CLOCK_SKEW)
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
            final = _encode_cursor(position, position, delivered)
        return with_final_cursor(events, final)

    def _pulls_updated_since(self, owner: str, repo: str, since: datetime) -> list[int]:
        """The numbers of the repository's pull requests updated at or after ``since``, newest first.

        Read page by page, newest update first, until a page reaches one older than ``since``
        (at most ``MAX_PAGES`` pages: beyond that the oldest are not looked at).
        """
        numbers: list[int] = []
        for page in range(1, MAX_PAGES + 1):
            batch = (
                self._api(
                    f"repos/{owner}/{repo}/pulls?state=all&sort=updated&direction=desc"
                    f"&per_page={PAGE_SIZE}&page={page}"
                ).json()
                or []
            )
            numbers += [
                p["number"] for p in batch if parse_time(p["updated_at"]) >= since
            ]
            if len(batch) < PAGE_SIZE or any(
                parse_time(p["updated_at"]) < since for p in batch
            ):
                break
        return numbers

    def _active_discussions(
        self, owner: str, repo: str, since: datetime, *, recent: int = 0
    ) -> list[int]:
        """The numbers of the repository's discussions active at or after ``since``, and the ``recent`` most recently active.

        GitHub orders discussions by latest activity (a new comment or reply moves one to
        the front) but has no ``since`` filter, so pages are read newest first until one
        holds a discussion last updated before ``since``. The order is by activity while
        ``updatedAt`` also moves on other changes (a label or category set in bulk), so a
        page is read whole rather than cut at its first older entry. Some replies move
        neither ``updatedAt`` nor the order, so the first ``recent`` discussions in that
        order are included whatever their ``updatedAt``: reading them whole finds such a
        reply by its own creation time.
        """
        numbers: list[int] = []
        walked = 0
        after = None
        while True:
            data = self._graphql(
                DISCUSSION_ACTIVITY_QUERY, {"owner": owner, "name": repo, "after": after}
            )
            page = (data.get("repository") or {}).get("discussions") or {}
            nodes = page.get("nodes") or []
            active = [n for n in nodes if parse_time(n["updatedAt"]) >= since]
            numbers += [
                int(n["number"])
                for i, n in enumerate(nodes, start=walked)
                if i < recent or parse_time(n["updatedAt"]) >= since
            ]
            walked += len(nodes)
            page_info = page.get("pageInfo") or {}
            if (len(active) < len(nodes) and walked >= recent) or not page_info.get(
                "hasNextPage"
            ):
                return list(dict.fromkeys(numbers))
            after = page_info.get("endCursor")

    def _discussion_activity(
        self, owner: str, repo: str, number: int, since: datetime
    ) -> list[tuple[datetime, str, str, Message]]:
        """Poll candidates from discussion ``number``: its opening if it is new, posts written or edited since."""
        candidates = []
        for message in self._discussion_messages(owner, repo, number):
            changed = max(message.sent_at, message.edited_at or message.sent_at)
            if changed < since:
                continue
            is_new = message.sent_at >= since
            if message.id.startswith("discussion-") and not is_new:
                continue  # an older discussion edited: like an issue, its posts are the events
            candidates.append(
                (
                    changed,
                    f"{message.id}@{format_time(changed)}",
                    "message.created" if is_new else "message.updated",
                    message,
                )
            )
        return candidates

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
        """Comment on an issue, pull request or discussion; on a repository, open an issue (``title`` required), or a discussion with native ``category``.

        ``draft.reply_to`` answers a discussion comment, or a review comment on a pull
        request's diff (``discussion_rID``), joining its thread.

        ``draft.notify`` handles (``octocat``, ``org/team``) the text does not @mention yet
        are mentioned on a closing ``cc`` line, which GitHub notifies; the plan lists them
        under ``mentions_added``.
        """
        if not draft.notify:
            return self._send(ref, draft, dry_run=dry_run)
        added = missing_mentions(draft.text, draft.notify)
        result = self._send(ref, self.prepare(ref, draft), dry_run=dry_run)
        notified = {
            "notify": [handle(n) for n in draft.notify],
            "mentions_added": list(added),
        }
        return replace(result, plan={**result.plan, **notified})

    def prepare(self, ref: ConversationRef, draft: Draft) -> Draft:
        """The draft as it will post: ``notify`` handles checked, the missing mentions added. Pure."""
        if not draft.notify:
            return draft
        bad = [n for n in draft.notify if not GITHUB_HANDLE_RE.match(handle(n))]
        if bad:
            raise ChannelError(
                f"not a GitHub user or team handle: {', '.join(bad)}", kind="validation"
            )
        return replace(draft, text=with_mentions(draft.text, draft.notify))

    def _send(
        self, ref: ConversationRef, draft: Draft, *, dry_run: bool = False
    ) -> SendResult:
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
            if DISCUSSION_FIELD in draft.native:
                return self._open_discussion(ref, draft, dry_run=dry_run)
            fields = self._creation_fields(draft.native)
            plan = {
                "action": "open an issue",
                "request": f"POST repos/{owner}/{repo}/issues",
                "title": draft.title,
                "body": draft.text,
                **fields,
            }
            if dry_run:
                return self._planned(ref, "send", plan)
            data = (
                self._api(
                    f"repos/{owner}/{repo}/issues",
                    method="POST",
                    payload={"title": draft.title, "body": draft.text, **fields},
                ).json()
                or {}
            )
            plan.update(_read_back(fields, data))
            return self._done(
                f"github:{owner}/{repo}#{data.get('number')}",
                "send",
                f"issue-{data.get('number')}",
                data.get("html_url"),
                plan,
            )
        if draft.title:
            raise ChannelError("a comment has no title; leave it out", kind="validation")
        if draft.native:
            raise ChannelError(
                f"native fields {sorted(draft.native)} apply when opening an issue "
                f"(github:{owner}/{repo}); on an existing one use label",
                kind="validation",
            )
        target = f"{owner}/{repo}#{number}"
        reply_kind = self._message_kind(draft.reply_to)[0] if draft.reply_to else None
        if dry_run:
            request = (
                f"POST repos/{owner}/{repo}/pulls/{number}/comments/{{thread}}/replies, "
                f"in the review thread {draft.reply_to} belongs to (checked when sending)"
                if reply_kind == "reviewcomment"
                else f"POST repos/{owner}/{repo}/issues/{number}/comments, or GraphQL addDiscussionComment if #{number} is a discussion (checked when sending)"
            )
            return self._planned(
                ref,
                "send",
                {
                    "action": "comment",
                    "target": target,
                    "request": request,
                    "reply_to": draft.reply_to,
                    "body": draft.text,
                },
            )
        probe = self._api(f"repos/{owner}/{repo}/issues/{number}", allow=(404, 410))
        if probe.status < 400:
            if draft.reply_to:
                if reply_kind == "reviewcomment" and (probe.json() or {}).get(
                    "pull_request"
                ):
                    return self._reply_in_review_thread(ref, draft)
                raise NotSupported(
                    "reply",
                    NAME,
                    alternatives=(
                        "issue and pull request conversation comments are flat: quote the message you answer",
                        "on a pull request, reply to a review comment on its diff (discussion_rID) to join that thread",
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

    def _reply_in_review_thread(self, ref: ConversationRef, draft: Draft) -> SendResult:
        """Reply in the review thread of ``draft.reply_to``, a review comment on pull request ``ref``.

        GitHub threads review comments one level deep, so a reply to a reply goes to the
        comment that opened its thread, which the plan names as ``thread_root``.
        """
        owner, repo, number = self._parts(ref)
        _, ident = self._message_kind(draft.reply_to)
        parent = self._api(f"repos/{owner}/{repo}/pulls/comments/{ident}").json() or {}
        on = str(parent.get("pull_request_url", "")).rstrip("/").rsplit("/", 1)[-1]
        if on != str(number):
            raise ChannelError(
                f"{draft.reply_to} is not a review comment on pull request #{number}",
                kind="validation",
            )
        root = parent.get("in_reply_to_id") or ident
        path = f"repos/{owner}/{repo}/pulls/{number}/comments/{root}/replies"
        plan = {
            "action": "reply in a review thread",
            "target": f"{owner}/{repo}#{number}",
            "request": f"POST {path}",
            "reply_to": draft.reply_to,
            "thread_root": f"discussion_r{root}",
            "body": draft.text,
        }
        data = self._api(path, method="POST", payload={"body": draft.text}).json() or {}
        return self._done(
            ref.encoded,
            "send",
            f"discussion_r{data.get('id')}",
            data.get("html_url"),
            plan,
        )

    def _open_discussion(
        self, ref: ConversationRef, draft: Draft, *, dry_run: bool
    ) -> SendResult:
        """Open a discussion in the category ``draft.native["category"]`` names (GraphQL ``createDiscussion``)."""
        owner, repo, _ = self._parts(ref)
        category = draft.native[DISCUSSION_FIELD]
        others = sorted(set(draft.native) - {DISCUSSION_FIELD})
        if others:
            raise ChannelError(
                f"native {others} apply when opening an issue; a discussion takes only "
                f"{DISCUSSION_FIELD}",
                kind="validation",
            )
        if not isinstance(category, str) or not category.strip():
            raise ChannelError(
                f"native {DISCUSSION_FIELD} is a discussion category's name or slug, not {category!r}",
                kind="validation",
            )
        category = category.strip()
        plan: dict[str, Any] = {
            "action": "open a discussion",
            "request": "GraphQL createDiscussion",
            "title": draft.title,
            "body": draft.text,
            DISCUSSION_FIELD: category,
        }
        if dry_run:
            return self._planned(ref, "send", plan)
        repository = (
            self._graphql(
                DISCUSSION_CATEGORIES_QUERY, {"owner": owner, "name": repo}
            ).get("repository")
            or {}
        )
        if not repository.get("hasDiscussionsEnabled"):
            raise NotSupported(
                "open a discussion",
                NAME,
                alternatives=(
                    f"enable discussions in {owner}/{repo}'s settings",
                    f"open an issue: leave out native {DISCUSSION_FIELD}",
                ),
            )
        categories = (repository.get("discussionCategories") or {}).get("nodes") or []
        wanted = category.lower()
        found = next(
            (
                c
                for c in categories
                if wanted in (str(c.get("name")).lower(), str(c.get("slug")).lower())
            ),
            None,
        )
        if found is None:
            names = ", ".join(str(c.get("name")) for c in categories) or "none"
            raise ChannelError(
                f"{owner}/{repo} has no discussion category {category!r}; it has: {names}",
                kind="validation",
            )
        created = (
            self._graphql(
                CREATE_DISCUSSION,
                {
                    "repositoryId": repository["id"],
                    "categoryId": found["id"],
                    "title": draft.title,
                    "body": draft.text,
                },
            ).get("createDiscussion")
            or {}
        ).get("discussion") or {}
        if not created.get("number"):
            raise ChannelError(
                f"GitHub answered createDiscussion without the discussion; it may have been "
                f"opened: read github:{owner}/{repo} before trying again",
                kind="unavailable",
            )
        plan["landed"] = {
            DISCUSSION_FIELD: (created.get("category") or {}).get("name")
            or found.get("name")
        }
        return self._done(
            f"github:{owner}/{repo}#{created.get('number')}",
            "send",
            f"discussion-{created.get('number')}",
            created.get("url"),
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
                f"GitHub message ids look like issue-12, issuecomment-345, discussion_r678 (a review comment), pullrequestreview-9 (a review), discussion-6 or discussioncomment-789, not {message_id!r}",
                kind="validation",
            )
        return MESSAGE_ID_KINDS[match["prefix"]], match["id"]

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
        """Replace the body of an issue, a comment, a review comment, a discussion or a discussion comment."""
        owner, repo, number = self._parts(ref)
        kind, ident = self._message_kind(message_id)
        rest = {
            "issue": f"repos/{owner}/{repo}/issues/{ident}",
            "issuecomment": f"repos/{owner}/{repo}/issues/comments/{ident}",
            "reviewcomment": f"repos/{owner}/{repo}/pulls/comments/{ident}",
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
        """Add one of :data:`REACTIONS` to an issue, a comment, a review comment, a discussion or a discussion comment."""
        owner, repo, number = self._parts(ref)
        kind, ident = self._message_kind(message_id)
        rest = {
            "issue": f"repos/{owner}/{repo}/issues/{ident}/reactions",
            "issuecomment": f"repos/{owner}/{repo}/issues/comments/{ident}/reactions",
            "reviewcomment": f"repos/{owner}/{repo}/pulls/comments/{ident}/reactions",
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

    def _require_issue(
        self,
        owner: str,
        repo: str,
        number: int,
        operation: str,
        *,
        why: str = "labels apply to issues and pull requests, not discussions",
    ) -> dict:
        """The issue or pull request ``#number``; raise ``NotSupported`` (saying ``why``) when it is a discussion."""
        probe = self._api(f"repos/{owner}/{repo}/issues/{number}", allow=(404, 410))
        if probe.status < 400:
            return probe.json() or {}
        self._discussion_id(
            owner, repo, number
        )  # raises not_found when there is nothing at all
        raise NotSupported(operation, NAME, alternatives=(why,))

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
        rejecting it, so ``landed`` in the plan is mostly a confirmation: the requested
        names the response's own (now-current) label list actually carries, which is read
        back and reported the way an assignee is not (a login that is not a member of the
        repository is silently dropped from ``assignees``; a label name never is). The
        response lists every label now on the issue, including ones this call never
        mentioned, so ``landed`` is filtered to the requested names, not the full list.
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
        # The response is every label now on the issue, not only the ones this call added
        # (an issue can carry labels this request never mentioned): read back only the
        # requested names, so `landed` reports this call's effect, not the issue's state.
        now_on_issue = {
            item.get("name")
            for item in data
            if isinstance(item, dict) and item.get("name")
        }
        landed = [name for name in names if name in now_on_issue]
        plan["landed"] = landed
        missing = [name for name in names if name not in now_on_issue]
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

    @staticmethod
    def _creation_fields(native: Mapping[str, Any]) -> dict[str, Any]:
        """``Draft.native`` as the issue-creation payload: labels, assignees, milestone."""
        fields: dict[str, Any] = {}
        for name in ("labels", "assignees"):
            if name not in native:
                continue
            values = native[name]
            if isinstance(values, str) or not isinstance(values, Iterable):
                raise ChannelError(
                    f"native {name} is a list of names, not {values!r}", kind="validation"
                )
            names = [str(value).strip() for value in values]
            if not all(names):
                raise ChannelError(f"a name in native {name} is empty", kind="validation")
            fields[name] = list(dict.fromkeys(names))
        if "milestone" in native and native["milestone"] is not None:
            milestone = native["milestone"]
            if isinstance(milestone, bool) or not isinstance(milestone, int):
                raise ChannelError(
                    f"native milestone is a milestone number, not {milestone!r}",
                    kind="validation",
                )
            fields["milestone"] = milestone
        return fields

    def _set_state(
        self,
        ref: ConversationRef,
        operation: str,
        state: str,
        *,
        reason: str | None,
        dry_run: bool,
    ) -> SendResult:
        owner, repo, number = self._parts(ref)
        if number is None:
            raise ChannelError(
                f"{operation} applies to an issue, pull request or discussion: github:{owner}/{repo}#N",
                kind="validation",
            )
        known = set(CLOSE_REASONS) | set(DISCUSSION_CLOSE_REASONS)
        if reason is not None and reason not in known:
            raise ChannelError(
                f"close reason {reason!r} is not one of {CLOSE_REASONS} (issue) "
                f"or {DISCUSSION_CLOSE_REASONS} (discussion)",
                kind="validation",
            )
        path = f"repos/{owner}/{repo}/issues/{number}"
        payload: dict[str, Any] = {"state": state}
        if reason is not None:
            payload["state_reason"] = reason
        target = f"{owner}/{repo}#{number}"
        if dry_run:
            # A dry run reads nothing, so it cannot know which kind #N is: plan both.
            plan = {
                "action": f"{operation} the issue, pull request or discussion",
                "target": target,
                "request": f"PATCH {path} (issue or pull request), or GraphQL "
                f"{operation}Discussion (discussion)",
                **payload,
            }
            return self._planned(ref, operation, plan)
        probe = self._api(path, allow=(404, 410))
        if probe.status >= 400:
            return self._set_discussion_state(
                ref, operation, owner, repo, number, reason=reason
            )
        before = probe.json() or {}
        if reason is not None and reason not in CLOSE_REASONS:
            raise ChannelError(
                f"close reason {reason!r} is for discussions; an issue takes one of {CLOSE_REASONS}",
                kind="validation",
            )
        if before.get("pull_request") and reason is not None:
            raise ChannelError(
                "a pull request closes without a reason; leave it out", kind="validation"
            )
        plan = {
            "action": f"{operation} the issue or pull request",
            "target": target,
            "request": f"PATCH {path}",
            **payload,
            "was": before.get("state"),
        }
        data = self._api(path, method="PATCH", payload=payload).json() or {}
        # Read back what the issue now says, rather than assuming the request took.
        plan["now"] = data.get("state")
        if data.get("state_reason") is not None:
            plan["now_reason"] = data.get("state_reason")
        return self._done(
            ref.encoded, operation, f"issue-{number}", data.get("html_url"), plan
        )

    def _set_discussion_state(
        self,
        ref: ConversationRef,
        operation: str,
        owner: str,
        repo: str,
        number: int,
        *,
        reason: str | None,
    ) -> SendResult:
        """Close or reopen discussion ``#number`` through GraphQL, reading the state back."""
        try:
            data = self._graphql(
                DISCUSSION_STATE_QUERY, {"owner": owner, "name": repo, "number": number}
            )
        except ChannelError as error:
            if error.kind != "not_found":
                raise
            data = {}
        discussion = (data.get("repository") or {}).get("discussion") or {}
        if not discussion.get("id"):
            raise _not_found(owner, repo, number)
        variables: dict[str, Any] = {"discussionId": discussion["id"]}
        plan: dict[str, Any] = {
            "action": f"{operation} the discussion",
            "target": f"{owner}/{repo}#{number}",
            "request": f"GraphQL {operation}Discussion",
        }
        if operation == "close":
            if reason is not None:
                mapped = DISCUSSION_REASON_ALIASES.get(reason, reason)
                if mapped not in DISCUSSION_CLOSE_REASONS:
                    raise ChannelError(
                        f"close reason {reason!r} is for issues; a discussion takes one of "
                        f"{DISCUSSION_CLOSE_REASONS}",
                        kind="validation",
                    )
                variables["reason"] = mapped.upper()
                plan["reason"] = mapped
            query, field = CLOSE_DISCUSSION, "closeDiscussion"
        else:
            query, field = REOPEN_DISCUSSION, "reopenDiscussion"
        plan["was"] = _discussion_state(discussion)
        after = (self._graphql(query, variables).get(field) or {}).get("discussion") or {}
        # Read back what the discussion now says, rather than assuming the request took.
        plan["now"] = _discussion_state(after)
        if after.get("closed") and after.get("stateReason"):
            plan["now_reason"] = str(after["stateReason"]).lower()
        return self._done(
            ref.encoded,
            operation,
            f"discussion-{number}",
            after.get("url") or discussion.get("url"),
            plan,
        )

    def close(
        self, ref: ConversationRef, *, reason: str | None = None, dry_run: bool = False
    ) -> SendResult:
        """Close an issue or pull request (``PATCH .../issues/N``) or a discussion (GraphQL).

        ``reason`` is the kind's own: an issue's ``state_reason`` (``completed``,
        ``not_planned``, ``duplicate``) or a discussion's (``resolved``, ``outdated``,
        ``duplicate``; ``completed`` is accepted as ``resolved``); a pull request takes
        none. The plan reads the state back: ``was`` (before) and ``now`` /
        ``now_reason`` (what the response says), so closing an already-closed
        conversation is visible as such.
        """
        return self._set_state(ref, "close", "closed", reason=reason, dry_run=dry_run)

    def reopen(self, ref: ConversationRef, *, dry_run: bool = False) -> SendResult:
        """Reopen a closed issue, pull request or discussion; the plan reads the state back as for :meth:`close`."""
        return self._set_state(ref, "reopen", "open", reason=None, dry_run=dry_run)

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
