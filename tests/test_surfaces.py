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
    "upload",
    "label",
    "unlabel",
    "close",
    "reopen",
    "webhook_info",
    "set_webhook",
    "delete_webhook",
    "authored",
    "authored_thread",
]
WRITES = {
    "send",
    "edit",
    "react",
    "upload",
    "label",
    "unlabel",
    "close",
    "reopen",
    "set_webhook",
    "delete_webhook",
}
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
    assert listed["fake"] == "available" and listed["signal"] == "planned"
    for report in (tools.requirements("telegram"), tools.requirements("discord")):
        assert report["ok"] is False and report["problems"]
        json.dumps(report)


def test_the_cli_offers_every_tool_with_help():
    help_text = _cli(["--help"], module="correspond").stdout
    for name in TOOL_NAMES:  # the CLI spells an underscore as a hyphen
        assert name in help_text or name.replace("_", "-") in help_text
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


def test_send_takes_native_as_a_json_object_and_refuses_what_the_channel_does_not_take(
    fake,
):
    refused = tools.send(
        "fake:example/demo", "hi", native='{"labels": ["x"]}', dry_run=True
    )
    assert refused["ok"] is False and refused["error_kind"] == "validation"
    assert "labels" in refused["error"]
    for bad in ("not json", '["a list"]'):
        result = tools.send("fake:example/demo", "hi", native=bad, dry_run=True)
        assert result["ok"] is False and result["error_kind"] == "validation"
    assert tools.send("fake:example/demo", "hi", native="", dry_run=True)["ok"] is True


def test_the_upload_tool_reads_a_local_file_and_dry_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("CORRESPOND_EMAIL_USER", "me@example.org")
    path = tmp_path / "notes.txt"
    path.write_bytes(b"hello")
    result = tools.upload("email:ada@example.org", str(path), dry_run=True)
    assert result["ok"] and result["dry_run"], result
    attachment = result["plan"]["attachment"]
    assert attachment["name"] == "notes.txt" and attachment["media_type"] == "text/plain"
    renamed = tools.upload("email:ada@example.org", str(path), name="n.md", dry_run=True)
    assert renamed["plan"]["attachment"]["name"] == "n.md"
    missing = tools.upload("email:ada@example.org", str(tmp_path / "nope"), dry_run=True)
    assert (missing["ok"], missing["error_kind"]) == (False, "not_found")


def test_the_upload_tool_refuses_what_is_not_a_plain_visible_file(tmp_path, monkeypatch):
    import os

    monkeypatch.setenv("CORRESPOND_EMAIL_USER", "me@example.org")
    secret = tmp_path / ".hidden" / "key"
    secret.parent.mkdir()
    secret.write_bytes(b"k")
    hidden = tools.upload("email:ada@example.org", str(secret), dry_run=True)
    assert (hidden["ok"], hidden["error_kind"]) == (False, "permission")
    allowed = tools.upload(
        "email:ada@example.org", str(secret), include_hidden=True, dry_run=True
    )
    assert allowed["ok"] and allowed["plan"]["source"] == os.path.realpath(secret)
    folder = tools.upload("email:ada@example.org", str(tmp_path), dry_run=True)
    assert (folder["ok"], folder["error_kind"]) == (False, "validation")
    if hasattr(os, "mkfifo"):  # not on Windows
        fifo = tmp_path / "pipe"
        os.mkfifo(fifo)
        piped = tools.upload("email:ada@example.org", str(fifo), dry_run=True)
        assert (piped["ok"], piped["error_kind"]) == (False, "validation"), "never opened"


def test_the_upload_plan_names_the_real_source_and_its_real_type(tmp_path, monkeypatch):
    monkeypatch.setenv("CORRESPOND_EMAIL_USER", "me@example.org")
    path = tmp_path / "notes.txt"
    path.write_bytes(b"hello")
    disguised = tools.upload(
        "email:ada@example.org", str(path), name="holiday.jpg", dry_run=True
    )
    assert disguised["plan"]["source"].endswith("notes.txt")
    assert disguised["plan"]["attachment"]["media_type"] == "text/plain"


def test_the_upload_tool_checks_support_before_reading_and_reads_no_more_than_the_limit(
    tmp_path, monkeypatch
):
    from correspond.channels import mail

    path = tmp_path / "big.bin"
    path.write_bytes(b"x" * 64)
    unsupported = tools.upload("ntfy:example-topic", str(path), dry_run=True)
    assert (unsupported["ok"], unsupported["error_kind"]) == (False, "not_supported")
    monkeypatch.setattr(mail, "MAX_ATTACHMENT_BYTES", 10)
    monkeypatch.setenv("CORRESPOND_EMAIL_USER", "me@example.org")
    too_big = tools.upload("email:ada@example.org", str(path), dry_run=True)
    assert (too_big["ok"], too_big["error_kind"]) == (False, "validation")
