"""Every surface comes from the one tool list; the core imports no surface library; failures are results; every write can dry-run."""

import asyncio
import inspect
import json
import os
import subprocess
import sys

import pytest

from correspond import tools
from correspond.mcp import refs

TOOL_NAMES = [
    "channels",
    "requirements",
    "capabilities",
    "ref",
    "read",
    "listen",
    "audience",
    "send",
    "edit",
    "react",
]
WRITES = {"send", "edit", "react"}
SURFACE_LIBS = {
    "argh",
    "cw",
    "click",
    "typer",
    "fastapi",
    "starlette",
    "uvicorn",
    "flask",
    "mcp",
    "fastmcp",
    "qh",
    "uf",
    "py2mcp",
}
EFFECTS = {"read", "external-read", "external"}


def _cli(args, *, module="correspond.testing", stdin=None):
    return subprocess.run(
        [sys.executable, "-m", module, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        input=stdin,
    )


def test_importing_the_core_pulls_in_no_surface_library():
    code = "import sys, json, correspond; print(json.dumps(sorted({m.split('.')[0] for m in sys.modules})))"
    loaded = json.loads(
        subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True
        ).stdout
    )
    assert not set(loaded) & SURFACE_LIBS
    assert not set(loaded) & {"xdol", "dol"}, (
        "the registry and the stores import these on first use"
    )


def test_the_tool_list_is_complete_and_classified():
    names = [t.__name__ for t in tools.TOOLS]
    assert names == TOOL_NAMES
    assert (
        set(tools.SIDE_EFFECTS) == set(names)
        and set(tools.SIDE_EFFECTS.values()) <= EFFECTS
    )
    assert {
        name for name, effect in tools.SIDE_EFFECTS.items() if effect == "external"
    } == WRITES


def test_tools_take_flat_serialisable_arguments_and_every_write_can_dry_run():
    allowed = {"str", "str | None", "bool", "int | None"}
    for tool in tools.TOOLS:
        assert (tool.__doc__ or "").strip(), (
            f"{tool.__name__} needs a docstring: it is the CLI help and the MCP description"
        )
        parameters = inspect.signature(tool).parameters
        for parameter in parameters.values():
            assert parameter.kind in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            ), (tool.__name__, parameter.name)
            assert parameter.annotation in allowed, (
                tool.__name__,
                parameter.name,
                parameter.annotation,
            )
        if tool.__name__ in WRITES:
            dry_run = parameters["dry_run"]
            assert (
                dry_run.default is False
                and dry_run.kind is inspect.Parameter.KEYWORD_ONLY
            )


def test_results_are_json_ready_and_failures_are_results_with_a_kind(fake):
    successes = [
        tools.channels(),
        tools.capabilities("github"),
        tools.ref("github:OctoCat/Hello-World#1"),
        tools.read("fake:example/demo"),
        tools.send("fake:example/demo", "hello", dry_run=True),
    ]
    failures = {
        "not_supported": tools.react("fake:example/demo", "m1", "eyes"),
        "unknown_channel": tools.read("nowhere:x"),
        "invalid_ref": tools.ref("nonsense"),
        "validation": tools.send("fake:example/demo", "hi", priority="whenever"),
        "not_found": tools.read("fake:missing/conversation"),
    }
    for result in successes:
        assert result["ok"] and result["summary"], result
        json.dumps(result)
    for kind, result in failures.items():
        assert (result["ok"], result["error_kind"]) == (False, kind), result
        json.dumps(result)
    assert failures["not_supported"]["operation"] == "react"
    assert (
        successes[2]["value"] == "github:octocat/hello-world#1"
        and successes[2]["round_trip"]
    )
    assert fake.sent == [], "a dry run sends nothing"
    listed = {row["name"]: row["status"] for row in successes[0]["channels"]}
    assert listed["fake"] == "available" and listed["discord"] == "planned"
    for report in (tools.requirements("telegram"), tools.requirements("discord")):
        assert report["ok"] is False and report["problems"]
        json.dumps(report)


def test_the_cli_offers_every_tool_with_help():
    help_text = _cli(["--help"], module="correspond").stdout
    for name in TOOL_NAMES:
        assert name in help_text
    send_help = _cli(["send", "--help"], module="correspond").stdout
    assert "--dry-run" in send_help and "contact nothing" in send_help


def test_cli_exit_codes_json_output_and_stdin():
    refused = _cli(["react", "fake:example/demo", "m1", "eyes"])
    assert refused.returncode == 1 and "does not support react" in refused.stderr
    assert refused.stderr.count("instead") == 1
    as_json = json.loads(
        _cli(["read", "fake:example/demo", "--limit", "1", "--json"]).stdout
    )
    assert as_json["count"] == 1 and as_json["messages"][0]["id"] == "m2"
    piped = json.loads(
        _cli(
            ["send", "fake:example/demo", "-", "--dry-run", "--json"], stdin="from stdin"
        ).stdout
    )
    assert piped["dry_run"] and piped["plan"]["text"] == "from stdin"
    unknown = _cli(["read", "nowhere:x"], module="correspond")
    assert unknown.returncode == 1 and "unknown channel" in unknown.stderr


def test_mcp_exposes_reads_by_default_and_writes_only_when_allowed():
    default = [ref.split(":", 1)[1] for ref in refs()]
    assert default == [
        name for name in TOOL_NAMES if tools.SIDE_EFFECTS[name] != "external"
    ]
    assert [ref.split(":", 1)[1] for ref in refs(allow_send=True)] == TOOL_NAMES


def test_mcp_server_registers_those_tools_without_data_dir():
    pytest.importorskip("py2mcp")
    from correspond.mcp import mk_server

    for allow_send in (False, True):
        registered = asyncio.run(mk_server(allow_send=allow_send).list_tools())
        assert sorted(t.name for t in registered) == sorted(
            r.split(":", 1)[1] for r in refs(allow_send=allow_send)
        )
        for tool in registered:
            assert "data_dir" not in (tool.parameters or {}).get("properties", {}), (
                tool.name
            )


def test_the_mcp_command_refuses_unknown_arguments():
    from correspond.mcp import main

    with pytest.raises(SystemExit, match="--allow-send"):
        main(["--send-everything"])
