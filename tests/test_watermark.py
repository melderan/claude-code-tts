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
        session_id="-Users-dev",
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
            session_id="-Users-dev", project_name="home",
        )
        _speak_from_hook(args)

    return spoken[-1] if spoken else None


class TestWatermarkScoping:
    """Watermark files are named per transcript UUID, not per session folder."""

    def test_state_file_uses_transcript_uuid(self, tmp_path, fake_state_dir):
        projects = tmp_path / "projects" / "-Users-dev"
        transcript = projects / "uuid-A.jsonl"
        _write_transcript(transcript, [_user("hi there"), _assistant("hello there world")])

        _run_hook(transcript, "stop")

        assert (fake_state_dir / "claude_tts_spoken_uuid-A.state").exists()
        # The legacy session-id-keyed name is NOT written.
        assert not (fake_state_dir / "claude_tts_spoken_-Users-dev.state").exists()

    def test_two_transcripts_have_independent_state(self, tmp_path, fake_state_dir):
        projects = tmp_path / "projects" / "-Users-dev"
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
        projects = tmp_path / "projects" / "-Users-dev"
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


class TestPAISummaryExtraction:
    """Stop hook speaks only the 🗣️ summary line from PAI-formatted responses."""

    def test_labeled_pai_line_speaks_summary_only(self, tmp_path, fake_state_dir):
        """Transcript with 🗣️ Lode: <summary> → summary always present in spoken output."""
        projects = tmp_path / "projects" / "-Users-dev"
        transcript = projects / "uuid-pai-a.jsonl"
        pai_block = (
            "════ PAI | NATIVE MODE ════\n"
            "\U0001F5E3 Lode: Voice bridge online; PAI now speaks through claude-code-tts."
        )
        _write_transcript(transcript, [_user("go"), _assistant(pai_block)])
        spoken = _run_hook(transcript, "stop")
        assert spoken is not None
        assert spoken.endswith("Voice bridge online; PAI now speaks through claude-code-tts.")

    def test_no_pai_line_falls_through_to_filter(self, tmp_path, fake_state_dir):
        """Transcript with no 🗣️ line uses the existing filter_text path unchanged."""
        projects = tmp_path / "projects" / "-Users-dev"
        transcript = projects / "uuid-pai-b.jsonl"
        plain = "Done. The file has been updated with the requested changes."
        _write_transcript(transcript, [_user("fix it"), _assistant(plain)])
        spoken = _run_hook(transcript, "stop")
        # filter_text will return the plain text largely unchanged at this length
        assert spoken is not None
        assert "Done" in spoken

    def test_multiple_pai_lines_last_wins(self, tmp_path, fake_state_dir):
        """When multiple 🗣️ lines present, the last summary is spoken (body+summary combined)."""
        projects = tmp_path / "projects" / "-Users-dev"
        transcript = projects / "uuid-pai-c.jsonl"
        pai_block = (
            "\U0001F5E3 Connery: First summary line that should be ignored.\n"
            "some intermediate content\n"
            "\U0001F5E3 Connery: Second summary line that wins."
        )
        _write_transcript(transcript, [_user("go"), _assistant(pai_block)])
        spoken = _run_hook(transcript, "stop")
        assert spoken is not None
        assert spoken.endswith("Second summary line that wins.")
        assert "First summary line" not in spoken

    def test_labelless_pai_line_strips_emoji_only(self, tmp_path, fake_state_dir):
        """🗣️ line with no label: emoji stripped, rest spoken."""
        projects = tmp_path / "projects" / "-Users-dev"
        transcript = projects / "uuid-pai-d.jsonl"
        pai_block = "\U0001F5E3 All tiers verified and serving on flare."
        _write_transcript(transcript, [_user("status"), _assistant(pai_block)])
        spoken = _run_hook(transcript, "stop")
        assert spoken == "All tiers verified and serving on flare."


# --- v9.10.0: summary-shaped prose, async-safe claims, unspoken intermediates ---


def _redacted_thinking(msg_id: str) -> dict:
    """Real reasoning as Claude Code 2.1.278 stores it: empty body, signature kept."""
    return {
        "type": "assistant",
        "message": {"id": msg_id, "content": [{"type": "thinking", "thinking": "", "signature": "sig-real"}]},
    }


def _summary_thinking(msg_id: str, text: str) -> dict:
    """The prose shown to the user, stored as a short signed thinking block."""
    return {
        "type": "assistant",
        "message": {"id": msg_id, "content": [{"type": "thinking", "thinking": text, "signature": "sig-short"}]},
    }


def _full_thinking(msg_id: str, text: str) -> dict:
    """A transcript that keeps full reasoning: non-empty body, no redacted twin."""
    return {
        "type": "assistant",
        "message": {"id": msg_id, "content": [{"type": "thinking", "thinking": text, "signature": "sig"}]},
    }


def _tool_use(msg_id: str) -> dict:
    return {
        "type": "assistant",
        "message": {"id": msg_id, "content": [{"type": "tool_use", "name": "Bash", "input": {}}]},
    }


def _assistant_msg(msg_id: str, text: str) -> dict:
    return {"type": "assistant", "message": {"id": msg_id, "content": [{"type": "text", "text": text}]}}


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("claude_code_tts.cli.time.sleep", lambda _s: None)


class TestSummaryShapedProse:
    """Prose stored as a signed thinking block beside redacted reasoning is spoken."""

    def test_post_tool_use_speaks_summary_block(self, tmp_path, fake_state_dir):
        transcript = tmp_path / "projects" / "-Users-dev" / "uuid-S.jsonl"
        _write_transcript(transcript, [_user("hi"), _assistant_msg("m0", "opening reply text here")])
        assert _run_hook(transcript, "stop") == "opening reply text here"

        summary = "The debug log lived in the sandbox, so the evidence was lost. Reading the parser next."
        with open(transcript, "a") as f:
            for line in (_user("go"), _redacted_thinking("m1"), _summary_thinking("m1", summary),
                         _tool_use("m1"), _tool_result()):
                f.write(json.dumps(line) + "\n")

        assert _run_hook(transcript, "post_tool_use") == summary

    def test_full_reasoning_is_never_spoken(self, tmp_path, fake_state_dir):
        transcript = tmp_path / "projects" / "-Users-dev" / "uuid-F.jsonl"
        _write_transcript(transcript, [_user("hi"), _assistant_msg("m0", "opening reply text here")])
        _run_hook(transcript, "stop")

        with open(transcript, "a") as f:
            for line in (_user("go"), _full_thinking("m1", "private reasoning that must stay private, long enough"),
                         _tool_use("m1"), _tool_result()):
                f.write(json.dumps(line) + "\n")

        assert _run_hook(transcript, "post_tool_use") is None

    def test_text_block_wins_over_summary_in_same_message(self, tmp_path, fake_state_dir):
        transcript = tmp_path / "projects" / "-Users-dev" / "uuid-T.jsonl"
        _write_transcript(transcript, [_user("hi"), _assistant_msg("m0", "opening reply text here")])
        _run_hook(transcript, "stop")

        with open(transcript, "a") as f:
            for line in (_user("go"), _redacted_thinking("m1"), _summary_thinking("m1", "a rewrite of the text"),
                         _assistant_msg("m1", "the verbatim text the user saw"), _tool_use("m1"), _tool_result()):
                f.write(json.dumps(line) + "\n")

        assert _run_hook(transcript, "post_tool_use") == "the verbatim text the user saw"


class TestAsyncHookClaims:
    """Two overlapping PostToolUse hooks that read the same text: one speaks."""

    def test_second_hook_over_same_lines_is_silent(self, tmp_path, fake_state_dir):
        transcript = tmp_path / "projects" / "-Users-dev" / "uuid-C.jsonl"
        _write_transcript(transcript, [_user("hi"), _assistant_msg("m0", "opening reply text here")])
        _run_hook(transcript, "stop")
        with open(transcript, "a") as f:
            for line in (_user("go"), _assistant_msg("m1", "intermediate update for the user"),
                         _tool_use("m1"), _tool_result()):
                f.write(json.dumps(line) + "\n")

        assert _run_hook(transcript, "post_tool_use") == "intermediate update for the user"
        assert _run_hook(transcript, "post_tool_use") is None

    def test_claim_refuses_line_below_watermark(self, tmp_path):
        from claude_code_tts.cli import _claim_watermark
        state = tmp_path / "wm.state"
        lock = tmp_path / "wm.lock"
        state.write_text("10")
        assert _claim_watermark(state, lock, text_line=12, line_count=15) is True
        assert state.read_text() == "15"
        assert _claim_watermark(state, lock, text_line=12, line_count=16) is False
        assert state.read_text() == "15"


class TestStopSpeaksUnspokenIntermediates:
    def _transcript_with_missed_intermediates(self, tmp_path, name):
        transcript = tmp_path / "projects" / "-Users-dev" / f"{name}.jsonl"
        _write_transcript(transcript, [_user("hi"), _assistant_msg("m0", "opening reply text here")])
        _run_hook(transcript, "stop")
        with open(transcript, "a") as f:
            for line in (_user("go"),
                         _assistant_msg("m1", "first intermediate update text"), _tool_use("m1"), _tool_result(),
                         _redacted_thinking("m2"), _summary_thinking("m2", "second intermediate as a summary block"),
                         _tool_use("m2"), _tool_result(),
                         _assistant_msg("m3", "the final response text")):
                f.write(json.dumps(line) + "\n")
        return transcript

    def test_stop_reads_missed_intermediates_in_order(self, tmp_path, fake_state_dir):
        transcript = self._transcript_with_missed_intermediates(tmp_path, "uuid-U")
        spoken = _run_hook(transcript, "stop")
        assert spoken == (
            "first intermediate update text second intermediate as a summary block the final response text"
        )

    def test_stop_skips_missed_intermediates_when_intermediate_off(self, tmp_path, fake_state_dir):
        transcript = self._transcript_with_missed_intermediates(tmp_path, "uuid-V")
        spoken: list[str] = []
        hook_input = json.dumps({"transcript_path": str(transcript), "tool_name": "Bash"})
        with patch("sys.stdin", io.StringIO(hook_input)), \
             patch("claude_code_tts.cli.load_config") as mock_load, \
             patch("claude_code_tts.audio.speak", side_effect=lambda text, cfg: spoken.append(text)), \
             patch("claude_code_tts.session.pin_session"):
            mock_load.return_value = TTSConfig(
                mode="direct", muted=False, intermediate=False,
                session_id="-Users-dev", project_name="home",
            )
            _speak_from_hook(argparse.Namespace(hook_type="stop"))
        assert spoken == ["the final response text"]


class TestTranscriptReread:
    def test_post_tool_use_waits_for_lagging_transcript(self, tmp_path, fake_state_dir, monkeypatch):
        transcript = tmp_path / "projects" / "-Users-dev" / "uuid-L.jsonl"
        _write_transcript(transcript, [_user("hi"), _assistant_msg("m0", "opening reply text here")])
        _run_hook(transcript, "stop")

        def late_write(_seconds):
            with open(transcript, "a") as f:
                for line in (_user("go"), _assistant_msg("m1", "text that arrived a moment late"),
                             _tool_use("m1"), _tool_result()):
                    f.write(json.dumps(line) + "\n")

        monkeypatch.setattr("claude_code_tts.cli.time.sleep", late_write)
        assert _run_hook(transcript, "post_tool_use") == "text that arrived a moment late"

    def test_task_tool_is_no_longer_skipped(self, tmp_path, fake_state_dir):
        transcript = tmp_path / "projects" / "-Users-dev" / "uuid-K.jsonl"
        _write_transcript(transcript, [_user("hi"), _assistant_msg("m0", "opening reply text here")])
        _run_hook(transcript, "stop")
        with open(transcript, "a") as f:
            for line in (_user("go"), _assistant_msg("m1", "text before launching a sub-agent"),
                         _tool_use("m1"), _tool_result()):
                f.write(json.dumps(line) + "\n")
        assert _run_hook(transcript, "post_tool_use", tool_name="Task") == "text before launching a sub-agent"
