"""The one-command test: the whole path, end to end, on the CLI, fully offline, against a fake channel.

This is the definition of v0.1, and it must keep passing after every later change::

    python -m correspond.testing send fake:example/demo "hello" --dry-run \\
      && python -m correspond.testing read fake:example/demo \\
      && python -m correspond.testing ref fake:example/demo \\
      && ! python -m correspond.testing react fake:example/demo m1 eyes

``correspond.testing`` is the ordinary CLI with an in-memory fake channel registered as
``fake``: a dry-run send, a read of a fake conversation, a reference round trip, and
``NotSupported`` from an operation the fake lacks. Nothing leaves the machine.
"""

import os
import shlex
import subprocess
import sys

import pytest

import correspond
from correspond.testing import demo_channel

ONE_COMMAND = (
    'python -m correspond.testing send fake:example/demo "hello" --dry-run'
    " && python -m correspond.testing read fake:example/demo"
    " && python -m correspond.testing ref fake:example/demo"
    " && ! python -m correspond.testing react fake:example/demo m1 eyes"
)


def _run(args):
    return subprocess.run(
        [sys.executable, "-m", "correspond.testing", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )


def test_one_command_path():
    sent = _run(["send", "fake:example/demo", "hello", "--dry-run"])
    assert sent.returncode == 0, sent.stderr
    assert (
        "dry run" in sent.stdout
        and "nothing was contacted" in sent.stdout
        and "text: hello" in sent.stdout
    )

    read = _run(["read", "fake:example/demo"])
    assert read.returncode == 0, read.stderr
    assert "The export drops the last row." in read.stdout
    assert "(platform)" in read.stdout and "(claimed)" in read.stdout, (
        "every message carries its authenticity grade"
    )

    ref = _run(["ref", "fake:example/demo"])
    assert ref.returncode == 0 and ref.stdout.strip() == "fake:example/demo"

    refused = _run(["react", "fake:example/demo", "m1", "eyes"])
    assert refused.returncode == 1 and "fake does not support react" in refused.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell line")
def test_one_command_as_the_shell_line():
    line = ONE_COMMAND.replace("python -m", f"{shlex.quote(sys.executable)} -m")
    result = subprocess.run(
        line, shell=True, capture_output=True, text=True, encoding="utf-8"
    )
    assert result.returncode == 0, (result.stdout, result.stderr)


def test_the_same_path_in_python_with_a_registered_fake_channel():
    channel = correspond.register_channel(demo_channel("rehearsal"))
    try:
        planned = correspond.send("rehearsal:example/demo", "hello", dry_run=True)
        assert planned.ok and planned.dry_run and channel.sent == []
        assert [m.id for m in correspond.read("rehearsal:example/demo")] == ["m1", "m2"]
        ref = correspond.parse_ref("rehearsal:example/demo")
        assert (
            correspond.ConversationRef.from_dict(ref.to_dict()) == ref
            and correspond.parse_ref(ref.encoded).encoded == ref.encoded
        )
        with pytest.raises(correspond.NotSupported) as caught:
            correspond.react(ref, "m1", "eyes")
        assert caught.value.operation == "react"
    finally:
        correspond.unregister_channel("rehearsal")
