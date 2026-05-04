"""Tests for the per-transcript watermark scoping.

Two `claude` instances opened in the same folder share a session_id (the
encoded folder name). Before v9.0.1 they also shared the watermark file,
which caused duplicated speech: stale-resets on one transcript would
trigger PostToolUse on the other to re-extract previously-spoken text.

The fix scopes the watermark by transcript UUID instead of session_id.
"""

from __future__ import annotations

import argparse
import io
import json
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_code_tts.cli import _speak_from_hook
from claude_code_tts.config import TTSConfig


def _write_transcript(path: Path, lines: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")


def _assistant(text: str) -> dict:
    return {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": text}]},
    }


def _user(text: str) -> dict:
    return {
        "type": "user",
        "message": {"content": [{"type": "text", "text": text}]},
    }


def _tool_result() -> dict:
    return {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}}


@pytest.fixture
def fake_state_dir(tmp_path, monkeypatch):
    """Redirect /tmp watermark files into tmp_path so tests don't collide."""
    real_path_class = Path
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    def fake_path(arg):
        s = str(arg)
        if s.startswith("/tmp/claude_tts_spoken_") or s.startswith("/tmp/claude_tts_wm_"):
            return state_dir / real_path_class(s).name
        return real_path_class(arg)

    monkeypatch.setattr("claude_code_tts.cli.Path", fake_path)
    yield state_dir


@pytest.fixture
def base_config():
    return TTSConfig(
        mode="direct",
        muted=False,
        intermediate=True,
        speed=2.0,
        active_persona="claude-prime",
        session_id="-Users-jwmoore",
        project_name="home",
    )


def _run_hook(transcript_path: Path, hook_type: str, tool_name: str = "Bash") -> str | None:
    """Invoke _speak_from_hook with mocked speak() and return what was spoken."""
    spoken: list[str] = []

    hook_input = json.dumps({
        "transcript_path": str(transcript_path),
        "tool_name": tool_name,
    })

    args = argparse.Namespace(hook_type=hook_type)

    with patch("sys.stdin", io.StringIO(hook_input)), \
         patch("claude_code_tts.cli.load_config") as mock_load, \
         patch("claude_code_tts.audio.speak", side_effect=lambda text, cfg: spoken.append(text)), \
         patch("claude_code_tts.session.pin_session"):
        mock_load.return_value = TTSConfig(
            mode="direct", muted=False, intermediate=True,
            session_id="-Users-jwmoore", project_name="home",
        )
        _speak_from_hook(args)

    return spoken[0] if spoken else None


class TestWatermarkScoping:
    """Watermark files are named per transcript UUID, not per session folder."""

    def test_state_file_uses_transcript_uuid(self, tmp_path, fake_state_dir):
        projects = tmp_path / "projects" / "-Users-jwmoore"
        transcript = projects / "uuid-A.jsonl"
        _write_transcript(transcript, [_user("hi there"), _assistant("hello there world")])

        _run_hook(transcript, "stop")

        assert (fake_state_dir / "claude_tts_spoken_uuid-A.state").exists()
        # The legacy session-id-keyed name is NOT written.
        assert not (fake_state_dir / "claude_tts_spoken_-Users-jwmoore.state").exists()

    def test_two_transcripts_have_independent_state(self, tmp_path, fake_state_dir):
        projects = tmp_path / "projects" / "-Users-jwmoore"
        ta = projects / "uuid-A.jsonl"
        tb = projects / "uuid-B.jsonl"
        _write_transcript(ta, [_user("hi there"), _assistant("hello from session A")])
        _write_transcript(
            tb,
            [_user("hi"), _assistant("first reply from B"),
             _user("ok"), _assistant("second reply from B")],
        )

        _run_hook(ta, "stop")
        _run_hook(tb, "stop")

        assert (fake_state_dir / "claude_tts_spoken_uuid-A.state").exists()
        assert (fake_state_dir / "claude_tts_spoken_uuid-B.state").exists()
        # The two state files hold different line counts.
        wm_a = (fake_state_dir / "claude_tts_spoken_uuid-A.state").read_text().strip()
        wm_b = (fake_state_dir / "claude_tts_spoken_uuid-B.state").read_text().strip()
        assert int(wm_a) == 2
        assert int(wm_b) == 4


class TestDuplicationRegression:
    """The A=>B=>A=>B=>C bug: PostToolUse re-speaks prior turn's text after
    the OTHER session's hook stale-resets the shared watermark."""

    def test_post_tool_use_does_not_replay_prior_turn(self, tmp_path, fake_state_dir):
        projects = tmp_path / "projects" / "-Users-jwmoore"
        ta = projects / "uuid-A.jsonl"
        tb = projects / "uuid-B.jsonl"

        text_a = "session A turn one assistant reply"
        text_b = "session B turn one assistant reply"
        text_c = "session A turn two assistant reply"

        # Session A: 4-line turn ending with text_a
        _write_transcript(
            ta,
            [_user("hi"), _assistant("partial response one"),
             _user("ok"), _assistant(text_a)],
        )
        # Session B: 2-line turn ending with text_b (shorter than A — this
        # is what triggers stale-reset of the shared watermark in old code)
        _write_transcript(tb, [_user("hi"), _assistant(text_b)])

        # 1) A speaks turn 1
        assert _run_hook(ta, "stop") == text_a

        # 2) B speaks turn 1. With shared watermark this would stale-reset
        # because B's transcript is shorter. With per-transcript scoping B
        # has its own wm, no thrash.
        assert _run_hook(tb, "stop") == text_b

        # 3) A starts turn 2: tool fires, then PostToolUse hook fires.
        # Append a tool_result line but no new assistant text yet.
        with open(ta, "a") as f:
            f.write(json.dumps(_tool_result()) + "\n")

        # With the OLD shared-watermark behavior, this PostToolUse would
        # see wm thrashed down by B's hook, scan the full window, and
        # return the prior turn's text_a — duplicating it. With per-transcript
        # scoping, A's wm is still at line 4 and the [4:5] window has no
        # assistant text, so nothing is spoken.
        spoken_a_post = _run_hook(ta, "post_tool_use")
        assert spoken_a_post != text_a, "PostToolUse re-spoke prior turn's assistant text"
        assert spoken_a_post is None

        # 4) A finishes turn 2 with new assistant text_c
        with open(ta, "a") as f:
            f.write(json.dumps(_user("u")) + "\n")
            f.write(json.dumps(_assistant(text_c)) + "\n")
        assert _run_hook(ta, "stop") == text_c
