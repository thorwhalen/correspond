"""The shipped skill: spec-valid, bridged into .claude/, and every command and reference it (and the README) shows exists."""

import os
import re
import sys
from importlib.resources import files
from pathlib import Path

import pytest

from correspond.ops import parse_ref
from correspond.tools import TOOLS

REPO = Path(__file__).resolve().parent.parent
SKILLS = REPO / "correspond" / "data" / "skills"
DOCS = [SKILLS / "correspond" / "SKILL.md", REPO / "README.md"]
#: Top-level keys the Agent Skills specification allows.
SPEC_KEYS = {
    "name",
    "description",
    "license",
    "compatibility",
    "metadata",
    "allowed-tools",
}
COMMANDS = {t.__name__.replace("_", "-") for t in TOOLS}
CODE_RE = re.compile(r"^```[^\n]*\n(.*?)^```", re.M | re.S)


def _frontmatter(path):
    match = re.match(r"\A---\n(.*?)\n---\n(.*)\Z", path.read_text(encoding="utf-8"), re.S)
    assert match, f"{path} has no frontmatter"
    front, body = match.groups()
    keys = {
        line.split(":", 1)[0]
        for line in front.splitlines()
        if line and not line[0].isspace()
    }
    fields = dict(re.findall(r"^(name|description): (.+)$", front, re.M))
    audience = re.search(r"^\s+audience: (\w+)\s*$", front, re.M)
    return keys, fields, audience.group(1) if audience else None, body


def test_exactly_one_skill_ships_and_it_is_spec_valid():
    assert {p.name for p in SKILLS.iterdir() if p.is_dir()} == {"correspond"}
    keys, fields, audience, body = _frontmatter(SKILLS / "correspond" / "SKILL.md")
    assert keys <= SPEC_KEYS, f"non-spec keys: {keys - SPEC_KEYS}"
    assert fields["name"] == "correspond"
    assert 0 < len(fields["description"]) <= 1024 and ": " not in fields["description"]
    assert audience in {"users", "developers", "both"}
    assert len(body.splitlines()) < 500
    assert (
        files("correspond").joinpath("data", "skills", "correspond", "SKILL.md").is_file()
    ), "the skill is package data"


@pytest.mark.skipif(
    not (REPO / ".claude").is_dir(), reason="not a checkout (sdists leave .claude out)"
)
@pytest.mark.skipif(
    sys.platform == "win32",
    reason="git checks symlinks out as plain files on Windows by default",
)
def test_the_claude_code_bridge_is_a_relative_symlink():
    link = REPO / ".claude" / "skills" / "correspond"
    assert (
        link.is_symlink()
        and os.readlink(link) == "../../correspond/data/skills/correspond"
    )
    assert (link / "SKILL.md").is_file()


def test_the_docs_mention_only_commands_the_cli_has():
    for doc in DOCS:
        text = doc.read_text(encoding="utf-8")
        code = "\n".join(CODE_RE.findall(text))
        mentions = re.findall(r"(?:^\s*|\|\s*)correspond ([a-z][a-z-]*)", code, re.M)
        mentions += re.findall(r"`correspond ([a-z][a-z-]*)", text)
        assert mentions, doc.name
        for command in mentions:
            assert command in COMMANDS, f"{doc.name} mentions `correspond {command}`"


def test_every_reference_the_docs_show_parses():
    shown = []
    for doc in DOCS:
        text = doc.read_text(encoding="utf-8")
        code = "\n".join(CODE_RE.findall(text))
        shown += re.findall(
            r"correspond (?:read|listen|send|edit|react|ref) ([a-z]+:[^\s\"'`<>]*)(?![<\w])",
            code,
        )
        shown += re.findall(r"^\| `([a-z]+:[^`<>\s]*)`", text, re.M)
    assert len(shown) >= 6
    for ref in shown:
        assert parse_ref(ref).encoded, ref
