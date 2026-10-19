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
from correspond.model import Draft, Support

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
NO_DISCUSSIONS = {
    "data": {
        "repository": {
            "discussions": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [],
            }
        }
    }
}
#: A repository poll's further reads, answered with nothing: no review comments, no discussions.
QUIET = (
    ("GET", rf"repos/{REPO}/pulls/comments\?.*", (200, [])),
    ("POST", "graphql", (200, NO_DISCUSSIONS)),
)


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


def test_dry_runs_run_no_gh_command_that_writes():
    """A dry run changes nothing on GitHub; the only calls a send or edit attempts are the audience's reads (liaise discussion 32, §3.4)."""
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
        correspond.label(
            f"github:{REPO}#1", ["needs-triage", "bug"], dry_run=True, registry=registry
        ),
        correspond.unlabel(f"github:{REPO}#1", ["bug"], dry_run=True, registry=registry),
    ]
    assert all(r.ok and r.dry_run for r in results)
    assert gh.calls and {c["method"] for c in gh.calls} == {"GET"}
    assert all(r.plan["audience"] for r in results[:3])
    assert (
        results[1].plan["action"] == "open an issue"
        and "discussion" in results[0].plan["request"]
    )
    assert results[4].plan["labels"] == ["needs-triage", "bug"]
    assert results[4].plan["action"] == "add labels"
    assert results[5].plan["action"] == "remove labels"


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


def test_label_and_unlabel_refuse_a_bare_repository_with_no_gh_call():
    gh = ScriptedGh()
    registry = {"github": GitHub(run=gh)}
    labelled = correspond.label(f"github:{REPO}", ["bug"], registry=registry)
    unlabelled = correspond.unlabel(f"github:{REPO}", ["bug"], registry=registry)
    assert (labelled.ok, labelled.error_kind) == (False, "validation")
    assert (unlabelled.ok, unlabelled.error_kind) == (False, "validation")
    assert gh.calls == []


def test_label_adds_labels_and_reports_what_landed():
    gh = ScriptedGh(
        WHOAMI,
        ("GET", rf"repos/{REPO}/issues/1", (200, _issue())),
        (
            "POST",
            rf"repos/{REPO}/issues/1/labels",
            (200, [{"name": "bug"}, {"name": "needs-triage"}]),
        ),
    )
    result = correspond.label(
        f"github:{REPO}#1",
        ["bug", "needs-triage", "bug"],
        registry={"github": GitHub(run=gh)},
    )
    assert result.ok and result.message_id == "issue-1"
    assert result.plan["labels"] == ["bug", "needs-triage"]  # de-duplicated
    assert result.plan["landed"] == ["bug", "needs-triage"]
    assert "not_applied" not in result.plan
    post = next(c for c in gh.calls if c["method"] == "POST")
    assert post["payload"] == {"labels": ["bug", "needs-triage"]}


def test_label_reports_a_label_the_response_did_not_include():
    gh = ScriptedGh(
        WHOAMI,
        ("GET", rf"repos/{REPO}/issues/1", (200, _issue())),
        ("POST", rf"repos/{REPO}/issues/1/labels", (200, [{"name": "bug"}])),
    )
    result = correspond.label(
        f"github:{REPO}#1", ["bug", "wontfix"], registry={"github": GitHub(run=gh)}
    )
    assert result.ok and result.plan["not_applied"] == ["wontfix"]


def test_label_landed_excludes_labels_the_issue_already_carried():
    """The POST response is every label now on the issue, not only the ones this call added."""
    gh = ScriptedGh(
        WHOAMI,
        ("GET", rf"repos/{REPO}/issues/1", (200, _issue())),
        (
            "POST",
            rf"repos/{REPO}/issues/1/labels",
            (200, [{"name": "priority:high"}, {"name": "bug"}]),
        ),
    )
    result = correspond.label(
        f"github:{REPO}#1", ["bug"], registry={"github": GitHub(run=gh)}
    )
    assert result.ok
    assert result.plan["landed"] == ["bug"]
    assert "priority:high" not in result.plan["landed"]
    assert "not_applied" not in result.plan


def test_unlabel_removes_labels_one_call_each_and_tolerates_an_absent_one():
    gh = ScriptedGh(
        WHOAMI,
        ("GET", rf"repos/{REPO}/issues/1", (200, _issue())),
        ("DELETE", rf"repos/{REPO}/issues/1/labels/bug", (200, [])),
        (
            "DELETE",
            rf"repos/{REPO}/issues/1/labels/wontfix",
            (404, {"message": "Label does not exist"}),
        ),
    )
    result = correspond.unlabel(
        f"github:{REPO}#1", ["bug", "wontfix"], registry={"github": GitHub(run=gh)}
    )
    assert result.ok and result.message_id == "issue-1"
    assert result.plan["removed"] == ["bug"]
    assert result.plan["already_absent"] == ["wontfix"]
    deletes = [c for c in gh.calls if c["method"] == "DELETE"]
    assert len(deletes) == 2 and all(c["payload"] is None for c in deletes)


def test_unlabel_a_label_with_special_characters_is_url_escaped():
    gh = ScriptedGh(
        WHOAMI,
        ("GET", rf"repos/{REPO}/issues/1", (200, _issue())),
        ("DELETE", rf"repos/{REPO}/issues/1/labels/help%20wanted", (200, [])),
    )
    result = correspond.unlabel(
        f"github:{REPO}#1", ["help wanted"], registry={"github": GitHub(run=gh)}
    )
    assert result.ok and result.plan["removed"] == ["help wanted"]


def test_label_on_a_discussion_is_refused_by_name():
    gh = ScriptedGh(
        WHOAMI,
        ("GET", rf"repos/{REPO}/issues/5", (404, {"message": "Not Found"})),
        (
            "POST",
            "graphql",
            (200, {"data": {"repository": {"discussion": {"id": "D_node5"}}}}),
        ),
    )
    with pytest.raises(NotSupported, match="label"):
        correspond.label(f"github:{REPO}#5", ["bug"], registry={"github": GitHub(run=gh)})


def test_unlabel_on_a_number_that_is_nothing_at_all_is_not_found():
    gh = ScriptedGh(
        WHOAMI,
        ("GET", rf"repos/{REPO}/issues/9", (404, {"message": "Not Found"})),
        ("POST", "graphql", (200, {"data": {"repository": {"discussion": None}}})),
    )
    result = correspond.unlabel(
        f"github:{REPO}#9", ["bug"], registry={"github": GitHub(run=gh)}
    )
    assert (result.ok, result.error_kind) == (False, "not_found")


def test_label_and_unlabel_refuse_an_empty_label_list_with_no_gh_call():
    gh = ScriptedGh()
    registry = {"github": GitHub(run=gh)}
    labelled = correspond.label(f"github:{REPO}#1", ["  ", ""], registry=registry)
    unlabelled = correspond.unlabel(f"github:{REPO}#1", [], registry=registry)
    assert (labelled.ok, labelled.error_kind) == (False, "validation")
    assert (unlabelled.ok, unlabelled.error_kind) == (False, "validation")
    assert gh.calls == []


def test_label_and_unlabel_are_named_in_capabilities():
    caps = GitHub().capabilities
    assert caps.label.value == "full" and caps.unlabel.value == "full"
    assert "label" in caps.operations and "unlabel" in caps.operations


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
        *QUIET,
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


def test_listening_to_one_discussion_delivers_its_new_and_edited_posts_once():
    since = "2026-09-11T09:00:00Z"
    replies = [_post(503, created="2026-09-11T10:05:00Z", replies=None)]
    thread = _discussion(
        [
            _post(501, created="2026-09-11T08:40:00Z", edited="2026-09-11T09:30:00Z"),
            _post(502, created="2026-09-11T08:50:00Z", replies=replies),
        ]
    )
    gh = ScriptedGh(
        WHOAMI,
        ("GET", rf"repos/{REPO}/issues/5", (404, {"message": "Not Found"})),
        ("POST", "graphql", (200, thread)),
    )
    cursors = {f"github:{REPO}#5": since}
    registry = {"github": GitHub(run=gh)}
    events = list(
        correspond.listen(f"github:{REPO}#5", cursors=cursors, registry=registry)
    )
    assert [(e.kind, e.message.id) for e in events] == [
        ("message.updated", "discussioncomment-501"),
        ("message.created", "discussioncomment-503"),
    ], "the discussion (08:30) and comment 502 (08:50) predate the cursor"
    assert events[1].message.reply_to == "discussioncomment-502"
    assert (
        events[1].delivery_id
        == f"github:{REPO}:discussioncomment-503@2026-09-11T10:05:00Z"
    )
    again = list(
        correspond.listen(f"github:{REPO}#5", cursors=cursors, registry=registry)
    )
    assert again == []


def test_listening_to_a_number_that_is_nothing_is_not_found():
    gh = ScriptedGh(
        WHOAMI,
        ("GET", rf"repos/{REPO}/issues/9", (404, {"message": "Not Found"})),
        ("POST", "graphql", (200, {"data": {"repository": {"discussion": None}}})),
    )
    with pytest.raises(ChannelError) as caught:
        list(
            correspond.listen(
                f"github:{REPO}#9", cursors={}, registry={"github": GitHub(run=gh)}
            )
        )
    assert caught.value.kind == "not_found"


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
        *QUIET,
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
        *QUIET,
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
RATE_LIMITED = (
    403,
    {"message": "API rate limit exceeded"},
    {"X-RateLimit-Remaining": "0"},
)


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
    assert reordered.hash == shared.hash, (
        "the platform's listing order is not the audience"
    )


def test_the_audience_does_not_depend_on_who_asks_or_whether_gh_can_say():
    routes = (
        (
            "GET",
            r"repos/example-bot/app",
            (200, _repository("private", owner="example-bot", owner_id=7, admin=True)),
        ),
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
    assert any(
        line.startswith("collaborators could not be listed") for line in found.evidence
    )


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
            argv,
            1,
            stdout="",
            stderr="error connecting to api.github.com: could not resolve host",
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
    _, out = _cli(
        monkeypatch, capsys, organisation, "audience", "github:example-org/app#12"
    )
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
    code, out = _cli(
        monkeypatch, capsys, ScriptedGh(), "capabilities", "github", "--json"
    )
    assert code == 0 and json.loads(out)["capabilities"]["audience"] == "full"


# ---------------------------------------------------- create-time fields, close, reopen (#26)


def _created(number=7, *, labels=(), assignees=(), milestone=None):
    data = _issue(number, labels=labels)
    data["assignees"] = [{"login": login} for login in assignees]
    data["milestone"] = None if milestone is None else {"number": milestone}
    return data


def test_opening_an_issue_sets_labels_assignees_and_milestone_in_the_one_request():
    gh = ScriptedGh(
        WHOAMI,
        (
            "POST",
            rf"repos/{REPO}/issues",
            (201, _created(labels=("question",), assignees=("Ada",), milestone=3)),
        ),
    )
    result = correspond.send(
        f"github:{REPO}",
        Draft(
            title="Q20",
            text="Where do the phase dates stop?",
            native={"labels": ["question"], "assignees": ["ada"], "milestone": 3},
        ),
        registry={"github": GitHub(run=gh)},
    )
    assert result.ok and result.conversation == f"github:{REPO}#7"
    (post,) = [c for c in gh.calls if c["method"] == "POST"]
    assert post["payload"] == {
        "title": "Q20",
        "body": "Where do the phase dates stop?",
        "labels": ["question"],
        "assignees": ["ada"],
        "milestone": 3,
    }
    assert result.plan["landed"] == {
        "labels": ["question"],
        "assignees": ["ada"],
        "milestone": 3,
    }
    assert "not_applied" not in result.plan


def test_an_assignee_github_silently_dropped_is_reported_as_not_applied():
    gh = ScriptedGh(
        WHOAMI,
        ("POST", rf"repos/{REPO}/issues", (201, _created(assignees=("ada",)))),
    )
    result = correspond.send(
        f"github:{REPO}",
        Draft(title="Q", text="?", native={"assignees": ["ada", "not-a-member"]}),
        registry={"github": GitHub(run=gh)},
    )
    assert result.ok
    assert result.plan["not_applied"] == {"assignees": ["not-a-member"]}


def test_the_dry_run_plan_shows_the_creation_fields_and_posts_nothing():
    gh = ScriptedGh(WHOAMI, ("GET", r".*", (404, {"message": "Not Found"})))
    result = correspond.send(
        f"github:{REPO}",
        Draft(title="Q", text="?", native={"labels": ["question"]}),
        dry_run=True,
        registry={"github": GitHub(run=gh)},
    )
    assert result.ok and result.dry_run and result.plan["labels"] == ["question"]
    assert "POST" not in {c["method"] for c in gh.calls}


def test_an_unknown_native_field_is_refused_not_dropped():
    gh = ScriptedGh()
    result = correspond.send(
        f"github:{REPO}",
        Draft(title="Q", text="?", native={"assignee": "ada"}),
        registry={"github": GitHub(run=gh)},
    )
    assert (result.ok, result.error_kind) == (False, "validation")
    assert "assignee" in result.error and "assignees" in result.error
    assert gh.calls == []


def test_native_fields_on_a_comment_are_refused():
    gh = ScriptedGh(WHOAMI)
    result = correspond.send(
        f"github:{REPO}#1",
        Draft(text="hi", native={"labels": ["bug"]}),
        registry={"github": GitHub(run=gh)},
    )
    assert (result.ok, result.error_kind) == (False, "validation")
    assert "POST" not in {c["method"] for c in gh.calls}


@pytest.mark.parametrize(
    "native",
    [{"labels": "bug"}, {"assignees": [""]}, {"milestone": "3"}, {"milestone": True}],
)
def test_malformed_native_values_are_validation_failures(native):
    result = correspond.send(
        f"github:{REPO}",
        Draft(title="Q", text="?", native=native),
        dry_run=True,
        registry={"github": GitHub(run=ScriptedGh())},
    )
    assert (result.ok, result.error_kind) == (False, "validation")


def test_close_patches_the_state_with_a_reason_and_reads_it_back():
    closed = {**_issue(), "state": "closed", "state_reason": "completed"}
    gh = ScriptedGh(
        WHOAMI,
        ("GET", rf"repos/{REPO}/issues/1", (200, _issue())),
        ("PATCH", rf"repos/{REPO}/issues/1", (200, closed)),
    )
    result = correspond.close(
        f"github:{REPO}#1", reason="completed", registry={"github": GitHub(run=gh)}
    )
    assert result.ok and result.operation == "close"
    (patch,) = [c for c in gh.calls if c["method"] == "PATCH"]
    assert patch["payload"] == {"state": "closed", "state_reason": "completed"}
    assert (result.plan["was"], result.plan["now"], result.plan["now_reason"]) == (
        "open",
        "closed",
        "completed",
    )


def test_reopen_patches_the_state_back_to_open():
    gh = ScriptedGh(
        WHOAMI,
        ("GET", rf"repos/{REPO}/issues/1", (200, {**_issue(), "state": "closed"})),
        ("PATCH", rf"repos/{REPO}/issues/1", (200, _issue())),
    )
    result = correspond.reopen(f"github:{REPO}#1", registry={"github": GitHub(run=gh)})
    assert result.ok and result.plan["now"] == "open" and result.plan["was"] == "closed"
    (patch,) = [c for c in gh.calls if c["method"] == "PATCH"]
    assert patch["payload"] == {"state": "open"}


def test_close_dry_run_patches_nothing():
    gh = ScriptedGh(WHOAMI, ("GET", r".*", (404, {"message": "Not Found"})))
    result = correspond.close(
        f"github:{REPO}#1",
        reason="not_planned",
        dry_run=True,
        registry={"github": GitHub(run=gh)},
    )
    assert result.ok and result.dry_run and result.plan["state_reason"] == "not_planned"
    assert "PATCH" not in {c["method"] for c in gh.calls}


def test_close_refuses_an_unknown_reason_and_a_bare_repository_with_no_gh_call():
    registry = {"github": GitHub(run=ScriptedGh())}
    for result in (
        correspond.close(f"github:{REPO}#1", reason="done", registry=registry),
        correspond.close(f"github:{REPO}", registry=registry),
    ):
        assert (result.ok, result.error_kind) == (False, "validation")


def _discussion_state(closed, reason=None, number=5):
    return {
        "data": {
            "repository": {
                "discussion": {
                    "id": f"D_node{number}",
                    "closed": closed,
                    "stateReason": reason,
                    "url": f"https://github.com/{REPO}/discussions/{number}",
                }
            }
        }
    }


def _discussion_mutation(field, closed, reason=None, number=5):
    return {
        "data": {
            field: {
                "discussion": {
                    "closed": closed,
                    "stateReason": reason,
                    "url": f"https://github.com/{REPO}/discussions/{number}",
                }
            }
        }
    }


def _graphql_router(*, state, mutation):
    """One graphql route: the state query, then the mutation (told apart by the query text)."""
    seen = []

    def reply(payload):
        seen.append(payload)
        query = payload.get("query", "")
        return (200, mutation if "mutation" in query else state)

    return reply, seen


def test_close_a_discussion_through_graphql_and_read_it_back():
    reply, seen = _graphql_router(
        state=_discussion_state(False),
        mutation=_discussion_mutation("closeDiscussion", True, "OUTDATED"),
    )
    gh = ScriptedGh(
        WHOAMI,
        ("GET", rf"repos/{REPO}/issues/5", (404, {"message": "Not Found"})),
        ("POST", "graphql", reply),
    )
    result = correspond.close(
        f"github:{REPO}#5", reason="outdated", registry={"github": GitHub(run=gh)}
    )
    assert result.ok and result.operation == "close"
    assert result.message_id == "discussion-5"
    assert (result.plan["was"], result.plan["now"], result.plan["now_reason"]) == (
        "open",
        "closed",
        "outdated",
    )
    mutation = seen[-1]
    assert "closeDiscussion" in mutation["query"]
    assert mutation["variables"] == {"discussionId": "D_node5", "reason": "OUTDATED"}
    assert "PATCH" not in {c["method"] for c in gh.calls}


def test_close_a_discussion_maps_completed_to_resolved():
    reply, seen = _graphql_router(
        state=_discussion_state(False),
        mutation=_discussion_mutation("closeDiscussion", True, "RESOLVED"),
    )
    gh = ScriptedGh(
        WHOAMI,
        ("GET", rf"repos/{REPO}/issues/5", (404, {"message": "Not Found"})),
        ("POST", "graphql", reply),
    )
    result = correspond.close(
        f"github:{REPO}#5", reason="completed", registry={"github": GitHub(run=gh)}
    )
    assert result.ok and result.plan["reason"] == "resolved"
    assert seen[-1]["variables"]["reason"] == "RESOLVED"


def test_close_a_discussion_without_a_reason_sends_none():
    reply, seen = _graphql_router(
        state=_discussion_state(False),
        mutation=_discussion_mutation("closeDiscussion", True, "RESOLVED"),
    )
    gh = ScriptedGh(
        WHOAMI,
        ("GET", rf"repos/{REPO}/issues/5", (404, {"message": "Not Found"})),
        ("POST", "graphql", reply),
    )
    result = correspond.close(f"github:{REPO}#5", registry={"github": GitHub(run=gh)})
    assert result.ok and "reason" not in seen[-1]["variables"]


def test_close_a_discussion_with_an_issue_only_reason_is_refused_before_writing():
    reply, seen = _graphql_router(
        state=_discussion_state(False),
        mutation=_discussion_mutation("closeDiscussion", True, "RESOLVED"),
    )
    gh = ScriptedGh(
        WHOAMI,
        ("GET", rf"repos/{REPO}/issues/5", (404, {"message": "Not Found"})),
        ("POST", "graphql", reply),
    )
    result = correspond.close(
        f"github:{REPO}#5", reason="not_planned", registry={"github": GitHub(run=gh)}
    )
    assert (result.ok, result.error_kind) == (False, "validation")
    assert not any("mutation" in p.get("query", "") for p in seen)


def test_close_an_issue_with_a_discussion_only_reason_is_refused():
    gh = ScriptedGh(WHOAMI, ("GET", rf"repos/{REPO}/issues/1", (200, _issue())))
    result = correspond.close(
        f"github:{REPO}#1", reason="outdated", registry={"github": GitHub(run=gh)}
    )
    assert (result.ok, result.error_kind) == (False, "validation")
    assert "PATCH" not in {c["method"] for c in gh.calls}


def test_reopen_a_discussion_through_graphql():
    reply, seen = _graphql_router(
        state=_discussion_state(True, "RESOLVED"),
        mutation=_discussion_mutation("reopenDiscussion", False, "REOPENED"),
    )
    gh = ScriptedGh(
        WHOAMI,
        ("GET", rf"repos/{REPO}/issues/5", (404, {"message": "Not Found"})),
        ("POST", "graphql", reply),
    )
    result = correspond.reopen(f"github:{REPO}#5", registry={"github": GitHub(run=gh)})
    assert result.ok and (result.plan["was"], result.plan["now"]) == ("closed", "open")
    assert "now_reason" not in result.plan
    assert "reopenDiscussion" in seen[-1]["query"]
    assert seen[-1]["variables"] == {"discussionId": "D_node5"}


def test_close_on_nothing_at_that_number_is_not_found():
    gh = ScriptedGh(
        WHOAMI,
        ("GET", rf"repos/{REPO}/issues/9", (404, {"message": "Not Found"})),
        ("POST", "graphql", (200, {"data": {"repository": {"discussion": None}}})),
    )
    result = correspond.close(f"github:{REPO}#9", registry={"github": GitHub(run=gh)})
    assert (result.ok, result.error_kind) == (False, "not_found")


def test_close_dry_run_plans_for_either_kind_and_reads_nothing():
    gh = ScriptedGh(WHOAMI)
    result = correspond.close(
        f"github:{REPO}#5",
        reason="resolved",
        dry_run=True,
        registry={"github": GitHub(run=gh)},
    )
    assert result.ok and result.dry_run
    assert (
        "closeDiscussion" in result.plan["request"] and "PATCH" in result.plan["request"]
    )
    # (the before_send audience may read the repository; the close itself reads nothing)
    assert not [c for c in gh.calls if "/issues/" in c["path"] or c["path"] == "graphql"]


def test_close_with_a_reason_on_a_pull_request_is_refused():
    gh = ScriptedGh(
        WHOAMI,
        ("GET", rf"repos/{REPO}/issues/2", (200, _issue(2, pr=True))),
    )
    result = correspond.close(
        f"github:{REPO}#2", reason="completed", registry={"github": GitHub(run=gh)}
    )
    assert (result.ok, result.error_kind) == (False, "validation")
    assert "PATCH" not in {c["method"] for c in gh.calls}


def test_close_reopen_and_draft_fields_are_in_capabilities():
    caps = correspond.capabilities("github")
    assert {"close", "reopen"} <= set(caps.operations)
    assert (caps.close, caps.reopen) == (Support.FULL, Support.FULL)
    assert caps.draft_fields == ("labels", "assignees", "milestone", "category")


# ------------------------------- review threads, discussion listening, opening a discussion (#9)


def _review(
    comment_id,
    *,
    pull=3,
    reply_to=None,
    created="2026-09-11T09:00:00Z",
    updated=None,
    user="octocat",
):
    data = {
        "id": comment_id,
        "user": {"login": user, "id": 3, "type": "User"},
        "body": f"review {comment_id}",
        "created_at": created,
        "updated_at": updated or created,
        "author_association": "MEMBER",
        "html_url": f"https://github.com/{REPO}/pull/{pull}#discussion_r{comment_id}",
        "pull_request_url": f"https://api.github.com/repos/{REPO}/pulls/{pull}",
        "path": "src/export.py",
        "line": 12,
        "original_line": 12,
        "pull_request_review_id": 40,
        "diff_hunk": "@@ -10,3 +10,3 @@",
    }
    if reply_to is not None:
        data["in_reply_to_id"] = reply_to
    return data


def _pull_routes(*reviews, comments=()):
    return (
        ("GET", rf"repos/{REPO}/issues/3", (200, _issue(3, pr=True))),
        (
            "GET",
            rf"repos/{REPO}/issues/3/comments\?per_page=100&page=1",
            (200, list(comments)),
        ),
        (
            "GET",
            rf"repos/{REPO}/pulls/3/comments\?per_page=100&page=1",
            (200, list(reviews)),
        ),
    )


def test_reading_a_pull_request_threads_its_review_comments_under_their_first():
    gh = ScriptedGh(
        WHOAMI,
        *_pull_routes(
            _review(61, created="2026-09-11T09:00:00Z"),
            _review(62, reply_to=61, created="2026-09-11T09:20:00Z"),
            comments=[_comment(11, issue=3, created="2026-09-11T09:10:00Z")],
        ),
    )
    messages = _read(gh, f"github:{REPO}#3")
    assert [m.id for m in messages] == [
        "issue-3",
        "discussion_r61",
        "issuecomment-11",
        "discussion_r62",
    ]
    first, reply = messages[1], messages[3]
    assert first.reply_to is None and first.conversation.kind == "pull_request"
    assert reply.reply_to == reply.thread_root == "discussion_r61"
    assert first.native == {
        "path": "src/export.py",
        "line": 12,
        "original_line": 12,
        "review_id": 40,
        "diff_hunk": "@@ -10,3 +10,3 @@",
    }
    assert first.edited_at is None, (
        "updated_at moves on review submission, not only on edits"
    )
    assert set(first.native) <= set(GitHub().capabilities.native_fields)


def test_an_issue_is_read_without_asking_for_review_comments():
    gh = ScriptedGh(
        WHOAMI,
        ("GET", rf"repos/{REPO}/issues/1", (200, _issue())),
        ("GET", rf"repos/{REPO}/issues/1/comments\?per_page=100&page=1", (200, [])),
    )
    assert [m.id for m in _read(gh, f"github:{REPO}#1")] == ["issue-1"]
    assert not any("pulls" in path for path in gh.paths())


def test_a_reply_to_a_review_reply_joins_the_thread_of_its_first_comment():
    gh = ScriptedGh(
        WHOAMI,
        ("GET", rf"repos/{REPO}/issues/3", (200, _issue(3, pr=True))),
        ("GET", rf"repos/{REPO}/pulls/comments/62", (200, _review(62, reply_to=61))),
        (
            "POST",
            rf"repos/{REPO}/pulls/3/comments/61/replies",
            (201, _review(63, reply_to=61)),
        ),
    )
    result = correspond.send(
        f"github:{REPO}#3",
        "agreed",
        reply_to="discussion_r62",
        registry={"github": GitHub(run=gh)},
    )
    assert result.ok and result.message_id == "discussion_r63"
    assert result.plan["thread_root"] == "discussion_r61"
    (post,) = [c for c in gh.calls if c["method"] == "POST"]
    assert post["payload"] == {"body": "agreed"} and "agreed" not in post["argv"]


def test_a_review_comment_on_another_pull_request_is_refused_before_writing():
    gh = ScriptedGh(
        WHOAMI,
        ("GET", rf"repos/{REPO}/issues/3", (200, _issue(3, pr=True))),
        ("GET", rf"repos/{REPO}/pulls/comments/70", (200, _review(70, pull=4))),
    )
    result = correspond.send(
        f"github:{REPO}#3",
        "x",
        reply_to="discussion_r70",
        registry={"github": GitHub(run=gh)},
    )
    assert (result.ok, result.error_kind) == (False, "validation")
    assert "POST" not in {c["method"] for c in gh.calls}


def test_a_review_reply_on_an_issue_is_refused_by_name():
    gh = ScriptedGh(WHOAMI, ("GET", rf"repos/{REPO}/issues/1", (200, _issue())))
    with pytest.raises(NotSupported, match="discussion_rID"):
        correspond.send(
            f"github:{REPO}#1",
            "x",
            reply_to="discussion_r61",
            registry={"github": GitHub(run=gh)},
        )


def test_a_dry_run_review_reply_plans_the_thread_endpoint_and_reads_nothing():
    gh = ScriptedGh(WHOAMI, ("GET", rf"repos/{REPO}$", (404, {"message": "Not Found"})))
    result = correspond.send(
        f"github:{REPO}#3",
        "x",
        reply_to="discussion_r62",
        dry_run=True,
        registry={"github": GitHub(run=gh)},
    )
    assert result.ok and "/replies" in result.plan["request"]
    assert not any("pulls" in path or "issues" in path for path in gh.paths())


def test_edit_and_react_on_a_review_comment_use_the_pulls_endpoints():
    gh = ScriptedGh(
        WHOAMI,
        (
            "PATCH",
            rf"repos/{REPO}/pulls/comments/61",
            (200, {"html_url": "https://example.org/r61"}),
        ),
        (
            "POST",
            rf"repos/{REPO}/pulls/comments/61/reactions",
            (201, {"content": "eyes"}),
        ),
    )
    registry = {"github": GitHub(run=gh)}
    assert correspond.edit(
        f"github:{REPO}#3", "discussion_r61", "fixed", registry=registry
    ).url.endswith("r61")
    assert correspond.react(
        f"github:{REPO}#3", "discussion_r61", "eyes", registry=registry
    ).ok
    for bad in ("discussion_r", "discussion_r-5", "discussion-r5"):
        assert (
            correspond.edit(f"github:{REPO}#3", bad, "x", registry=registry).error_kind
            == "validation"
        )


def test_listening_to_a_pull_request_includes_review_comment_activity():
    since = "2026-09-11T08:00:00Z"
    gh = ScriptedGh(
        WHOAMI,
        ("GET", rf"repos/{REPO}/issues/3", (200, _issue(3, pr=True))),
        (
            "GET",
            rf"repos/{REPO}/issues/3/comments\?since={since}&per_page=100&page=1",
            (200, []),
        ),
        (
            "GET",
            rf"repos/{REPO}/pulls/3/comments\?since={since}&per_page=100&page=1",
            (
                200,
                [
                    _review(
                        61, created="2026-09-11T07:00:00Z", updated="2026-09-11T08:30:00Z"
                    ),
                    _review(62, reply_to=61, created="2026-09-11T08:10:00Z"),
                ],
            ),
        ),
    )
    events = list(
        correspond.listen(
            f"github:{REPO}#3",
            cursors={f"github:{REPO}#3": since},
            registry={"github": GitHub(run=gh)},
        )
    )
    assert [(e.kind, e.message.id) for e in events] == [
        ("message.created", "discussion_r62"),
        ("message.updated", "discussion_r61"),
    ]


def _activity(*nodes, next_page=None):
    return {
        "data": {
            "repository": {
                "discussions": {
                    "pageInfo": {
                        "hasNextPage": next_page is not None,
                        "endCursor": next_page,
                    },
                    "nodes": [{"number": n, "updatedAt": t} for n, t in nodes],
                }
            }
        }
    }


def _numbered_discussion(number, created, comments=()):
    data = _discussion(comments)
    data["data"]["repository"]["discussion"].update(
        id=f"D_node{number}", number=number, createdAt=created
    )
    return data


def test_polling_a_repository_finds_review_comments_and_active_discussions():
    since = "2026-09-11T08:00:00Z"
    date = {"Date": "Fri, 11 Sep 2026 12:00:00 GMT"}
    discussions = {
        5: _numbered_discussion(
            5,
            "2026-09-01T00:00:00Z",
            [_post(501, created="2026-09-11T09:00:00Z", replies=[])],
        ),
        6: _numbered_discussion(6, "2026-09-11T10:00:00Z"),
    }

    def graphql(payload):
        variables = payload["variables"]
        if "discussions(" in payload["query"]:
            if variables["after"] is None:
                # One page, newest activity first: 8 is older than the cursor, 5 after it still counts.
                return 200, _activity(
                    (6, "2026-09-11T10:00:00Z"),
                    (8, "2026-09-10T00:00:00Z"),
                    (5, "2026-09-11T09:00:00Z"),
                    next_page="more",
                )
            raise AssertionError("a page holding an older discussion is the last read")
        return 200, discussions[variables["number"]]

    gh = ScriptedGh(
        WHOAMI,
        (
            "GET",
            rf"repos/{REPO}/issues/comments\?sort=updated&direction=asc&since={since}&per_page=100&page=1",
            (200, [], date),
        ),
        (
            "GET",
            rf"repos/{REPO}/issues\?state=all&sort=updated&direction=asc&since={since}&per_page=100&page=1",
            (200, []),
        ),
        (
            "GET",
            rf"repos/{REPO}/pulls/comments\?sort=updated&direction=asc&since={since}&per_page=100&page=1",
            (200, [_review(61, created="2026-09-11T08:30:00Z")]),
        ),
        ("POST", "graphql", graphql),
    )
    cursors = {f"github:{REPO}": since}
    events = list(
        correspond.listen(
            f"github:{REPO}", cursors=cursors, registry={"github": GitHub(run=gh)}
        )
    )
    assert [(e.kind, e.message.id) for e in events] == [
        ("message.created", "discussion_r61"),
        ("message.created", "discussioncomment-501"),
        ("message.created", "discussion-6"),
    ]
    assert events[0].message.conversation.encoded == f"github:{REPO}#3"
    assert events[1].message.conversation.kind == "discussion"
    assert json.loads(cursors[f"github:{REPO}"])["t"] == "2026-09-11T11:59:55Z"


def test_discussion_activity_reads_further_pages_while_every_entry_is_active():
    def graphql(payload):
        if payload["variables"]["after"] is None:
            return 200, _activity((6, "2026-09-11T10:00:00Z"), next_page="p2")
        return 200, _activity((5, "2026-09-11T09:00:00Z"), (4, "2026-09-01T00:00:00Z"))

    gh = ScriptedGh(("POST", "graphql", graphql))
    since = correspond.model.parse_time("2026-09-11T08:00:00Z")
    assert GitHub(run=gh)._active_discussions("octocat", "hello-world", since) == [6, 5]


def _categories(enabled=True):
    return {
        "data": {
            "repository": {
                "id": "R_node1",
                "hasDiscussionsEnabled": enabled,
                "discussionCategories": {
                    "nodes": [
                        {"id": "DIC_ideas", "name": "Ideas", "slug": "ideas"},
                        {"id": "DIC_qa", "name": "Q&A", "slug": "q-a"},
                    ]
                },
            }
        }
    }


def _creating(categories):
    def graphql(payload):
        if "createDiscussion" in payload["query"]:
            return 200, {
                "data": {
                    "createDiscussion": {
                        "discussion": {
                            "number": 12,
                            "url": f"https://github.com/{REPO}/discussions/12",
                            "category": {"name": "Q&A"},
                        }
                    }
                }
            }
        return 200, categories

    return ScriptedGh(WHOAMI, ("POST", "graphql", graphql))


def test_a_category_opens_a_discussion_instead_of_an_issue():
    gh = _creating(_categories())
    result = correspond.send(
        f"github:{REPO}",
        Draft(title="How do I export?", text="Asking here", native={"category": "Q-A"}),
        registry={"github": GitHub(run=gh)},
    )
    assert result.ok and result.conversation == f"github:{REPO}#12"
    assert result.message_id == "discussion-12" and result.url.endswith("/discussions/12")
    assert result.plan["landed"] == {"category": "Q&A"}
    mutation = next(
        c["payload"]
        for c in gh.calls
        if c["payload"] and "createDiscussion" in c["payload"]["query"]
    )
    assert mutation["variables"] == {
        "repositoryId": "R_node1",
        "categoryId": "DIC_qa",
        "title": "How do I export?",
        "body": "Asking here",
    }
    assert not any(path.endswith("/issues") for path in gh.paths("POST"))


def test_an_unknown_category_or_disabled_discussions_create_nothing():
    unknown = correspond.send(
        f"github:{REPO}",
        Draft(title="T", text="x", native={"category": "General"}),
        registry={"github": GitHub(run=(gh := _creating(_categories())))},
    )
    assert (unknown.ok, unknown.error_kind) == (False, "validation")
    assert "Ideas" in unknown.error and "Q&A" in unknown.error
    assert not any(
        "createDiscussion" in (c["payload"] or {}).get("query", "") for c in gh.calls
    )
    with pytest.raises(NotSupported, match="enable discussions"):
        correspond.send(
            f"github:{REPO}",
            Draft(title="T", text="x", native={"category": "Ideas"}),
            registry={"github": GitHub(run=_creating(_categories(enabled=False)))},
        )


@pytest.mark.parametrize(
    "native",
    [{"category": "Ideas", "labels": ["question"]}, {"category": ""}, {"category": 3}],
)
def test_a_malformed_discussion_draft_is_refused_with_no_graphql_call(native):
    gh = _creating(_categories())
    result = correspond.send(
        f"github:{REPO}",
        Draft(title="T", text="x", native=native),
        registry={"github": GitHub(run=gh)},
    )
    assert (result.ok, result.error_kind) == (False, "validation")
    assert "graphql" not in gh.paths("POST")


def test_a_dry_run_discussion_plans_the_category_and_creates_nothing():
    gh = ScriptedGh(WHOAMI, ("GET", rf"repos/{REPO}$", (404, {"message": "Not Found"})))
    result = correspond.send(
        f"github:{REPO}",
        Draft(title="T", text="x", native={"category": "Ideas"}),
        dry_run=True,
        registry={"github": GitHub(run=gh)},
    )
    assert result.ok and result.plan["action"] == "open a discussion"
    assert result.plan["category"] == "Ideas"
    assert "POST" not in {c["method"] for c in gh.calls}


def test_opening_a_discussion_still_needs_a_title():
    gh = _creating(_categories())
    result = correspond.send(
        f"github:{REPO}",
        Draft(text="x", native={"category": "Ideas"}),
        registry={"github": GitHub(run=gh)},
    )
    assert (result.ok, result.error_kind) == (False, "validation")


def _repository_poll(since, date, graphql, *, comments=()):
    return ScriptedGh(
        WHOAMI,
        (
            "GET",
            rf"repos/{REPO}/issues/comments\?sort=updated&direction=asc&since={since}&per_page=100&page=1",
            (200, list(comments), {"Date": date}),
        ),
        (
            "GET",
            rf"repos/{REPO}/issues\?state=all&sort=updated&direction=asc&since={since}&per_page=100&page=1",
            (200, [], {"Date": date}),
        ),
        (
            "GET",
            rf"repos/{REPO}/pulls/comments\?sort=updated&direction=asc&since={since}&per_page=100&page=1",
            (200, [], {"Date": date}),
        ),
        ("POST", "graphql", graphql),
    )


def test_a_discussion_post_newer_than_the_lists_waits_for_the_next_poll():
    """Read after the REST lists, a discussion must not move the cursor past what they could hold."""
    since = "2026-09-11T08:00:00Z"
    late = _numbered_discussion(
        5,
        "2026-09-01T00:00:00Z",
        [_post(501, created="2026-09-11T12:00:30Z", replies=[])],
    )

    def graphql(payload):
        if "discussions(" in payload["query"]:
            return 200, _activity((5, "2026-09-11T12:00:30Z"))
        return 200, late

    gh = _repository_poll(since, "Fri, 11 Sep 2026 12:00:00 GMT", graphql)
    cursors = {f"github:{REPO}": since}
    events = list(
        correspond.listen(
            f"github:{REPO}", cursors=cursors, registry={"github": GitHub(run=gh)}
        )
    )
    assert events == []
    assert json.loads(cursors[f"github:{REPO}"])["t"] == "2026-09-11T11:59:55Z"


def test_a_token_that_may_not_read_discussions_still_listens_to_the_rest(caplog):
    forbidden = {"errors": [{"type": "FORBIDDEN", "message": "Resource not accessible"}]}
    gh = _repository_poll(
        "2026-09-11T08:00:00Z",
        "Fri, 11 Sep 2026 12:00:00 GMT",
        (200, forbidden),
        comments=[_comment(21, created="2026-09-11T09:00:00Z")],
    )
    events = list(
        correspond.listen(
            f"github:{REPO}",
            cursors={f"github:{REPO}": "2026-09-11T08:00:00Z"},
            registry={"github": GitHub(run=gh)},
        )
    )
    assert [e.message.id for e in events] == ["issuecomment-21"]
    assert "discussions skipped" in caplog.text


def test_a_discussion_deleted_between_the_walk_and_the_read_is_skipped():
    def graphql(payload):
        if "discussions(" in payload["query"]:
            return 200, _activity((5, "2026-09-11T09:00:00Z"))
        return 200, {"data": {"repository": {"discussion": None}}}

    gh = _repository_poll(
        "2026-09-11T08:00:00Z", "Fri, 11 Sep 2026 12:00:00 GMT", graphql
    )
    events = list(
        correspond.listen(
            f"github:{REPO}",
            cursors={f"github:{REPO}": "2026-09-11T08:00:00Z"},
            registry={"github": GitHub(run=gh)},
        )
    )
    assert events == []


def test_listen_asks_for_a_comments_latest_replies():
    from correspond.channels.github import DISCUSSION_QUERY

    assert "replies(last:100)" in DISCUSSION_QUERY


def test_listen_is_declared_partial_with_why():
    caps = GitHub().capabilities
    assert caps.listen is Support.PARTIAL
    assert any("listen is partial on a repository's discussions" in n for n in caps.notes)
