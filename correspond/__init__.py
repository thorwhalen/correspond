"""correspond: a channel facade for AI agents.

Read, listen, write and identify the sender over GitHub, email, push notifications,
Telegram and a web inbox, through one model and one set of verbs::

    >>> import correspond                                                  # doctest: +SKIP
    >>> messages = correspond.read("github:octocat/hello-world#1")         # doctest: +SKIP
    >>> messages[0].author.handle, messages[0].authenticity.grade.value    # doctest: +SKIP
    ('octocat', 'platform')
    >>> correspond.send("ntfy:", "backup finished", dry_run=True).plan     # doctest: +SKIP

A conversation reference is ``<channel>:<id>``. Each channel implements the operations it
can (``read``, ``listen``, ``send``, ``edit``, ``react``, ``upload``, ``verify``,
``audience``); asking for one it lacks raises :class:`NotSupported`, and
:func:`capabilities` says so in advance. :func:`audience` is the exception: it never
refuses, because an audience nobody can compute is public.
correspond knows no people: a message's ``author`` is what the platform attests, and its
``authenticity`` is how sure the channel is.

The command line (``correspond read github:octocat/hello-world#1``) and the MCP server use
the same verbs, through :mod:`correspond.tools`.
"""

from correspond.errors import (
    ChannelError,
    CorrespondError,
    InvalidRef,
    MissingRequirement,
    NotSupported,
    UnknownChannel,
)
from correspond.model import (
    Account,
    Attachment,
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
)
from correspond.ops import (
    AudienceReader,
    Editor,
    Listener,
    Reactor,
    Reader,
    Uploader,
    Verifier,
    Writer,
    audience,
    capabilities,
    edit,
    get_channel,
    listen,
    parse_ref,
    react,
    read,
    send,
    upload,
    verify,
)
from correspond.registry import check_requirements, register_channel, unregister_channel
from correspond.registry import channels as channel_registry
from correspond.routing import RouteDecision, check_binding, metadata_rule, route

__all__ = [
    "Account",
    "Attachment",
    "Audience",
    "AudienceReader",
    "Authenticity",
    "Capabilities",
    "ChannelError",
    "ChannelIdentity",
    "ConversationRef",
    "CorrespondError",
    "Draft",
    "Editor",
    "Event",
    "Grade",
    "HistoryDepth",
    "InvalidRef",
    "Listener",
    "Message",
    "MissingRequirement",
    "NotSupported",
    "Reactor",
    "Reader",
    "RouteDecision",
    "Scope",
    "SendResult",
    "Support",
    "UnknownChannel",
    "Uploader",
    "Verifier",
    "Writer",
    "audience",
    "capabilities",
    "channel_registry",
    "check_binding",
    "check_requirements",
    "edit",
    "get_channel",
    "listen",
    "metadata_rule",
    "parse_ref",
    "react",
    "read",
    "register_channel",
    "route",
    "send",
    "unregister_channel",
    "upload",
    "verify",
]
