"""A dry run sends nothing: no network, no subprocess, no Keychain, no store, on every write of every built-in channel.

The one read a dry run attempts is the audience lookup of the ``before_send`` check; here it is
refused like everything else, so the plan shows the audience as public (defaulted).
"""

import shutil
import subprocess
import sys

import pytest

import correspond
from correspond import settings
from correspond.channels.github import GitHub
from correspond.channels.macos import MacOS
from correspond.channels.mail import Email
from correspond.channels.ntfy import Ntfy
from correspond.channels.telegram import Telegram


def _refuse(*args, **kwargs):
    raise AssertionError(f"a dry run tried to reach something: {args!r}")


class Untouchable(dict):
    """A store that fails the test if anything reads or writes it."""

    def __getitem__(self, key):
        _refuse(key)

    def __setitem__(self, key, value):
        _refuse(key)

    def __iter__(self):
        _refuse("iterate")


@pytest.fixture
def nothing_reachable(monkeypatch):
    """Every way out fails loudly: subprocesses, the Keychain; and it looks like a Mac with every tool installed."""
    monkeypatch.setattr(subprocess, "run", _refuse)
    monkeypatch.setattr(settings, "keychain_get", _refuse)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")


def test_every_write_of_every_built_in_channel_dry_runs_without_reaching_anything(
    nothing_reachable,
):
    registry = {
        "github": GitHub(run=_refuse),
        "email": Email(imap=_refuse, smtp=_refuse, run=_refuse),
        "ntfy": Ntfy(http=_refuse, run=_refuse),
        "macos": MacOS(run=_refuse),
        "telegram": Telegram(http=_refuse, log=Untouchable(), run=_refuse),
    }
    writes = [
        lambda: correspond.send(
            "github:octocat/hello-world#1", "hi", dry_run=True, registry=registry
        ),
        lambda: correspond.send(
            "github:octocat/hello-world",
            "hi",
            title="A title",
            dry_run=True,
            registry=registry,
        ),
        lambda: correspond.edit(
            "github:octocat/hello-world#1",
            "issuecomment-1",
            "hi",
            dry_run=True,
            registry=registry,
        ),
        lambda: correspond.react(
            "github:octocat/hello-world#5",
            "discussioncomment-1",
            "eyes",
            dry_run=True,
            registry=registry,
        ),
        lambda: correspond.send(
            "email:ada@example.org",
            "hi",
            title="A title",
            reply_to="<m1@example.org>",
            priority="high",
            dry_run=True,
            registry=registry,
        ),
        lambda: correspond.send(
            "ntfy:example-topic", "hi", priority="high", dry_run=True, registry=registry
        ),
        lambda: correspond.send("ntfy:", "hi", dry_run=True, registry=registry),
        lambda: correspond.send("macos:", "hi", dry_run=True, registry=registry),
        lambda: correspond.send(
            "telegram:-4001/7", "hi", reply_to="3", dry_run=True, registry=registry
        ),
        lambda: correspond.edit(
            "telegram:-4001", "3", "hi", dry_run=True, registry=registry
        ),
        lambda: correspond.react(
            "telegram:-4001", "3", "👍", dry_run=True, registry=registry
        ),
    ]
    for write in writes:
        result = write()
        assert result.ok and result.dry_run, result
        assert result.plan["audience"] and result.plan["before_send"], result
