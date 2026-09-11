"""macOS Notification Centre: a banner on this Mac (send only).

Reference: ``macos:`` (this machine; it takes no id). A send uses ``terminal-notifier`` when
it is installed, because it keeps bodies with quotes intact, and ``osascript`` otherwise.
Either way the text travels as program arguments, never inside a script, so nothing in a
message can be run.

>>> MacOS().parse_ref("").encoded
'macos:'
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Callable
from typing import Any

from correspond.errors import ChannelError, InvalidRef, MissingRequirement
from correspond.model import (
    Account,
    Capabilities,
    ConversationRef,
    Draft,
    SendResult,
    Support,
)

__all__ = ["MacOS"]

NAME = "macos"
DEFAULT_TITLE = "correspond"
TIMEOUT_S = 30
#: osascript reads this from stdin; the body and title arrive as argv, never as script text.
SCRIPT = (
    "on run argv\n"
    "  display notification (item 1 of argv) with title (item 2 of argv)\n"
    "end run\n"
)


class MacOS:
    """Notification Centre on the Mac correspond runs on."""

    name = NAME

    def __init__(self, *, run: Callable[..., Any] = subprocess.run):
        self.run = run

    @property
    def capabilities(self) -> Capabilities:
        """Send only."""
        return Capabilities(
            channel=NAME,
            send=Support.FULL,
            initiate=Support.FULL,
            notes=(
                "send only, to the Mac correspond runs on",
                "banners from a background job may be held back by Focus or notification settings",
            ),
        )

    def parse_ref(self, id: str) -> ConversationRef:
        """Only ``macos:``."""
        if id:
            raise InvalidRef(f"macos takes no id: use macos:, not macos:{id}")
        return ConversationRef(channel=NAME, id="", kind="device")

    def send(
        self, ref: ConversationRef, draft: Draft, *, dry_run: bool = False
    ) -> SendResult:
        """Show a banner with the draft's title and text."""
        if sys.platform != "darwin":
            raise MissingRequirement(
                NAME,
                f"Notification Centre exists only on macOS (this is {sys.platform})",
                fix="send to another channel, such as ntfy:",
            )
        title = " ".join((draft.title or DEFAULT_TITLE).split())
        notifier = shutil.which("terminal-notifier")
        osascript = shutil.which("osascript")
        if notifier:
            argv, stdin, via = (
                [notifier, "-title", title, "-message", draft.text],
                None,
                "terminal-notifier",
            )
        elif osascript:
            argv, stdin, via = [osascript, "-", draft.text, title], SCRIPT, "osascript"
        else:
            raise MissingRequirement(
                NAME,
                "neither terminal-notifier nor osascript is on PATH",
                fix="brew install terminal-notifier",
            )
        plan = {"action": "show a banner", "via": via, "title": title, "text": draft.text}
        if dry_run:
            return SendResult(
                ok=True, channel=NAME, conversation=ref.encoded, dry_run=True, plan=plan
            )
        try:
            proc = self.run(
                argv, input=stdin, capture_output=True, text=True, timeout=TIMEOUT_S
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise ChannelError(f"{via} failed: {error}", kind="unavailable") from None
        if proc.returncode != 0:
            raise ChannelError(
                f"{via} exited with {proc.returncode}: {(proc.stderr or '').strip()[:200]}",
                kind="unavailable",
            )
        return SendResult(
            ok=True,
            channel=NAME,
            conversation=ref.encoded,
            account=Account(channel=NAME, id="local", acts_as="service"),
            plan=plan,
        )
