"""Turning a tool's result into terminal output: ``(stdout, stderr, exit code)``.

Used by the CLI only. The rules, in order: a result with a ``value`` prints just the value
(one line per item, so it pipes); otherwise its ``text``, else its ``summary``. A result
with ``ok: False`` exits 1 and puts its summary on stderr.

>>> render({"ok": True, "value": "github:octocat/hello-world#1", "summary": "…"})
('github:octocat/hello-world#1', '', 0)
>>> render({"ok": False, "summary": "ntfy does not support react"})
('', 'ntfy does not support react', 1)
"""

from __future__ import annotations

import json
from typing import Any

__all__ = ["render"]


def _scalar(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def render(result: Any) -> tuple[str, str, int]:
    """``(stdout, stderr, exit_code)`` for one tool result."""
    if not isinstance(result, dict):
        return ("" if result is None else str(result), "", 0)
    ok = bool(result.get("ok", True))
    warnings = [
        f"warning: {w}" for w in result.get("warnings") or [] if isinstance(w, str)
    ]
    if ok and "value" in result:
        found = result["value"]
        out = (
            "\n".join(_scalar(v) for v in found)
            if isinstance(found, list)
            else _scalar(found)
        )
    elif ok:
        out = (
            result.get("text")
            or result.get("summary")
            or json.dumps(result, indent=2, ensure_ascii=False)
        )
    else:
        summary = str(result.get("summary") or "failed")
        text = str(result.get("text") or "").rstrip("\n")
        out = "" if text in ("", summary) else text.removeprefix(summary).lstrip("\n")
        warnings = [summary, *warnings]
    return (out.rstrip("\n"), "\n".join(warnings), 0 if ok else 1)
