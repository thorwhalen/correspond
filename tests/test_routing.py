"""Routing: bindings, then thread continuity, then metadata rules, then the classifier; each decision says why."""

from correspond.model import ConversationRef, Grade
from correspond.routing import binding_matches, metadata_rule, route
from correspond.testing import demo_message


def _issue_comment(**kwargs):
    message = demo_message(conversation="github:example/app#12", **kwargs)
    parent = ConversationRef(channel="github", id="example/app", kind="repository")
    conversation = ConversationRef(
        channel="github", id="example/app#12", kind="issue", parent=parent
    )
    return type(message)(**{**message.__dict__, "conversation": conversation})


def test_a_binding_without_wildcards_matches_the_conversations_under_it():
    message = _issue_comment()
    for pattern in ("github:example/app", "github:example/app#12", "github:example/*"):
        assert binding_matches(pattern, message), pattern
    assert not binding_matches("github:example/application", message)
    # the prefix rule works without a parent too (a reference parsed without its adapter)
    assert binding_matches(
        "github:example/app", demo_message(conversation="github:example/app#12")
    )


def test_binding_conditions_look_at_native_fields_author_and_grade():
    message = _issue_comment(
        labels=["partner:ada", "bug"], grade=Grade.PLATFORM, handle="ada"
    )
    assert binding_matches("github:example/app?labels=partner:*", message)
    assert binding_matches(
        "github:example/app?labels=bug&author=ada&grade=platform", message
    )
    assert not binding_matches("github:example/app?labels=partner:*&grade=bound", message)
    assert not binding_matches("github:example/app?labels=wontfix", message)


def test_the_chain_runs_in_order_and_reports_the_deciding_rule():
    message = _issue_comment(labels=["partner:ada"], reply_to="m0")
    bindings = {
        "github:example/other": "subject:other",
        "github:example/app?labels=partner:*": "subject:app",
    }
    threads = {"m0": "case:7"}
    decision = route(message, bindings=bindings, threads=threads)
    assert (decision.target, decision.rule) == ("subject:app", "binding")
    assert "matched github:example/app#12" in decision.reason

    decision = route(message, bindings={"github:example/other": "x"}, threads=threads)
    assert (decision.target, decision.rule, decision.reason) == (
        "case:7",
        "thread",
        "reply to m0 belongs to case:7",
    )

    rule = metadata_rule("queue:partners", labels="partner:*")
    decision = route(message, threads={"elsewhere": "x"}, rules=[rule])
    assert (decision.target, decision.rule) == ("queue:partners", "metadata")
    assert "labels=partner:*" in decision.reason


def test_thread_continuity_checks_the_root_the_reply_and_the_conversation():
    message = _issue_comment()
    assert route(message, threads={"github:example/app#12": "case:3"}).reason.startswith(
        "conversation"
    )
    rooted = type(message)(**{**message.__dict__, "thread_root": "r1"})
    assert (
        route(rooted, threads={"r1": "case:9", "github:example/app#12": "case:3"}).target
        == "case:9"
    )


def test_the_classifier_runs_last_and_nothing_matched_means_unrouted():
    message = _issue_comment()
    seen = []

    def classifier(m):
        seen.append(m.id)
        return ("subject:app", "the text is about the export")

    decision = route(message, bindings=[("github:nope", "x")], classifier=classifier)
    assert (decision.rule, decision.reason) == (
        "classifier",
        "the text is about the export",
    ) and seen == ["m1"]
    assert route(message, classifier=lambda m: "subject:y").reason == "classifier"
    assert (
        route(
            message,
            bindings={"github:nope": "x"},
            rules=[lambda m: None],
            classifier=lambda m: None,
        )
        is None
    )
    assert (
        route(
            message, bindings={"github:example/app": "first"}, classifier=classifier
        ).rule
        == "binding"
    )
    assert seen == ["m1"], "the classifier is not asked when a rule decided"
