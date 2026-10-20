"""Slack's clouds: the Web API base a workspace lives behind, and the hosts that go with it.

Commercial Slack answers at ``https://slack.com/api`` and serves Socket Mode and files from
``*.slack.com``; GovSlack answers at ``https://slack-gov.com/api`` and serves them from
``*.slack-gov.com``. The Slack adapter's ``api_base`` setting picks one of these bases (no
other: the token is sent to it). Every host the token or a Socket Mode connection may then
reach is derived from that base, so a commercial workspace's answer can never point the
token or the listener at GovSlack's hosts, nor the other way round.

>>> api_base("https://slack-gov.com/api/")
'https://slack-gov.com/api'
>>> on_slack_host("files.slack.com", API_BASE), on_slack_host("files.slack-gov.com", API_BASE)
(True, False)
>>> on_slack_host("files.slack-gov.com", GOV_API_BASE), on_slack_host("files.slack.com", GOV_API_BASE)
(True, False)
"""

from __future__ import annotations

import re

from correspond.errors import ChannelError

__all__ = [
    "API_BASE",
    "API_BASES",
    "GOV_API_BASE",
    "api_base",
    "on_slack_host",
]

#: Commercial Slack's Web API (the default).
API_BASE = "https://slack.com/api"
#: GovSlack's Web API.
GOV_API_BASE = "https://slack-gov.com/api"
#: Each Web API base and the domain its other hosts (Socket Mode, files) are under.
API_BASES = {API_BASE: "slack.com", GOV_API_BASE: "slack-gov.com"}
#: One DNS label.
_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def api_base(configured: str | None) -> str:
    """The Web API base a setting names (``None`` or empty: commercial Slack's), or a ``validation`` error.

    >>> api_base(None), api_base(" https://slack.com/api ")
    ('https://slack.com/api', 'https://slack.com/api')
    >>> api_base("https://example.org/api")
    Traceback (most recent call last):
    ...
    correspond.errors.ChannelError: slack api_base is one of ('https://slack.com/api', 'https://slack-gov.com/api'), not 'https://example.org/api'
    """
    base = (configured or "").strip().rstrip("/") or API_BASE
    if base not in API_BASES:
        raise ChannelError(
            f"slack api_base is one of {tuple(API_BASES)}, not {configured!r}",
            kind="validation",
        )
    return base


def on_slack_host(host: str | None, base: str = API_BASE) -> bool:
    """Whether ``host`` is the domain of ``base``'s cloud, or one of its subdomains.

    >>> on_slack_host("wss-primary.slack.com"), on_slack_host("slack.com.example.org")
    (True, False)
    >>> on_slack_host("evilslack.com"), on_slack_host("")
    (False, False)
    """
    domain = API_BASES[api_base(base)]
    name = (host or "").lower()
    if name == domain:
        return True
    prefix, dot, rest = name.rpartition("." + domain)
    if not dot or rest or not prefix:
        return False
    return all(_LABEL_RE.match(label) for label in prefix.split("."))
