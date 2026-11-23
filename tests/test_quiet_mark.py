"""The CLI's quiet mark: a PostToolUse that finds nothing to speak writes the transcript's size.

A CPU worry (2026-11-21): in one room 66 of 76 PostToolUse hook starts in two minutes were a subagent's tool calls,
each scanning a parent transcript that had not changed and finding nothing. The mark was the first answer (9.92.0): a
shim read it and ended a subagent hook with no Python start. The gate that replaces it reads the transcript, the
watermark and the pins instead (hooks/speak-intermediate.sh; tests/test_intermediate_gate.py), so the shim no longer
reads this mark. The CLI still writes it: a shim installed before the gate reads it, and the CLI cannot know which
shim runs. These tests pin what the CLI writes.
"""

from __future__ import annotations

import json
import uuid

from tests.test_watermark import _assistant, _run_hook, _tool_result, _user, _write_transcript


def test_a_post_tool_use_that_finds_nothing_marks_the_size_it_read(tmp_path, fake_state_dir):
    transcript = tmp_path / "projects" / "-Users-dev" / "uuid-A.jsonl"
    _write_transcript(transcript, [_user("hi"), _assistant("first reply, long enough to speak")])
    assert _run_hook(transcript, "stop") == "first reply, long enough to speak"
    with open(transcript, "a") as f:
        f.write(json.dumps(_tool_result()) + "\n")

    assert _run_hook(transcript, "post_tool_use") is None
    mark = fake_state_dir / "claude_tts_spoken_uuid-A.quiet"  # the name the CLI builds from the path
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


def test_the_mark_name_is_the_one_the_gate_would_build_for_an_old_shim(tmp_path, fake_state_dir):
    """Only the name matters to an installed older shim: <stem of the transcript>.quiet under /tmp."""
    key = f"uuid-{uuid.uuid4().hex}"
    transcript = tmp_path / "projects" / "-Users-dev" / f"{key}.jsonl"
    _write_transcript(transcript, [_user("hi"), _tool_result()])
    assert _run_hook(transcript, "post_tool_use") is None
    assert (fake_state_dir / f"claude_tts_spoken_{key}.quiet").is_file()
