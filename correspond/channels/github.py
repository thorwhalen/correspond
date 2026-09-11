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
  three in one sequence; correspond asks which.

Message ids match the anchors in GitHub's own URLs: ``issue-N`` (the opening post of issue
or pull request N), ``issuecomment-ID``, ``discussion-N``, ``discussioncomment-ID``.

Messages read through the API are ``platform`` (GitHub authenticated the account), with the
author's ``author_association`` (``OWNER``, ``MEMBER``, ``CONTRIBUTOR``, ``NONE``, …) as
``authority``. A webhook delivery is graded by :meth:`GitHub.verify`: ``crypto`` when its
``X-Hub-Signature-256`` matches the configured secret, ``forged`` otherwise.

>>> GitHub().parse_ref("OctoCat/Hello-World#1").encoded
'github:octocat/hello-world#1'
>>> signature("a-secret", b"{}")
'sha256=...'
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import subprocess
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from correspond.channels._http import classify, retry_after_seconds
from correspond.errors import ChannelError, InvalidRef, MissingRequirement, NotSupported
from correspond.model import (
    Account,
    Authenticity,
    Capabilities,
    ChannelIdentity,
    ConversationRef,
    Draft,
    Event,
    Grade,
    HistoryDepth,
    Message,
    SendResult,
    Support,
    format_time,
    parse_time,
)
from correspond.ops import window, with_final_cursor
from correspond.registry import require

__all__ = ["GitHub", "REACTIONS", "signature"]

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


class GitHub:
    """GitHub through ``gh``: read, listen, send, edit, react, and verify webhook deliveries."""

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

    def _collect(self, path: str) -> tuple[list[Any], bool, str | None]:
        """Every item of a paginated list: ``(items, truncated, the server's Date on the last page)``."""
        separator = "&" if "?" in path else "?"
        items: list[Any] = []
        date = None
        for page in range(1, MAX_PAGES + 1):
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
        comments, _, _ = self._collect(path)
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

    # ----------------------------------------------------------------- listening

    def poll(
        self, ref: ConversationRef, *, cursor: str | None = None, limit: int | None = None
    ):
        """Issue and comment activity since ``cursor`` (an ISO time); a first poll looks back a day."""
        owner, repo, number = self._parts(ref)
        since = (
            parse_time(cursor) if cursor else datetime.now(timezone.utc) - LISTEN_LOOKBACK
        )
        stamp = format_time(since)
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
            comments, comments_cut, date = self._collect(
                f"repos/{owner}/{repo}/issues/{number}/comments?since={stamp}"
            )
            issues, issues_cut = [], False

        timed: list[tuple[datetime, Event]] = []
        for comment in comments:
            issue_number = (
                str(comment.get("issue_url", "")).rstrip("/").rsplit("/", 1)[-1]
            )
            conversation = ConversationRef(
                channel=NAME, id=f"{owner}/{repo}#{issue_number}", parent=repository
            )
            changed = comment.get("updated_at") or comment["created_at"]
            created = parse_time(comment["created_at"]) >= since
            timed.append(
                (
                    parse_time(changed),
                    Event(
                        kind="message.created" if created else "message.updated",
                        channel=NAME,
                        delivery_id=f"github:{owner}/{repo}:issuecomment-{comment['id']}@{changed}",
                        cursor=changed,
                        message=self._comment(conversation, comment),
                    ),
                )
            )
        for issue in issues:
            if parse_time(issue["created_at"]) < since:
                continue  # an older issue that changed: its comments arrive as their own events
            timed.append(
                (
                    parse_time(issue["created_at"]),
                    Event(
                        kind="message.created",
                        channel=NAME,
                        delivery_id=f"github:{owner}/{repo}:issue-{issue['number']}@{issue['created_at']}",
                        cursor=issue["created_at"],
                        message=self._opening(
                            self._issue_conversation(owner, repo, issue), issue
                        ),
                    ),
                )
            )
        timed.sort(key=lambda pair: pair[0])
        # A list cut short (too many changes since the cursor) is complete only up to its last
        # item. An event from the other list after that point would move the cursor past
        # changes not fetched yet, so it waits for the next poll.
        horizons = [
            parse_time(items[-1].get("updated_at") or items[-1]["created_at"])
            for items, cut in ((comments, comments_cut), (issues, issues_cut))
            if cut and items
        ]
        if horizons:
            timed = [pair for pair in timed if pair[0] <= min(horizons)]
        truncated = comments_cut or issues_cut or (bool(limit) and len(timed) > limit)
        if limit:
            timed = timed[:limit]
        final = None
        server_now = _http_date(date)
        if not truncated and server_now is not None:
            candidates = [since, server_now - CLOCK_SKEW] + (
                [timed[-1][0]] if timed else []
            )
            final = format_time(max(candidates))
        return with_final_cursor([event for _, event in timed], final)

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
