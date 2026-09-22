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


def test_bindings_are_checked_against_the_fields_a_channel_carries():
    from correspond.channels.github import GitHub
    from correspond.routing import check_binding
    from correspond.testing import demo_channel

    registry = {"fake": demo_channel(), "github": GitHub()}
    assert (
        check_binding(
            "fake:example/demo?labels=bug&grade=platform&author=ada", registry=registry
        )
        == []
    )
    [problem] = check_binding(
        "github:example/app-issues?label=partner:*", registry=registry
    )
    assert "label=partner:* never matches" in problem and "labels" in problem
    assert (
        check_binding("github:example/app-issues?labels=partner:*", registry=registry)
        == []
    )
    assert check_binding("*:example/demo?anything=x", registry=registry) == []
    assert "unknown channel" in check_binding("nowhere:x", registry=registry)[0]
    assert check_binding("no channel here", registry=registry)


def test_check_binding_flags_a_ref_not_in_the_channels_canonical_form():
    """#24: GitHub refs are lower-cased by parse_ref, but binding_matches compares
    the pattern literally -- a mis-cased binding looked valid and silently routed
    nothing. check_binding now catches it at load time instead."""
    from correspond.channels.github import GitHub
    from correspond.routing import binding_matches, check_binding
    from correspond.testing import demo_message

    registry = {"github": GitHub()}

    [problem] = check_binding("github:Example/App", registry=registry)
    assert "not in canonical form" in problem
    assert "github:example/app" in problem

    # And it is not merely a lint nit: the mis-cased pattern really does never match --
    # this is the silent-routing-failure check_binding exists to surface at load time.
    message = demo_message(conversation="github:example/app#12")
    assert binding_matches("github:Example/App", message) is None
    assert binding_matches("github:example/app", message) is not None

    # A wildcard ref is not checked for canonical form -- it cannot be re-parsed.
    assert check_binding("github:Example/*", registry=registry) == []

    # Already-canonical and unparseable refs raise no false positive.
    assert check_binding("github:example/app", registry=registry) == []
    assert check_binding("github:not a valid ref", registry=registry) == []


def test_conditions_keep_plus_signs_and_undeclared_fields_are_not_checked():
    from correspond.model import Capabilities, Support
    from correspond.routing import binding_matches, check_binding
    from correspond.testing import FakeChannel, demo_channel, demo_message

    assert binding_matches("fake:example/demo?labels=c++", demo_message(labels=["c++"]))
    assert binding_matches("fake:example/demo?labels=a%26b", demo_message(labels=["a&b"]))

    class External(FakeChannel):
        @property
        def capabilities(self):
            return Capabilities(channel=self.name, read=Support.FULL)

    registry = {"fake": demo_channel(), "ext": External("ext"), "bare": object()}
    assert check_binding("ext:board?state=open", registry=registry) == []
    assert check_binding("fake:example/demo?state=open", registry=registry) == []
    assert "'?' starts" in check_binding("gith?b:example/app", registry=registry)[0]
    assert "no capabilities" in check_binding("bare:x", registry=registry)[0]
