"""A subagent's PostToolUse starts no Python while the parent transcript holds nothing new.

JMO's CPU worry (2026-11-21): in the jmo room 66 of 76 PostToolUse hook starts in two minutes
were brothers' tool calls, each scanning a parent transcript that had not changed and finding
nothing. A PostToolUse that finds nothing marks the transcript's size; hooks/speak-intermediate.sh
ends a subagent hook in the shell while the transcript is still that size and no newer.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from tests.test_watermark import _assistant, _run_hook, _tool_result, _user, _write_transcript

SHIM = Path(__file__).resolve().parent.parent / "hooks" / "speak-intermediate.sh"


def test_a_post_tool_use_that_finds_nothing_marks_the_size_it_read(tmp_path, fake_state_dir):
    transcript = tmp_path / "projects" / "-Users-dev" / "uuid-A.jsonl"
    _write_transcript(transcript, [_user("hi"), _assistant("first reply, long enough to speak")])
    assert _run_hook(transcript, "stop") == "first reply, long enough to speak"
    with open(transcript, "a") as f:
        f.write(json.dumps(_tool_result()) + "\n")

    assert _run_hook(transcript, "post_tool_use") is None
    mark = fake_state_dir / "claude_tts_spoken_uuid-A.quiet"  # the name the shim builds from the path
    assert mark.read_text() == str(transcript.stat().st_size)


def test_a_post_tool_use_that_speaks_leaves_no_mark_for_its_bytes(tmp_path, fake_state_dir):
    transcript = tmp_path / "projects" / "-Users-dev" / "uuid-B.jsonl"
    _write_transcript(transcript, [_user("hi"), _assistant("first reply, long enough to speak")])
    assert _run_hook(transcript, "stop") == "first reply, long enough to speak"
    with open(transcript, "a") as f:
        f.write(json.dumps(_tool_result()) + "\n")
    assert _run_hook(transcript, "post_tool_use") is None
    mark = fake_state_dir / "claude_tts_spoken_uuid-B.quiet"
    quiet = mark.read_text()

    with open(transcript, "a") as f:
        f.write(json.dumps(_assistant("text before the next tool call")) + "\n")
    assert _run_hook(transcript, "post_tool_use") == "text before the next tool call"
    assert mark.read_text() == quiet  # the spoken bytes are not marked quiet
    assert int(quiet) < transcript.stat().st_size


@pytest.fixture
def shim_env(tmp_path):
    """A fake claude-tts that records its stdin, and a transcript with a unique /tmp mark, removed after."""
    key = f"quiet-test-{uuid.uuid4().hex}"
    transcript = tmp_path / "projects" / "-Users-dev" / f"{key}.jsonl"
    _write_transcript(transcript, [_user("hi"), _assistant("a reply"), _tool_result()])
    mark = Path(f"/tmp/claude_tts_spoken_{key}.quiet")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    got = tmp_path / "stdin-got"
    fake = bindir / "claude-tts"
    fake.write_text(f'#!/bin/bash\necho "$@" > "{tmp_path}/args"\ncat > "{got}"\n')
    fake.chmod(0o755)
    env = {**os.environ, "HOME": str(tmp_path), "PATH": f"{bindir}:{os.environ.get('PATH', '')}"}
    yield transcript, mark, got, env
    mark.unlink(missing_ok=True)


def _run_shim(payload: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(SHIM)], input=payload, capture_output=True, text=True, env=env, timeout=10)


def _mark(mark: Path, transcript: Path, value: str | None = None) -> None:
    mark.write_text(value if value is not None else str(transcript.stat().st_size))
    past = time.time() - 5  # the transcript is older than the mark, as when the hook wrote it after its read
    os.utime(transcript, (past, past))


@pytest.mark.parametrize(("agent", "mark_value", "touch_after", "cli_runs"), [
    (True, None, False, False),       # a brother's call, nothing new since the last empty scan: no Python
    (False, None, False, True),       # the parent's own call: its tool_use line may not be written yet
    (True, "absent", False, True),    # no mark: no scan has found nothing yet
    (True, "12", False, True),        # a different size: a line came in since
    (True, "garbage", False, True),   # a mark that is not a number decides nothing
    (True, None, True, True),         # the transcript is newer than the mark: written since, same size or not
])
def test_the_shim_skips_only_a_subagent_hook_on_a_transcript_already_found_quiet(
    shim_env, agent, mark_value, touch_after, cli_runs
):
    transcript, mark, got, env = shim_env
    if mark_value != "absent":
        _mark(mark, transcript, mark_value)
    if touch_after:
        future = time.time() + 5
        os.utime(transcript, (future, future))
    payload = {"session_id": "s", "transcript_path": str(transcript), "tool_name": "Bash",
               "tool_input": {"command": "ls"}, "tool_response": {"stdout": "x" * 200_000}}
    if agent:
        payload["agent_id"] = "a372d89335898f923"
    text = json.dumps(payload)  # past a pipe's buffer: the shim takes it all and hands it on whole

    r = _run_shim(text, env)

    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")
    assert got.exists() is cli_runs
    if cli_runs:
        assert json.loads(got.read_text()) == payload


def test_an_agent_id_inside_a_tool_string_is_not_a_subagent(shim_env):
    """Only a key reads as the subagent's: the same words inside a string are escaped quotes."""
    transcript, mark, got, env = shim_env
    _mark(mark, transcript)
    payload = {"transcript_path": str(transcript), "tool_name": "Bash",
               "tool_input": {"command": 'echo \'{"agent_id":"x"}\''}}
    r = _run_shim(json.dumps(payload), env)
    assert r.returncode == 0 and got.exists()


def test_a_path_the_shell_cannot_read_plainly_runs_the_cli(shim_env):
    transcript, mark, got, env = shim_env
    _mark(mark, transcript)
    text = json.dumps({"transcript_path": str(transcript), "agent_id": "a1"}).replace("/", "\\/")
    r = _run_shim(text, env)
    assert r.returncode == 0 and got.exists()
