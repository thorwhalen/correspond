"""An in-memory channel for tests and rehearsals, and ``python -m correspond.testing``: the CLI with it registered.

:class:`FakeChannel` reads the conversations it was seeded with and records what it is
asked to send. It implements ``Reader`` and ``Writer`` only, so every other operation
raises ``NotSupported``, exactly as a real channel without that operation does. Nothing it
does leaves the process, which makes it the channel to rehearse a workflow on::

    python -m correspond.testing read fake:example/demo
    python -m correspond.testing send fake:example/demo "hello" --dry-run

>>> fake = demo_channel()
>>> [m.text for m in fake.read(fake.parse_ref("example/demo"))]
['The export drops the last row.', 'Seen it too, on the CSV export.']
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone

from correspond.errors import ChannelError, InvalidRef
from correspond.model import (
    Account,
    Authenticity,
    Capabilities,
    ChannelIdentity,
    ConversationRef,
    Draft,
    Grade,
    HistoryDepth,
    Message,
    SendResult,
    Support,
)

__all__ = ["DEMO_CONVERSATION", "FakeChannel", "demo_channel", "demo_message", "main"]

DEMO_CONVERSATION = "example/demo"
_ID_RE = re.compile(r"^[A-Za-z0-9._/#-]+$")
_EPOCH = datetime(2026, 9, 11, 9, 0, tzinfo=timezone.utc)


class FakeChannel:
    """A channel held in memory: seeded conversations to read, and a record of sends (``sent``)."""

    def __init__(
        self,
        name: str = "fake",
        *,
        conversations: Mapping[str, Iterable[Message]] | None = None,
    ):
        self.name = name
        self.conversations: dict[str, list[Message]] = {
            key: list(messages) for key, messages in (conversations or {}).items()
        }
        self.sent: list[tuple[ConversationRef, Draft]] = []

    @property
    def capabilities(self) -> Capabilities:
        """Read and send, with a small text limit; nothing else."""
        return Capabilities(
            channel=self.name,
            read=Support.FULL,
            send=Support.FULL,
            initiate=Support.FULL,
            reply=Support.FULL,
            history_depth=HistoryDepth.FULL,
            grades=(Grade.PLATFORM, Grade.CLAIMED),
            max_text_length=10_000,
            notes=("in memory: nothing leaves the process",),
        )

    def parse_ref(self, id: str) -> ConversationRef:
        """Any non-empty id of letters, digits and ``._/#-``."""
        if not _ID_RE.match(id or ""):
            raise InvalidRef(
                f"{self.name} conversation ids are like {DEMO_CONVERSATION}, not {id!r}"
            )
        return ConversationRef(channel=self.name, id=id, kind="thread")

    def read(self, ref: ConversationRef, *, since=None, limit=None) -> list[Message]:
        """The seeded (and sent) messages of a conversation."""
        if ref.id not in self.conversations:
            raise ChannelError(f"{ref} does not exist", kind="not_found")
        messages = [
            m for m in self.conversations[ref.id] if since is None or m.sent_at >= since
        ]
        return messages[-limit:] if limit else messages

    def send(
        self, ref: ConversationRef, draft: Draft, *, dry_run: bool = False
    ) -> SendResult:
        """Append to the conversation (or, with ``dry_run``, say that it would)."""
        plan = {
            "action": "append a message to the in-memory conversation",
            "conversation": ref.encoded,
            "text": draft.text,
            "reply_to": draft.reply_to,
        }
        account = Account(channel=self.name, id="fake-bot", acts_as="bot")
        if dry_run:
            return SendResult(
                ok=True,
                channel=self.name,
                conversation=ref.encoded,
                dry_run=True,
                plan=plan,
                account=account,
            )
        thread = self.conversations.setdefault(ref.id, [])
        message = demo_message(
            conversation=ref.encoded,
            text=draft.text,
            message_id=f"m{len(thread) + 1}",
            handle="fake-bot",
            is_self=True,
            reply_to=draft.reply_to,
            sent_at=_EPOCH + timedelta(minutes=len(thread)),
        )
        thread.append(message)
        self.sent.append((ref, draft))
        return SendResult(
            ok=True,
            channel=self.name,
            conversation=ref.encoded,
            message_id=message.id,
            account=account,
            plan=plan,
        )


def demo_message(
    *,
    conversation: str = f"fake:{DEMO_CONVERSATION}",
    text: str = "The export drops the last row.",
    message_id: str = "m1",
    handle: str | None = "ada",
    grade: Grade = Grade.PLATFORM,
    labels: Iterable[str] = (),
    is_self: bool = False,
    reply_to: str | None = None,
    sent_at: datetime = _EPOCH,
) -> Message:
    """A fictional message, for examples and tests."""
    ref = ConversationRef.parse(conversation)
    return Message(
        id=message_id,
        conversation=ref,
        author=ChannelIdentity(
            channel=ref.channel,
            native_id=handle or "",
            handle=handle,
            display_name=handle.title() if handle else None,
            is_self=is_self,
        ),
        authenticity=Authenticity(grade=grade, evidence={"source": "correspond.testing"}),
        sent_at=sent_at,
        text=text,
        reply_to=reply_to,
        native={"labels": list(labels)} if labels else {},
    )


def demo_channel(name: str = "fake") -> FakeChannel:
    """A :class:`FakeChannel` seeded with one short conversation, ``example/demo``."""
    return FakeChannel(
        name,
        conversations={
            DEMO_CONVERSATION: [
                demo_message(conversation=f"{name}:{DEMO_CONVERSATION}"),
                demo_message(
                    conversation=f"{name}:{DEMO_CONVERSATION}",
                    text="Seen it too, on the CSV export.",
                    message_id="m2",
                    handle=None,
                    grade=Grade.CLAIMED,
                    reply_to="m1",
                    sent_at=_EPOCH + timedelta(minutes=5),
                ),
            ]
        },
    )


def main(argv: list[str] | None = None) -> None:
    """The ordinary ``correspond`` CLI, with the demo fake channel registered as ``fake``."""
    from correspond.__main__ import main as cli
    from correspond.registry import register_channel

    register_channel(demo_channel(), replace=True)
    cli(argv)


if __name__ == "__main__":
    main()
