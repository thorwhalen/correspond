# PYTHON_ARGCOMPLETE_OK
"""``correspond`` on the command line: ``cw`` over :data:`correspond.tools.TOOLS`.

``--json`` anywhere prints the tool's result as JSON instead of text. ``-`` as the text of
``send`` or ``edit`` reads it from stdin. A result with ``ok: false`` exits with status 1.
"""

import dataclasses
import json
import sys

import cw

from correspond import tools
from correspond.render import render

_REF = {
    "help": "a conversation reference, <channel>:<id> (e.g. github:octocat/hello-world#1)"
}
_DRY_RUN = {
    "help": "show the plan, who can read it and the before_send verdict; contact nothing but the audience lookup, and change nothing"
}
_CC = {"help": "comma-separated addresses to copy (email)"}
_BCC = {
    "help": "comma-separated addresses to copy blind (email): they count in the audience"
}
#: Per-parameter help for the command line (the tools' docstrings are the command help).
HELP = {
    "requirements": {"channel": {"help": "a channel name, e.g. telegram"}},
    "capabilities": {"channel": {"help": "a channel name, e.g. github"}},
    "ref": {"ref": _REF},
    "read": {
        "ref": _REF,
        "since": {"help": "only messages sent or edited at or after this ISO 8601 time"},
        "limit": {"help": "keep the most recent N messages"},
    },
    "listen": {
        "ref": _REF,
        "limit": {"help": "at most N events"},
        "peek": {"help": "show new events without moving the cursor"},
        "data_dir": {"help": "where cursors are kept (default: the data root)"},
    },
    "audience": {"ref": _REF, "cc": _CC, "bcc": _BCC},
    "send": {
        "ref": _REF,
        "text": {"help": "the message, or - to read it from stdin"},
        "title": {"help": "a title; on github:owner/repo it opens an issue"},
        "reply_to": {"help": "the id of the message this answers"},
        "priority": {"help": "low, normal, high or urgent (channels with priorities)"},
        "cc": _CC,
        "bcc": _BCC,
        "dry_run": _DRY_RUN,
    },
    "edit": {
        "ref": _REF,
        "message_id": {"help": "the message id, as read shows it"},
        "text": {"help": "the new text, or - to read it from stdin"},
        "dry_run": _DRY_RUN,
    },
    "react": {
        "ref": _REF,
        "message_id": {"help": "the message id, as read shows it"},
        "reaction": {"help": "the reaction, e.g. +1 or eyes (see capabilities)"},
        "dry_run": _DRY_RUN,
    },
    "label": {
        "ref": _REF,
        "labels": {
            "help": "comma-separated labels to add (a label name may not itself contain a comma)"
        },
        "dry_run": _DRY_RUN,
    },
    "unlabel": {
        "ref": _REF,
        "labels": {
            "help": "comma-separated labels to remove (a label name may not itself contain a comma)"
        },
        "dry_run": _DRY_RUN,
    },
}


def _egress(as_json):
    def egress(result, *, out, err):
        if as_json:
            print(json.dumps(result, indent=2, ensure_ascii=False, default=str), file=out)
            return 0 if result.get("ok", True) else 1
        stdout, stderr, code = render(result)
        if stderr:
            print(stderr, file=err)
        if stdout:
            print(stdout, file=out)
        return code

    return egress


def main(argv=None):
    """Run one ``correspond`` command."""
    for stream in (
        sys.stdout,
        sys.stderr,
    ):  # a cp1252 pipe must not crash on a message's characters
        getattr(stream, "reconfigure", lambda **_: None)(errors="backslashreplace")
    argv = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in argv
    commands = {tool.__name__.replace("_", "-"): tool for tool in tools.TOOLS}
    config = {
        command: {param: dict(spec) for param, spec in params.items()}
        for command, params in HELP.items()
    }
    for command in ("send", "edit"):
        config[command]["text"]["codec"] = lambda text: (
            sys.stdin.read() if text == "-" else text
        )
    raise SystemExit(
        cw.dispatch(
            commands,
            [a for a in argv if a != "--json"],
            prog="correspond",
            convention=dataclasses.replace(cw.MODERN, default_in_help=False),
            egress=_egress(as_json),
            config=config,
        )
    )


if __name__ == "__main__":
    main()
