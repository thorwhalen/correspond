"""GitHub over a scripted gh: reads, discussion fallback, writes on stdin, error classification, polling, webhook signatures."""

import json
import re
import subprocess

import pytest

import correspond
from correspond import registry
from correspond.__main__ import main as cli
from correspond.channels.github import (
    APPS_AND_WEBHOOKS,
    BASE_PERMISSION_DEFAULT,
    SECURITY_MANAGERS,
    UNLISTED_COLLABORATORS,
    WATCHERS,
    GitHub,
    _parse_reply,
    signature,
)
from correspond.errors import ChannelError, MissingRequirement, NotSupported

REPO = "octocat/hello-world"


def _issue(
    number=1,
    *,
    user="octocat",
    association="OWNER",
    pr=False,
    created="2026-09-11T08:00:00Z",
    labels=("bug",),
):
    data = {
        "number": number,
        "title": "The export drops the last row",
        "body": "Steps: export, count rows.",
        "user": {"login": user, "id": 1, "type": "User"},
        "author_association": association,
        "labels": [{"name": label} for label in labels],
        "state": "open",
        "locked": False,
        "created_at": created,
        "updated_at": created,
        "html_url": f"https://github.com/{REPO}/issues/{number}",
    }
    if pr:
        data["pull_request"] = {"url": "https://example.org/pull"}
    return data


def _comment(
    comment_id,
    *,
    user="octocat",
    issue=1,
    created="2026-09-11T09:00:00Z",
    updated=None,
    association="NONE",
):
    return {
        "id": comment_id,
        "user": {
            "login": user,
            "id": 2,
            "type": "Bot" if user.endswith("[bot]") else "User",
        },
        "body": f"comment {comment_id}",
        "created_at": created,
        "updated_at": updated or created,
        "author_association": association,
        "html_url": f"https://github.com/{REPO}/issues/{issue}#issuecomment-{comment_id}",
        "issue_url": f"https://api.github.com/repos/{REPO}/issues/{issue}",
    }


def _post(database_id, *, created="2026-09-11T10:00:00Z", edited=None, replies=()):
    post = {
        "id": f"DC_node{database_id}",
        "databaseId": database_id,
        "body": f"post {database_id}",
        "url": f"https://github.com/{REPO}/discussions/5#discussioncomment-{database_id}",
        "createdAt": created,
        "lastEditedAt": edited,
        "authorAssociation": "MEMBER",
        "isMinimized": False,
        "author": {"__typename": "User", "login": "octocat", "databaseId": 1},
    }
    if replies is not None:
        post["replies"] = {"totalCount": len(replies), "nodes": list(replies)}
    return post


def _discussion(comments=()):
    return {
        "data": {
            "repository": {
                "discussion": {
                    "id": "D_node5",
                    "number": 5,
                    "title": "Ideas for the export",
                    "body": "What should it support?",
                    "url": f"https://github.com/{REPO}/discussions/5",
                    "createdAt": "2026-09-11T08:30:00Z",
                    "lastEditedAt": None,
                    "authorAssociation": "OWNER",
                    "locked": False,
                    "category": {"name": "Ideas"},
                    "author": {"__typename": "User", "login": "octocat", "databaseId": 1},
                    "comments": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": list(comments),
                    },
                }
            }
        }
    }


class ScriptedGh:
    """Answers `gh api -i` like gh does: status line, headers, blank line, body; records every call and its stdin."""

    def __init__(self, *routes):
        self.routes = list(routes)
        self.calls = []

    def __call__(self, argv, *, input=None, capture_output=True, text=True, timeout=None):
        if argv[0] != "gh":
            raise AssertionError(f"unexpected command {argv}")
        method = argv[argv.index("--method") + 1]
        path = argv[argv.index("--method") + 2]
        payload = json.loads(input) if input else None
        self.calls.append(
            {"method": method, "path": path, "payload": payload, "argv": argv}
        )
        for route_method, pattern, reply in self.routes:
            if route_method == method and re.fullmatch(pattern, path):
                status, body, *headers = reply(payload) if callable(reply) else reply
                return _completed(argv, status, body, headers[0] if headers else {})
        raise AssertionError(f"unexpected gh call: {method} {path}")

    def paths(self, method=None):
        return [c["path"] for c in self.calls if method is None or c["method"] == method]


def _completed(argv, status, body, headers):
    head = "\n".join(
        [
            f"HTTP/2.0 {status} X",
            "Content-Type: application/json",
            *(f"{k}: {v}" for k, v in headers.items()),
        ]
    )
    return subprocess.CompletedProcess(
        argv,
        0 if status < 400 else 1,
        stdout=f"{head}\n\n{json.dumps(body)}",
        stderr="" if status < 400 else f"gh: error (HTTP {status})",
    )


WHOAMI = ("GET", "user", (200, {"login": "example-bot"}))


def _read(gh, ref, **kwargs):
    return correspond.read(ref, registry={"github": GitHub(run=gh)}, **kwargs)


def test_read_an_issue_with_its_comments():
    gh = ScriptedGh(
        WHOAMI,
        ("GET", rf"repos/{REPO}/issues/1", (200, _issue())),
        (
            "GET",
            rf"repos/{REPO}/issues/1/comments\?per_page=100&page=1",
            (
                200,
                [
                    _comment(11, user="example-bot"),
                    _comment(
                        12, created="2026-09-11T09:10:00Z", updated="2026-09-11T09:20:00Z"
                    ),
                ],
            ),
        ),
    )
    messages = _read(gh, "github:OctoCat/Hello-World#1")
    assert [m.id for m in messages] == ["issue-1", "issuecomment-11", "issuecomment-12"]
    opening, mine, edited = messages
    assert (
        opening.conversation.kind == "issue"
        and opening.conversation.parent.encoded == f"github:{REPO}"
    )
    assert opening.native["title"] == "The export drops the last row" and opening.native[
        "labels"
    ] == ["bug"]
    assert (
        opening.author.authority == "OWNER"
        and opening.authenticity.grade.value == "platform"
    )
    assert mine.author.is_self and not opening.author.is_self
    assert edited.edited_at is not None and mine.edited_at is None
    assert all(c["payload"] is None for c in gh.calls)


def test_a_number_that_is_no_issue_is_read_as_a_discussion_with_replies():
    reply = _post(502, created="2026-09-11T10:05:00Z", replies=None)
    gh = ScriptedGh(
        WHOAMI,
        ("GET", rf"repos/{REPO}/issues/5", (404, {"message": "Not Found"})),
        (
            "POST",
            "graphql",
            (
                200,
                _discussion([_post(501, edited="2026-09-11T11:00:00Z", replies=[reply])]),
            ),
        ),
    )
    messages = _read(gh, f"github:{REPO}#5")
    assert [m.id for m in messages] == [
        "discussion-5",
        "discussioncomment-501",
        "discussioncomment-502",
    ]
    assert (
        messages[0].conversation.kind == "discussion"
        and messages[0].native["category"] == "Ideas"
    )
    assert messages[2].reply_to == messages[2].thread_root == "discussioncomment-501"
    assert messages[1].edited_at is not None and messages[1].author.authority == "MEMBER"


def test_neither_an_issue_nor_a_discussion_is_not_found():
    gh = ScriptedGh(
        WHOAMI,
        ("GET", rf"repos/{REPO}/issues/9", (404, {"message": "Not Found"})),
        (
            "POST",
            "graphql",
            (
                200,
                {
                    "data": {"repository": {"discussion": None}},
                    "errors": [{"type": "NOT_FOUND", "message": "Could not resolve"}],
                },
            ),
        ),
    )
    with pytest.raises(ChannelError) as caught:
        _read(gh, f"github:{REPO}#9")
    assert caught.value.kind == "not_found" and "#9" in str(caught.value)


def test_reading_a_repository_lists_its_open_issues_oldest_first():
    gh = ScriptedGh(
        WHOAMI,
        (
            "GET",
            rf"repos/{REPO}/issues\?state=open&sort=created&direction=desc&per_page=2",
            (
                200,
                [
                    _issue(3, created="2026-09-11T09:00:00Z", pr=True),
                    _issue(2, created="2026-09-11T08:00:00Z"),
                ],
            ),
        ),
    )
    messages = _read(gh, f"github:{REPO}", limit=2)
    assert [m.conversation.encoded for m in messages] == [
        f"github:{REPO}#2",
        f"github:{REPO}#3",
    ]
    assert messages[1].conversation.kind == "pull_request"


def test_dry_runs_run_no_gh_command_at_all():
    gh = ScriptedGh()
    registry = {"github": GitHub(run=gh)}
    results = [
        correspond.send(f"github:{REPO}#1", "a comment", dry_run=True, registry=registry),
        correspond.send(
            f"github:{REPO}", "body", title="A new issue", dry_run=True, registry=registry
        ),
        correspond.edit(
            f"github:{REPO}#1",
            "issuecomment-11",
            "new text",
            dry_run=True,
            registry=registry,
        ),
        correspond.react(
            f"github:{REPO}#1", "issue-1", "eyes", dry_run=True, registry=registry
        ),
    ]
    assert all(r.ok and r.dry_run for r in results) and gh.calls == []
    assert (
        results[1].plan["action"] == "open an issue"
        and "discussion" in results[0].plan["request"]
    )


def test_a_comment_travels_on_stdin_never_on_the_command_line():
    text = "a comment with 'quotes' and --flags"
    gh = ScriptedGh(
        WHOAMI,
        ("GET", rf"repos/{REPO}/issues/1", (200, _issue())),
        (
            "POST",
            rf"repos/{REPO}/issues/1/comments",
            (201, {"id": 99, "html_url": "https://example.org/c99"}),
        ),
    )
    result = correspond.send(
        f"github:{REPO}#1", text, registry={"github": GitHub(run=gh)}
    )
    assert (
        result.ok
        and result.message_id == "issuecomment-99"
        and result.account.id == "example-bot"
    )
    post = next(c for c in gh.calls if c["method"] == "POST")
    assert post["payload"] == {"body": text} and all(
        text not in arg for arg in post["argv"]
    )


def test_opening_an_issue_needs_a_title():
    gh = ScriptedGh(
        WHOAMI,
        (
            "POST",
            rf"repos/{REPO}/issues",
            (201, {"number": 7, "html_url": "https://example.org/7"}),
        ),
    )
    registry = {"github": GitHub(run=gh)}
    untitled = correspond.send(f"github:{REPO}", "body", registry=registry)
    assert (untitled.ok, untitled.error_kind) == (False, "validation") and gh.calls == []
    opened = correspond.send(
        f"github:{REPO}", "body", title="Export drops a row", registry=registry
    )
    assert opened.conversation == f"github:{REPO}#7" and opened.message_id == "issue-7"


def test_commenting_on_a_discussion_and_replying_to_a_comment():
    def graphql(payload):
        query = payload["query"]
        if "addDiscussionComment" in query:
            return 200, {
                "data": {
                    "addDiscussionComment": {
                        "comment": {"databaseId": 777, "url": "https://example.org/777"}
                    }
                }
            }
        if "comments(" in query:
            return 200, _discussion([_post(501, replies=[])])
        return 200, {"data": {"repository": {"discussion": {"id": "D_node5"}}}}

    gh = ScriptedGh(
        WHOAMI,
        ("GET", rf"repos/{REPO}/issues/5", (404, {"message": "Not Found"})),
        ("POST", "graphql", graphql),
    )
    result = correspond.send(
        f"github:{REPO}#5",
        "a reply",
        reply_to="discussioncomment-501",
        registry={"github": GitHub(run=gh)},
    )
    assert result.ok and result.message_id == "discussioncomment-777"
    mutation = next(
        c["payload"]
        for c in gh.calls
        if c["payload"] and "addDiscussionComment" in c["payload"]["query"]
    )
    assert mutation["variables"] == {
        "discussionId": "D_node5",
        "body": "a reply",
        "replyToId": "DC_node501",
    }


def test_replying_to_an_issue_comment_is_refused_by_name():
    gh = ScriptedGh(WHOAMI, ("GET", rf"repos/{REPO}/issues/1", (200, _issue())))
    with pytest.raises(NotSupported, match="reply"):
        correspond.send(
            f"github:{REPO}#1",
            "hi",
            reply_to="issuecomment-11",
            registry={"github": GitHub(run=gh)},
        )


def test_edit_and_react_use_the_endpoint_for_each_kind_of_message():
    gh = ScriptedGh(
        WHOAMI,
        (
            "PATCH",
            rf"repos/{REPO}/issues/comments/11",
            (200, {"html_url": "https://example.org/c11"}),
        ),
        (
            "PATCH",
            rf"repos/{REPO}/issues/1",
            (200, {"html_url": "https://example.org/i1"}),
        ),
        ("POST", rf"repos/{REPO}/issues/comments/11/reactions", (201, {"content": "+1"})),
        ("POST", rf"repos/{REPO}/issues/1/reactions", (201, {"content": "eyes"})),
    )
    registry = {"github": GitHub(run=gh)}
    assert correspond.edit(
        f"github:{REPO}#1", "issuecomment-11", "fixed", registry=registry
    ).url.endswith("c11")
    assert correspond.edit(
        f"github:{REPO}#1", "issue-1", "fixed body", registry=registry
    ).ok
    assert correspond.react(
        f"github:{REPO}#1", "issuecomment-11", "+1", registry=registry
    ).ok
    assert correspond.react(f"github:{REPO}#1", "issue-1", "eyes", registry=registry).ok
    assert [c["payload"] for c in gh.calls if c["method"] != "GET"] == [
        {"body": "fixed"},
        {"body": "fixed body"},
        {"content": "+1"},
        {"content": "eyes"},
    ]
    refused = correspond.react(f"github:{REPO}#1", "issue-1", "party", registry=registry)
    bad_id = correspond.edit(f"github:{REPO}#1", "comment-11", "x", registry=registry)
    assert (refused.error_kind, bad_id.error_kind) == ("validation", "validation")


def test_editing_and_reacting_in_a_discussion_go_through_graphql():
    def graphql(payload):
        query = payload["query"]
        if "updateDiscussionComment" in query:
            return 200, {
                "data": {
                    "updateDiscussionComment": {
                        "comment": {"url": "https://example.org/501"}
                    }
                }
            }
        if "addReaction" in query:
            return 200, {"data": {"addReaction": {"reaction": {"content": "HEART"}}}}
        return 200, _discussion([_post(501, replies=[])])

    gh = ScriptedGh(WHOAMI, ("POST", "graphql", graphql))
    registry = {"github": GitHub(run=gh)}
    assert (
        correspond.edit(
            f"github:{REPO}#5", "discussioncomment-501", "fixed", registry=registry
        ).url
        == "https://example.org/501"
    )
    assert correspond.react(
        f"github:{REPO}#5", "discussioncomment-501", "heart", registry=registry
    ).ok
    reaction = next(
        c["payload"]
        for c in gh.calls
        if c["payload"] and "addReaction" in c["payload"]["query"]
    )
    assert reaction["variables"] == {"id": "DC_node501", "content": "HEART"}
    no_number = correspond.edit(
        f"github:{REPO}", "discussioncomment-501", "x", registry=registry
    )
    assert no_number.error_kind == "validation"


@pytest.mark.parametrize(
    "status, body, headers, kind, retry_after",
    [
        (
            403,
            {"message": "You have exceeded a secondary rate limit"},
            {},
            "rate_limited",
            60.0,
        ),
        (429, {"message": "Too many"}, {"Retry-After": "7"}, "rate_limited", 7.0),
        (401, {"message": "Bad credentials"}, {}, "auth", None),
        (403, {"message": "Resource not accessible"}, {}, "permission", None),
        (404, {"message": "Not Found"}, {}, "not_found", None),
        (422, {"message": "Validation Failed"}, {}, "validation", None),
        (502, {"message": "Bad gateway"}, {}, "unavailable", None),
    ],
)
def test_platform_errors_become_classified_results(
    status, body, headers, kind, retry_after
):
    gh = ScriptedGh(
        WHOAMI,
        ("GET", rf"repos/{REPO}/issues/1", (200, _issue())),
        ("POST", rf"repos/{REPO}/issues/1/comments", (status, body, headers)),
    )
    result = correspond.send(
        f"github:{REPO}#1", "hello", registry={"github": GitHub(run=gh)}
    )
    assert (result.ok, result.error_kind) == (False, kind)
    assert result.retryable == (kind in ("rate_limited", "unavailable"))
    assert result.retry_after == retry_after


def test_an_exhausted_primary_rate_limit_waits_until_the_reset(monkeypatch):
    monkeypatch.setattr("time.time", lambda: 1_000.0)
    gh = ScriptedGh(
        WHOAMI,
        (
            "GET",
            rf"repos/{REPO}/issues/1",
            (
                403,
                {"message": "API rate limit exceeded"},
                {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1090"},
            ),
        ),
    )
    with pytest.raises(ChannelError) as caught:
        GitHub(run=gh).read(GitHub().parse_ref(f"{REPO}#1"))
    assert caught.value.kind == "rate_limited" and caught.value.retry_after == 90.0


def test_a_missing_or_logged_out_gh_says_what_to_do():
    def missing(argv, **kwargs):
        raise FileNotFoundError("gh")

    def logged_out(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv,
            4,
            stdout="",
            stderr="To get started with GitHub CLI, please run:  gh auth login",
        )

    result = correspond.send(
        f"github:{REPO}#1", "hi", registry={"github": GitHub(run=missing)}
    )
    assert result.error_kind == "unavailable" and "cli.github.com" in result.error
    with pytest.raises(ChannelError) as caught:
        GitHub(run=logged_out).read(GitHub().parse_ref(f"{REPO}#1"))
    assert caught.value.kind == "auth" and "gh auth login" in str(caught.value)


def test_polling_a_repository_orders_new_issues_and_comment_activity_and_moves_the_cursor():
    date = {"Date": "Fri, 11 Sep 2026 10:00:00 GMT"}
    gh = ScriptedGh(
        WHOAMI,
        (
            "GET",
            rf"repos/{REPO}/issues/comments\?sort=updated&direction=asc&since=2026-09-11T08:00:00Z&per_page=100&page=1",
            (
                200,
                [
                    _comment(21, issue=3, created="2026-09-11T09:00:00Z"),
                    _comment(
                        22,
                        issue=2,
                        created="2026-09-11T07:00:00Z",
                        updated="2026-09-11T09:30:00Z",
                    ),
                ],
                date,
            ),
        ),
        (
            "GET",
            rf"repos/{REPO}/issues\?state=all&sort=updated&direction=asc&since=2026-09-11T08:00:00Z&per_page=100&page=1",
            (
                200,
                [
                    _issue(4, created="2026-09-11T08:30:00Z"),
                    _issue(2, created="2026-09-11T07:00:00Z"),
                ],
            ),
        ),
    )
    cursors = {f"github:{REPO}": "2026-09-11T08:00:00Z"}
    events = list(
        correspond.listen(
            f"github:{REPO}", cursors=cursors, registry={"github": GitHub(run=gh)}
        )
    )
    assert [(e.kind, e.message.id) for e in events] == [
        ("message.created", "issue-4"),
        ("message.created", "issuecomment-21"),
        ("message.updated", "issuecomment-22"),
    ]
    assert len({e.delivery_id for e in events}) == 3
    assert events[1].message.conversation.encoded == f"github:{REPO}#3"
    assert json.loads(cursors[f"github:{REPO}"]) == {
        "t": "2026-09-11T09:59:55Z",
        "l": "2026-09-11T09:59:55Z",
        "seen": [],
    }


def test_listening_to_a_discussion_is_refused_by_name():
    gh = ScriptedGh(
        WHOAMI,
        ("GET", rf"repos/{REPO}/issues/5", (404, {"message": "Not Found"})),
        (
            "POST",
            "graphql",
            (200, {"data": {"repository": {"discussion": {"id": "D_node5"}}}}),
        ),
    )
    with pytest.raises(NotSupported, match="listen to a discussion"):
        correspond.listen(
            f"github:{REPO}#5", cursors={}, registry={"github": GitHub(run=gh)}
        )


def test_webhook_deliveries_are_crypto_when_the_signature_matches(monkeypatch):
    body = b'{"action": "opened"}'
    registry = {"github": GitHub(run=ScriptedGh())}
    with pytest.raises(MissingRequirement, match="GITHUB_WEBHOOK_SECRET"):
        correspond.verify(
            "github", {"X-Hub-Signature-256": "sha256=00"}, body, registry=registry
        )
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "an-example-webhook-secret")
    good = {
        "X-Hub-Signature-256": signature("an-example-webhook-secret", body),
        "X-GitHub-Delivery": "d-1",
    }
    assert (
        correspond.verify("github", good, body, registry=registry).grade.value == "crypto"
    )
    tampered = correspond.verify("github", good, body + b" ", registry=registry)
    assert (
        tampered.grade.value == "forged" and tampered.evidence["signature"] == "mismatch"
    )
    missing = correspond.verify("github", {}, body, registry=registry)
    assert missing.grade.value == "forged" and missing.evidence["signature"] == "missing"
    assert (
        correspond.verify(
            "github", {"x-hub-signature-256": "sha256=é"}, body, registry=registry
        ).grade.value
        == "forged"
    )


def test_replies_are_parsed_whatever_the_line_endings():
    reply = _parse_reply('HTTP/2.0 200 OK\r\nDate: today\r\n\r\n{"body": "a\\n\\nb"}')
    assert (
        reply.status == 200
        and reply.headers["date"] == "today"
        and reply.json() == {"body": "a\n\nb"}
    )
    assert _parse_reply("not http") is None


def test_references_are_validated_and_normalised():
    github = GitHub()
    assert github.parse_ref("OctoCat/Hello-World").kind == "repository"
    for bad in ("octocat", "octocat/hello-world#0", "-bad/repo", "octocat/hello world"):
        with pytest.raises(correspond.InvalidRef):
            github.parse_ref(bad)


def test_the_native_fields_messages_carry_are_the_ones_capabilities_declare():
    declared = set(GitHub().capabilities.native_fields)
    gh = ScriptedGh(
        WHOAMI,
        ("GET", rf"repos/{REPO}/issues/1", (200, _issue())),
        (
            "GET",
            rf"repos/{REPO}/issues/1/comments\?per_page=100&page=1",
            (200, [_comment(11)]),
        ),
        ("GET", rf"repos/{REPO}/issues/5", (404, {"message": "Not Found"})),
        (
            "POST",
            "graphql",
            (200, _discussion([_post(501, replies=[_post(502, replies=None)])])),
        ),
    )
    for ref in (f"github:{REPO}#1", f"github:{REPO}#5"):
        for message in _read(gh, ref):
            assert set(message.native) <= declared, (
                message.id,
                set(message.native) - declared,
            )


def test_a_list_cut_short_holds_back_later_events_from_the_other_list(monkeypatch):
    from correspond.channels import github as github_module

    monkeypatch.setattr(github_module, "PAGE_SIZE", 2)
    monkeypatch.setattr(github_module, "MAX_PAGES", 1)
    since = "2026-09-11T08:00:00Z"
    gh = ScriptedGh(
        WHOAMI,
        (
            "GET",
            rf"repos/{REPO}/issues/comments\?sort=updated&direction=asc&since={since}&per_page=2&page=1",
            (
                200,
                [
                    _comment(31, created="2026-09-11T08:10:00Z"),
                    _comment(32, created="2026-09-11T08:20:00Z"),
                ],
                {"Date": "Fri, 11 Sep 2026 10:00:00 GMT"},
            ),
        ),
        (
            "GET",
            rf"repos/{REPO}/issues\?state=all&sort=updated&direction=asc&since={since}&per_page=2&page=1",
            (200, [_issue(7, created="2026-09-11T09:30:00Z")]),
        ),
    )
    cursors = {f"github:{REPO}": since}
    events = list(
        correspond.listen(
            f"github:{REPO}", cursors=cursors, registry={"github": GitHub(run=gh)}
        )
    )
    assert [e.message.id for e in events] == ["issuecomment-31", "issuecomment-32"], (
        "issue 7 waits: comments after 08:20 were not fetched"
    )
    assert json.loads(cursors[f"github:{REPO}"]) == {
        "t": "2026-09-11T08:20:00Z",
        "l": "2026-09-11T08:20:00Z",
        "seen": ["issuecomment-32@2026-09-11T08:20:00Z"],
    }
    later = "2026-09-11T08:40:00Z"
    gh.routes += [
        (
            "GET",
            rf"repos/{REPO}/issues/comments\?sort=updated&direction=asc&since=2026-09-11T08:20:00Z&per_page=2&page=1",
            (
                200,
                [
                    _comment(32, created="2026-09-11T08:20:00Z"),
                    _comment(33, created=later),
                ],
            ),
        ),
        (
            "GET",
            rf"repos/{REPO}/issues\?state=all&sort=updated&direction=asc&since=2026-09-11T08:20:00Z&per_page=2&page=1",
            (200, [_issue(7, created="2026-09-11T09:30:00Z")]),
        ),
        (
            "GET",
            rf"repos/{REPO}/issues/comments\?sort=updated&direction=asc&since={later}&per_page=2&page=1",
            (200, [_comment(33, created=later)]),
        ),
        (
            "GET",
            rf"repos/{REPO}/issues\?state=all&sort=updated&direction=asc&since={later}&per_page=2&page=1",
            (200, [_issue(7, created="2026-09-11T09:30:00Z")]),
        ),
    ]
    registry = {"github": GitHub(run=gh)}
    second = [
        e.message.id
        for e in correspond.listen(f"github:{REPO}", cursors=cursors, registry=registry)
    ]
    third = [
        e.message.id
        for e in correspond.listen(f"github:{REPO}", cursors=cursors, registry=registry)
    ]
    assert (second, third) == (["issuecomment-33"], ["issue-7"])


def _paged_routes(since, *, comments=(), issues=(), page_size=2):
    return [
        (
            "GET",
            rf"repos/{REPO}/issues/comments\?sort=updated&direction=asc&since={since}&per_page={page_size}&page=1",
            (200, list(comments)),
        ),
        (
            "GET",
            rf"repos/{REPO}/issues\?state=all&sort=updated&direction=asc&since={since}&per_page={page_size}&page=1",
            (200, list(issues)),
        ),
    ]


def _issue_at(number, created, updated):
    return {**_issue(number, created=created), "updated_at": updated}


def _polls(gh, times, **kwargs):
    registry, cursors = (
        {"github": GitHub(run=gh)},
        {f"github:{REPO}": "2026-09-11T08:00:00Z"},
    )
    return [
        [
            e.message.id
            for e in correspond.listen(
                f"github:{REPO}", cursors=cursors, registry=registry, **kwargs
            )
        ]
        for _ in range(times)
    ], cursors


@pytest.fixture
def small_pages(monkeypatch):
    from correspond.channels import github as github_module

    monkeypatch.setattr(github_module, "PAGE_SIZE", 2)
    monkeypatch.setattr(github_module, "MAX_PAGES", 1)


def test_a_cut_issues_list_with_nothing_new_still_moves_the_cursor(small_pages):
    old = "2026-09-01T00:00:00Z"
    comment = _comment(900, created="2026-09-11T09:00:00Z")
    gh = ScriptedGh(
        WHOAMI,
        *_paged_routes(
            "2026-09-11T08:00:00Z",
            comments=[comment],
            issues=[
                _issue_at(101, old, "2026-09-11T08:01:00Z"),
                _issue_at(102, old, "2026-09-11T08:02:00Z"),
            ],
        ),
        *_paged_routes(
            "2026-09-11T08:02:00Z",
            comments=[comment],
            issues=[
                _issue_at(102, old, "2026-09-11T08:02:00Z"),
                _issue_at(103, old, "2026-09-11T08:03:00Z"),
            ],
        ),
        *_paged_routes(
            "2026-09-11T08:03:00Z",
            comments=[comment],
            issues=[_issue_at(103, old, "2026-09-11T08:03:00Z")],
        ),
    )
    delivered, cursors = _polls(gh, 3)
    assert delivered == [[], [], ["issuecomment-900"]]
    assert json.loads(cursors[f"github:{REPO}"])["t"] == "2026-09-11T09:00:00Z"


def test_a_limit_takes_each_event_once_even_within_one_second():
    at = "2026-09-11T08:10:00Z"
    everything = [
        _comment(1, created=at),
        _comment(2, created=at),
        _comment(3, created="2026-09-11T08:20:00Z"),
    ]
    gh = ScriptedGh(
        WHOAMI,
        *_paged_routes("2026-09-11T08:00:00Z", comments=everything, page_size=100),
        *_paged_routes(at, comments=everything, page_size=100),
        *_paged_routes("2026-09-11T08:20:00Z", comments=everything[2:], page_size=100),
    )
    delivered, _ = _polls(gh, 4, limit=1)
    assert delivered == [["issuecomment-1"], ["issuecomment-2"], ["issuecomment-3"], []]


def test_an_issue_created_early_in_a_cut_issues_list_is_not_lost(small_pages):
    seven = _issue_at(7, "2026-09-11T08:30:00Z", "2026-09-11T08:40:00Z")
    eight = _issue_at(8, "2026-09-11T08:50:00Z", "2026-09-11T09:00:00Z")
    nine = _issue_at(9, "2026-09-11T08:10:00Z", "2026-09-11T09:30:00Z")
    gh = ScriptedGh(
        WHOAMI,
        *_paged_routes("2026-09-11T08:00:00Z", issues=[seven, eight]),
        *_paged_routes("2026-09-11T09:00:00Z", issues=[eight, nine]),
        *_paged_routes("2026-09-11T09:30:00Z", issues=[nine]),
    )
    delivered, _ = _polls(gh, 4)
    assert delivered[0] == ["issue-7", "issue-8"]
    assert "issue-9" in delivered[1], (
        "created before the cut, updated after it: still new"
    )
    assert delivered[3] == [], "and delivered for the last time once the list is whole"


def test_one_issue_is_listened_to_across_every_page(small_pages):
    gh = ScriptedGh(
        WHOAMI,
        ("GET", rf"repos/{REPO}/issues/1", (200, _issue())),
        (
            "GET",
            rf"repos/{REPO}/issues/1/comments\?since=2026-09-11T08:00:00Z&per_page=2&page=1",
            (
                200,
                [
                    _comment(
                        1, created="2026-09-11T07:00:00Z", updated="2026-09-11T09:00:00Z"
                    ),
                    _comment(2, created="2026-09-11T08:06:00Z"),
                ],
            ),
        ),
        (
            "GET",
            rf"repos/{REPO}/issues/1/comments\?since=2026-09-11T08:00:00Z&per_page=2&page=2",
            (200, [_comment(3, created="2026-09-11T08:30:00Z")]),
        ),
    )
    cursors = {f"github:{REPO}#1": "2026-09-11T08:00:00Z"}
    events = list(
        correspond.listen(
            f"github:{REPO}#1", cursors=cursors, registry={"github": GitHub(run=gh)}
        )
    )
    assert [e.message.id for e in events] == [
        "issuecomment-2",
        "issuecomment-3",
        "issuecomment-1",
    ]
    assert [e.kind for e in events][-1] == "message.updated"


def test_a_malformed_cursor_is_a_validation_error():
    gh = ScriptedGh(WHOAMI)
    for bad in ("yesterday", '{"t": "not a time"}', "{"):
        with pytest.raises(ChannelError) as caught:
            list(
                correspond.listen(
                    f"github:{REPO}",
                    cursors={f"github:{REPO}": bad},
                    registry={"github": GitHub(run=gh)},
                )
            )
        assert caught.value.kind == "validation"


# --------------------------------------------------------------------------- audience

APP = "example/app"
ISSUE = f"github:{APP}#12"
PUBLIC_WORDS = "world-readable; emailed to watchers and participants; archived by others; edits keep a visible history; not retractable"
PRIVATE_DURABILITY = {"copies_pushed", "edit_history_visible"}
PRIVATE_WIDENING = {"joiners_read_history", "forks", "visibility_flip"}
RATE_LIMITED = (403, {"message": "API rate limit exceeded"}, {"X-RateLimit-Remaining": "0"})


def _repository(
    visibility="public",
    *,
    owner="example",
    owner_id=10,
    owner_type="User",
    write=False,
    admin=False,
):
    return {
        "full_name": f"{owner}/app",
        "private": visibility != "public",
        "visibility": visibility,
        "owner": {"login": owner, "id": owner_id, "type": owner_type},
        "permissions": {
            "admin": admin,
            "maintain": False,
            "push": write or admin,
            "triage": write or admin,
            "pull": True,
        },
    }


def _user(login, user_id):
    return {"login": login, "id": user_id, "type": "User"}


def _audience(gh, ref=ISSUE, draft=None):
    return correspond.audience(ref, draft, registry={"github": GitHub(run=gh)})


def _org_repository(**changes):
    visibility = changes.pop("visibility", "private")
    return (
        "GET",
        r"repos/example-org/app",
        (
            200,
            _repository(
                visibility, owner="example-org", owner_type="Organization", **changes
            ),
        ),
    )


def _owned_by_example_bot(*collaborators, cut=False):
    """A private repository of the user example-bot, seen by its owner, with these collaborators."""
    pages = r"\d+" if cut else "1"
    return ScriptedGh(
        (
            "GET",
            r"repos/example-bot/app",
            (200, _repository("private", owner="example-bot", owner_id=7, admin=True)),
        ),
        (
            "GET",
            rf"repos/example-bot/app/collaborators\?per_page=100&page={pages}",
            (200, list(collaborators)),
        ),
    )


def test_a_public_repository_is_world_readable_after_one_call_and_a_mention_changes_nothing():
    gh = ScriptedGh(("GET", rf"repos/{APP}", (200, _repository())))
    found = _audience(gh, "github:Example/App#12")
    assert (
        found.ref,
        found.scope,
        found.complete,
        found.external,
        found.retractable,
        found.defaulted,
    ) == (ISSUE, "public", False, True, False, False)
    assert found.readers == () and found.classes == (WATCHERS,)
    assert set(found.durability) == {
        "indexed",
        "archived_by_others",
        "copies_pushed",
        "edit_history_visible",
    }
    assert set(found.widening) == {"forks", "visibility_flip"}
    assert found.in_words() == PUBLIC_WORDS
    assert gh.paths() == [f"repos/{APP}"]
    mentioned = _audience(gh, draft=correspond.Draft(text="@octocat can you look?"))
    assert mentioned.hash == found.hash
    assert gh.paths() == [f"repos/{APP}"] * 2, "nothing is cached"


@pytest.mark.parametrize(
    "organisation, member_class",
    [
        ((200, {"login": "example-org"}), True),
        ((403, {"message": "Must be an organization owner"}), True),
        ((200, {"default_repository_permission": "read"}), True),
        ((200, {"default_repository_permission": "admin"}), True),
        ((200, {"default_repository_permission": "none"}), False),
    ],
)
def test_a_private_organisation_repository_reaches_members_through_the_base_permission(
    organisation, member_class
):
    gh = ScriptedGh(_org_repository(), ("GET", r"orgs/example-org", organisation))
    found = _audience(gh, "github:example-org/app#12")
    assert (found.scope, found.complete, found.external, found.defaulted) == (
        "org",
        False,
        None,
        False,
    )
    expected = {WATCHERS, APPS_AND_WEBHOOKS, SECURITY_MANAGERS, UNLISTED_COLLABORATORS}
    assert set(found.classes) == expected | (
        {BASE_PERMISSION_DEFAULT} if member_class else set()
    )
    assert (set(found.durability), set(found.widening), found.retractable) == (
        PRIVATE_DURABILITY,
        PRIVATE_WIDENING,
        False,
    )
    assert gh.paths() == ["repos/example-org/app", "orgs/example-org"], (
        "only an account that can write is shown collaborators, and the gh account is never asked who it is"
    )


def test_a_known_base_permission_and_the_documented_default_hash_alike():
    def seen(organisation):
        return _audience(
            ScriptedGh(_org_repository(), ("GET", r"orgs/example-org", organisation)),
            "github:example-org/app#12",
        )

    by_owner = seen((200, {"default_repository_permission": "write"}))
    by_member = seen((200, {"login": "example-org"}))
    assert by_owner.hash == by_member.hash and by_owner.evidence != by_member.evidence


def test_collaborators_are_listed_with_write_access_and_stay_a_lower_bound():
    gh = ScriptedGh(
        _org_repository(write=True),
        (
            "GET",
            r"repos/example-org/app/collaborators\?per_page=100&page=1",
            (200, [_user("example-bot", 7), _user("ada", 3)]),
        ),
        (
            "GET",
            r"repos/example-org/app/teams\?per_page=100&page=1",
            (200, [{"slug": "developers", "permission": "push"}]),
        ),
        ("GET", r"orgs/example-org", (200, {"default_repository_permission": "none"})),
    )
    found = _audience(gh, "github:example-org/app#12")
    assert [(r.handle, r.is_self) for r in found.readers] == [
        ("ada", False),
        ("example-bot", False),
    ]
    assert (found.scope, found.complete, found.external) == ("org", False, None)
    assert set(found.classes) == {WATCHERS, APPS_AND_WEBHOOKS, SECURITY_MANAGERS}
    assert any("developers (push)" in line for line in found.evidence)
    assert any("base permission none" in line for line in found.evidence)


def test_a_users_private_repository_is_never_complete_because_apps_and_webhooks_read_it():
    alone = _audience(
        _owned_by_example_bot(_user("example-bot", 7)), "github:example-bot/app#12"
    )
    assert (alone.scope, alone.complete, alone.external) == ("named", False, None)
    assert [r.handle for r in alone.readers] == ["example-bot"]
    assert set(alone.classes) == {WATCHERS, APPS_AND_WEBHOOKS}
    assert alone.in_words() == (
        "named readers; at least 1 known reader; " + APPS_AND_WEBHOOKS + "; "
        "emailed to watchers and participants; edits keep a visible history; not retractable"
    )
    shared = _audience(
        _owned_by_example_bot(_user("example-bot", 7), _user("ada", 3)),
        "github:example-bot/app#12",
    )
    assert (shared.complete, shared.external, len(shared.readers)) == (False, True, 2)
    reordered = _audience(
        _owned_by_example_bot(_user("ada", 3), _user("example-bot", 7)),
        "github:example-bot/app#12",
    )
    assert reordered.hash == shared.hash, "the platform's listing order is not the audience"


def test_the_audience_does_not_depend_on_who_asks_or_whether_gh_can_say():
    routes = (
        ("GET", r"repos/example-bot/app", (200, _repository("private", owner="example-bot", owner_id=7, admin=True))),
        (
            "GET",
            r"repos/example-bot/app/collaborators\?per_page=100&page=1",
            (200, [_user("example-bot", 7), _user("ada", 3)]),
        ),
    )
    other_account = ScriptedGh(("GET", "user", (200, {"login": "ada"})), *routes)
    no_account = ScriptedGh(("GET", "user", (502, {"message": "Bad Gateway"})), *routes)
    first = _audience(other_account, "github:example-bot/app#12")
    second = _audience(no_account, "github:example-bot/app#12")
    assert first.hash == second.hash
    assert "user" not in other_account.paths() + no_account.paths()


def test_a_users_private_repository_seen_by_a_reader_is_only_a_lower_bound():
    gh = ScriptedGh(("GET", rf"repos/{APP}", (200, _repository("private"))))
    found = _audience(gh)
    assert (found.scope, found.complete, found.external) == ("named", False, None)
    assert [r.handle for r in found.readers] == ["example"]
    assert set(found.classes) == {WATCHERS, APPS_AND_WEBHOOKS, UNLISTED_COLLABORATORS}


def test_collaborators_refused_despite_write_access_leave_the_list_unknown():
    gh = ScriptedGh(
        ("GET", rf"repos/{APP}", (200, _repository("private", write=True))),
        (
            "GET",
            rf"repos/{APP}/collaborators\?per_page=100&page=1",
            (403, {"message": "Must have push access to view repository collaborators."}),
        ),
    )
    found = _audience(gh)
    assert (found.complete, found.defaulted) == (False, False)
    assert UNLISTED_COLLABORATORS in found.classes
    assert any(line.startswith("collaborators could not be listed") for line in found.evidence)


def test_a_collaborator_list_cut_short_says_so():
    page = [_user(f"member-{i}", 1000 + i) for i in range(100)]
    found = _audience(_owned_by_example_bot(*page, cut=True), "github:example-bot/app#12")
    assert (found.scope, found.complete, found.external) == ("named", False, True)
    assert any("so the list is a lower bound" in line for line in found.evidence)


def test_an_internal_repository_reaches_the_whole_enterprise():
    gh = ScriptedGh(
        _org_repository(visibility="internal"),
        ("GET", r"orgs/example-org", (403, {"message": "Forbidden"})),
    )
    found = _audience(gh, "github:example-org/app#12")
    assert (found.scope, found.complete, found.external) == ("org", False, None)
    assert any("enterprise that owns example-org" in c for c in found.classes)
    assert {BASE_PERMISSION_DEFAULT, APPS_AND_WEBHOOKS} <= set(found.classes)


@pytest.mark.parametrize(
    "status, message",
    [(404, "Not Found"), (403, "Resource not accessible by integration")],
)
def test_a_repository_the_account_cannot_see_resolves_to_public(status, message):
    gh = ScriptedGh(("GET", rf"repos/{APP}", (status, {"message": message})))
    found = _audience(gh)
    assert (found.scope, found.complete, found.defaulted, found.external) == (
        "public",
        False,
        True,
        None,
    )
    failed = found.evidence[0]
    assert f"GET repos/{APP}" in failed and str(status) in failed and message in failed


def test_a_rate_limit_raises_in_the_adapter_and_resolves_to_public_through_the_registry():
    limited = ScriptedGh(("GET", rf"repos/{APP}", RATE_LIMITED))
    adapter = GitHub(run=limited)
    with pytest.raises(ChannelError) as caught:
        adapter.audience(adapter.parse_ref(f"{APP}#12"))
    assert caught.value.kind == "rate_limited"
    found = _audience(limited)
    assert found.defaulted and "rate limit" in found.evidence[0]

    organisation_limited = ScriptedGh(
        _org_repository(), ("GET", r"orgs/example-org", RATE_LIMITED)
    )
    found = _audience(organisation_limited, "github:example-org/app#12")
    assert found.defaulted and "rate limit" in found.evidence[0], (
        "a rate limit is not the base permission being hidden"
    )

    def offline(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv, 1, stdout="", stderr="error connecting to api.github.com: could not resolve host"
        )

    unreachable = _audience(offline)
    assert unreachable.defaulted and "could not reach GitHub" in unreachable.evidence[0]


def test_unknown_visibility_resolves_to_public():
    odd = _repository()
    odd["visibility"] = "secret-club"
    found = _audience(ScriptedGh(("GET", rf"repos/{APP}", (200, odd))))
    assert found.defaulted and "secret-club" in found.evidence[-2]
    older = _repository("private")
    del older["visibility"]
    fallback = _audience(ScriptedGh(("GET", rf"repos/{APP}", (200, older))))
    assert (fallback.scope, fallback.defaulted) == ("named", False)


def _cli(monkeypatch, capsys, gh, *args):
    monkeypatch.setattr(registry, "channels", lambda: {"github": GitHub(run=gh)})
    with pytest.raises(SystemExit) as done:
        cli(list(args))
    return done.value.code, capsys.readouterr().out


def test_correspond_audience_on_the_command_line(monkeypatch, capsys):
    public = ScriptedGh(("GET", rf"repos/{APP}", (200, _repository())))
    code, out = _cli(monkeypatch, capsys, public, "audience", ISSUE)
    assert code == 0 and out.splitlines()[0] == PUBLIC_WORDS
    code, out = _cli(monkeypatch, capsys, public, "audience", ISSUE, "--json")
    assert code == 0 and json.loads(out)["audience"]["scope"] == "public"

    organisation = ScriptedGh(
        _org_repository(), ("GET", r"orgs/example-org", (403, {"message": "Forbidden"}))
    )
    _, out = _cli(monkeypatch, capsys, organisation, "audience", "github:example-org/app#12")
    assert {"scope: org", f"class: {BASE_PERMISSION_DEFAULT}", "complete: false"} <= set(
        out.splitlines()
    )

    hidden = ScriptedGh(("GET", rf"repos/{APP}", (404, {"message": "Not Found"})))
    code, out = _cli(monkeypatch, capsys, hidden, "audience", ISSUE)
    lines = out.splitlines()
    assert code == 0 and {"scope: public", "defaulted: true"} <= set(lines)
    assert any(
        line.startswith("evidence: ") and f"GET repos/{APP}" in line for line in lines
    )

    code, out = _cli(monkeypatch, capsys, ScriptedGh(), "capabilities", "github")
    assert code == 0 and "audience: full" in out.splitlines()
    code, out = _cli(monkeypatch, capsys, ScriptedGh(), "capabilities", "github", "--json")
    assert code == 0 and json.loads(out)["capabilities"]["audience"] == "full"
