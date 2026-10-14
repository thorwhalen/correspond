"""Make a draft's ``notify`` list a property of sending, not a rule a sender can forget.

A :class:`~correspond.model.Draft` may name who must be notified (``Draft.notify``).
Each writer then guarantees it in its platform's terms: a mention added to the text
when it is absent (GitHub, Telegram), an address in To or Cc (email). A channel whose
``capabilities.notify`` is ``none`` refuses such a draft with ``NotSupported("notify")``
before anything is planned, so a notification is never silently dropped.

These helpers are pure: the writers call them while building the plan, so the dry run
shows the exact text, or the exact Cc, that will go out.

>>> with_mentions("Fixed in #12.", ["octocat", "@hubot"])
'Fixed in #12.\\n\\ncc @octocat @hubot'
>>> with_mentions("Thanks @Octocat, fixed.", ["octocat"])
'Thanks @Octocat, fixed.'
"""

from __future__ import annotations

import re
from typing import Iterable

#: The line that carries the mentions a text lacked, before the handles.
DFLT_MENTION_LEAD = "cc"


def handle(name: str) -> str:
    """A handle without its leading ``@``.

    >>> handle("@octocat"), handle("octocat")
    ('octocat', 'octocat')
    """
    return name.strip().removeprefix("@")


def is_mentioned(text: str, name: str) -> bool:
    """Is ``@name`` in ``text`` as a whole mention (not inside an address or a longer handle)? Case-insensitive.

    >>> is_mentioned("ping @octocat.", "octocat")
    True
    >>> is_mentioned("mail octocat@example.org", "octocat")
    False
    >>> is_mentioned("ping @octocat-two", "octocat")
    False
    """
    pattern = rf"(?<![\w@/.-])@{re.escape(handle(name))}(?![\w-]|/\w)"
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def missing_mentions(text: str, names: Iterable[str]) -> tuple[str, ...]:
    """The handles in ``names`` that ``text`` does not mention yet, in order, each once."""
    seen: set[str] = set()
    missing = []
    for name in map(handle, names):
        if name and name.lower() not in seen and not is_mentioned(text, name):
            missing.append(name)
        seen.add(name.lower())
    return tuple(missing)


def with_mentions(
    text: str, names: Iterable[str], *, lead: str = DFLT_MENTION_LEAD
) -> str:
    """``text`` with a closing ``cc @a @b`` line naming every handle it did not mention."""
    missing = missing_mentions(text, names)
    if not missing:
        return text
    line = " ".join([lead, *(f"@{name}" for name in missing)]).strip()
    return f"{text.rstrip()}\n\n{line}"


def with_copies(
    to: str, cc: Iterable[str], names: Iterable[str]
) -> tuple[str, ...]:
    """``cc`` plus every address in ``names`` that is neither ``to`` nor already copied (case-insensitive).

    >>> with_copies("a@example.org", ["b@example.org"], ["A@example.org", "c@example.org"])
    ('b@example.org', 'c@example.org')
    """
    cc = tuple(cc)
    present = {to.strip().lower(), *(a.strip().lower() for a in cc)}
    added = []
    for name in names:
        address = name.strip()
        if address and address.lower() not in present:
            added.append(address)
            present.add(address.lower())
    return (*cc, *added)
