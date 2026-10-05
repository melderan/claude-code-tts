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


def _run_hook(
    transcript_path: Path, hook_type: str, tool_name: str = "Bash", last_assistant_message: str | None = None
) -> str | None:
    """Invoke _speak_from_hook with mocked speak() and return what was spoken."""
    spoken: list[str] = []

    payload: dict = {"transcript_path": str(transcript_path), "tool_name": tool_name}
    if last_assistant_message is not None:
        payload["last_assistant_message"] = last_assistant_message
    hook_input = json.dumps(payload)

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


class TestTranscriptGrowsDuringHook:
    """Lines written between the line count and the scan are covered by the watermark.

    Seen 2026-09-26 in the debug log: a PostToolUse hook counted 1833 lines,
    the scan then read a text at index 1834 and spoke it, and the watermark
    was written as 1833. The next hook found index 1834 again and spoke it
    a second time, 20 s later.
    """

    def _grow_before_scan(self, transcript, monkeypatch):
        import claude_code_tts.cli as cli
        real_scan = cli._scan_transcript
        grown = False

        def scan_after_growth(path, watermark, hook_type):
            nonlocal grown
            if not grown:
                grown = True
                with open(transcript, "a") as f:
                    for line in (_assistant_msg("m1", "text written while the hook was counting"),
                                 _tool_use("m1"), _tool_result()):
                        f.write(json.dumps(line) + "\n")
            return real_scan(path, watermark, hook_type)

        monkeypatch.setattr("claude_code_tts.cli._scan_transcript", scan_after_growth)

    def test_post_tool_use_covers_lines_the_scan_read(self, tmp_path, fake_state_dir, monkeypatch):
        transcript = tmp_path / "projects" / "-Users-dev" / "uuid-G.jsonl"
        _write_transcript(transcript, [_user("hi"), _assistant_msg("m0", "opening reply text here")])
        _run_hook(transcript, "stop")
        with open(transcript, "a") as f:
            f.write(json.dumps(_user("go")) + "\n")

        self._grow_before_scan(transcript, monkeypatch)
        assert _run_hook(transcript, "post_tool_use") == "text written while the hook was counting"
        assert _run_hook(transcript, "post_tool_use") is None

    def test_stop_covers_lines_the_scan_read(self, tmp_path, fake_state_dir, monkeypatch):
        transcript = tmp_path / "projects" / "-Users-dev" / "uuid-H.jsonl"
        _write_transcript(transcript, [_user("hi"), _assistant_msg("m0", "opening reply text here")])
        _run_hook(transcript, "stop")
        with open(transcript, "a") as f:
            f.write(json.dumps(_user("go")) + "\n")

        self._grow_before_scan(transcript, monkeypatch)
        assert _run_hook(transcript, "stop") == "text written while the hook was counting"
        assert _run_hook(transcript, "post_tool_use") is None


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


class TestSessionFromEnvOrPath:
    """The hook must behave the same whether the session id comes from the kit's env var or the path."""

    @pytest.mark.parametrize("env_session", [None, "room-from-env"])
    def test_pai_summary_spoken_either_way(self, tmp_path, fake_state_dir, monkeypatch, env_session):
        """9.25.1 raised UnboundLocalError here when CLAUDE_TTS_SESSION was set (shadowed `re`)."""
        if env_session is None:
            monkeypatch.delenv("CLAUDE_TTS_SESSION", raising=False)
        else:
            monkeypatch.setenv("CLAUDE_TTS_SESSION", env_session)
        projects = tmp_path / "projects" / "-Users-dev"
        transcript = projects / f"uuid-env-{bool(env_session)}.jsonl"
        _write_transcript(transcript, [_user("go"), _assistant("\U0001F5E3 Lode: Summary spoken either way.")])
        spoken = _run_hook(transcript, "stop")
        assert spoken is not None and spoken.endswith("Summary spoken either way.")


def _append(path: Path, lines: list[dict]) -> None:
    with open(path, "a") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")


class TestStopWaitsForTheResponse:
    """Claude Code 2.1.286 (2026-09-30) writes the response in the same second it fires Stop,
    usually after the hook has read the transcript. A Stop that finds the turn's tool lines
    but no text waits for the text instead of moving the watermark past them, which made
    every room speak each answer one message late."""

    def test_stop_speaks_a_response_that_lands_after_it_fired(self, tmp_path, fake_state_dir, monkeypatch):
        transcript = tmp_path / "projects" / "-Users-dev" / "uuid-S.jsonl"
        _write_transcript(transcript, [_user("hi"), _assistant_msg("m0", "opening reply text here")])
        _run_hook(transcript, "stop")
        _append(transcript, [_user("go"), _tool_use("m1"), _tool_result()])

        sleeps: list[float] = []

        def late_write(seconds: float) -> None:
            sleeps.append(seconds)
            if len(sleeps) == 3:
                _append(transcript, [_assistant_msg("m2", "the answer that landed after the hook fired")])

        monkeypatch.setattr("claude_code_tts.cli.time.sleep", late_write)
        assert _run_hook(transcript, "stop") == "the answer that landed after the hook fired"
        assert len(sleeps) == 3, "the hook stops waiting as soon as the text is there"

    def test_stop_with_no_response_gives_up_within_budget(self, tmp_path, fake_state_dir, monkeypatch):
        from claude_code_tts.cli import STOP_REREAD_ATTEMPTS

        transcript = tmp_path / "projects" / "-Users-dev" / "uuid-T.jsonl"
        _write_transcript(transcript, [_user("hi"), _assistant_msg("m0", "opening reply text here")])
        _run_hook(transcript, "stop")
        _append(transcript, [_user("go"), _tool_use("m1"), _tool_result()])

        sleeps: list[float] = []
        monkeypatch.setattr("claude_code_tts.cli.time.sleep", sleeps.append)
        assert _run_hook(transcript, "stop") is None
        assert len(sleeps) == 1 + STOP_REREAD_ATTEMPTS

        # The watermark covers the tool lines: the next answer is spoken alone.
        _append(transcript, [_user("again"), _assistant_msg("m2", "the next answer, spoken on its own")])
        assert _run_hook(transcript, "stop") == "the next answer, spoken on its own"

    def test_first_stop_on_a_transcript_waits_for_the_file_to_settle(self, tmp_path, fake_state_dir, monkeypatch):
        """With no watermark the Stop takes the last text in the file; the response must get there first."""
        transcript = tmp_path / "projects" / "-Users-dev" / "uuid-U.jsonl"
        _write_transcript(transcript, [_user("hi"), _assistant_msg("m0", "prose before the tool call"),
                                       _tool_use("m0"), _tool_result()])

        def late_write(seconds: float) -> None:
            # The old tenth-of-a-second yield is over before the response lands.
            if seconds >= 0.5:
                _append(transcript, [_assistant_msg("m1", "the final answer of the first turn")])

        monkeypatch.setattr("claude_code_tts.cli.time.sleep", late_write)
        assert _run_hook(transcript, "stop") == "the final answer of the first turn"


class TestStopSpeaksFromItsInput:
    """The Stop hook's input carries the response (last_assistant_message); since Claude Code
    2.1.286 the transcript gets it only after the hook fires. Stop speaks from the input at
    once and records what it said; the hook that later finds the line skips it."""

    def _turn_in_progress(self, tmp_path, name: str) -> Path:
        transcript = tmp_path / "projects" / "-Users-dev" / f"{name}.jsonl"
        _write_transcript(transcript, [_user("hi"), _assistant_msg("m0", "opening reply text here")])
        _run_hook(transcript, "stop", last_assistant_message="opening reply text here")
        _append(transcript, [_user("go"), _tool_use("m1"), _tool_result()])
        return transcript

    def test_stop_speaks_the_input_without_waiting(self, tmp_path, fake_state_dir, monkeypatch):
        transcript = self._turn_in_progress(tmp_path, "uuid-I")
        sleeps: list[float] = []
        monkeypatch.setattr("claude_code_tts.cli.time.sleep", sleeps.append)
        assert _run_hook(transcript, "stop", last_assistant_message="the answer, straight from the input") == (
            "the answer, straight from the input"
        )
        assert len(sleeps) == 1, "the yield only; no reread loop, no settle"

    def test_the_landed_response_is_not_spoken_again(self, tmp_path, fake_state_dir):
        transcript = self._turn_in_progress(tmp_path, "uuid-J")
        _run_hook(transcript, "stop", last_assistant_message="the answer, straight from the input")
        # The response lands, then the next turn begins with an intermediate text.
        _append(transcript, [_assistant_msg("m2", "the answer, straight from the input")])
        assert _run_hook(transcript, "stop", last_assistant_message="") is None, "a Stop with nothing new is silent"
        _append(transcript, [_user("next"), _assistant_msg("m3", "working on the next thing now"), _tool_use("m3"),
                             _tool_result()])
        assert _run_hook(transcript, "post_tool_use") == "working on the next thing now"

    def test_post_tool_use_covers_a_landed_response_it_skipped(self, tmp_path, fake_state_dir):
        transcript = self._turn_in_progress(tmp_path, "uuid-K")
        _run_hook(transcript, "stop", last_assistant_message="the answer, straight from the input")
        _append(transcript, [_assistant_msg("m2", "the answer, straight from the input"), _user("next"),
                             _tool_use("m3"), _tool_result()])
        assert _run_hook(transcript, "post_tool_use") is None
        # Watermark moved past the landed line: a later hook does not find it either.
        _append(transcript, [_tool_use("m3"), _tool_result()])
        assert _run_hook(transcript, "post_tool_use") is None
        assert _run_hook(transcript, "stop", last_assistant_message="") is None

    def test_a_response_already_in_the_file_is_spoken_once(self, tmp_path, fake_state_dir):
        transcript = self._turn_in_progress(tmp_path, "uuid-L2")
        _append(transcript, [_assistant_msg("m2", "the answer, already written down")])
        assert _run_hook(transcript, "stop", last_assistant_message="the answer, already written down") == (
            "the answer, already written down"
        )
        _append(transcript, [_user("next"), _tool_use("m3"), _tool_result()])
        assert _run_hook(transcript, "post_tool_use") is None
        assert _run_hook(transcript, "stop", last_assistant_message="") is None

    def test_unspoken_intermediates_come_before_the_input(self, tmp_path, fake_state_dir):
        transcript = self._turn_in_progress(tmp_path, "uuid-M")
        _append(transcript, [_assistant_msg("m1b", "an intermediate nobody spoke yet"), _tool_use("m1b"),
                             _tool_result()])
        spoken = _run_hook(transcript, "stop", last_assistant_message="then the final answer arrives")
        assert spoken == "an intermediate nobody spoke yet then the final answer arrives"

    def test_a_final_message_with_an_earlier_block_speaks_only_that_block(self, tmp_path, fake_state_dir):
        """Unmeasured whether Claude Code ever stores two text blocks in one message (Geordi,
        2026-10-01); if it does, the input carries the last one and the first must not be lost."""
        transcript = self._turn_in_progress(tmp_path, "uuid-N")
        _run_hook(transcript, "stop", last_assistant_message="And the second block of the same message.")
        _append(transcript, [{"type": "assistant", "message": {"id": "m2", "content": [
            {"type": "text", "text": "The first block of the final message."},
            {"type": "text", "text": "And the second block of the same message."}]}}])
        _append(transcript, [_user("next"), _tool_use("m3"), _tool_result()])
        assert _run_hook(transcript, "post_tool_use") == "The first block of the final message."

    def test_a_short_last_block_is_spoken_once_and_the_first_block_once(self, tmp_path, fake_state_dir):
        transcript = self._turn_in_progress(tmp_path, "uuid-N2")
        assert _run_hook(transcript, "stop", last_assistant_message="All done, ship it.") == "All done, ship it."
        _append(transcript, [{"type": "assistant", "message": {"id": "m2", "content": [
            {"type": "text", "text": "The first block of the final message."},
            {"type": "text", "text": "All done, ship it."}]}}])
        _append(transcript, [_user("next"), _tool_use("m3"), _tool_result()])
        assert _run_hook(transcript, "post_tool_use") == "The first block of the final message."

    def test_an_intermediate_containing_the_response_is_not_the_response(self, tmp_path, fake_state_dir):
        transcript = self._turn_in_progress(tmp_path, "uuid-N3")
        _append(transcript, [_assistant_msg("m1b", "Running the tests to confirm the fix works."), _tool_use("m1b"),
                             _tool_result()])
        spoken = _run_hook(transcript, "stop", last_assistant_message="the tests to confirm the fix")
        assert spoken == "Running the tests to confirm the fix works. the tests to confirm the fix"
        _append(transcript, [_assistant_msg("m2", "the tests to confirm the fix"), _user("n"), _tool_use("m3"),
                             _tool_result()])
        assert _run_hook(transcript, "post_tool_use") is None

    def test_a_post_tool_use_match_clears_the_record(self, tmp_path, fake_state_dir):
        transcript = self._turn_in_progress(tmp_path, "uuid-N4")
        final = "I fixed the parser and all forty tests pass now."
        _run_hook(transcript, "stop", last_assistant_message=final)
        _append(transcript, [_assistant_msg("m2", final), _user("next"), _tool_use("m3"), _tool_result()])
        assert _run_hook(transcript, "post_tool_use") is None
        _append(transcript, [_assistant_msg("m4", "all forty tests pass now"), _tool_use("m4"), _tool_result()])
        assert _run_hook(transcript, "post_tool_use") == "all forty tests pass now"

    def test_only_the_first_new_text_is_compared(self, tmp_path, fake_state_dir):
        transcript = self._turn_in_progress(tmp_path, "uuid-N5")
        _run_hook(transcript, "stop", last_assistant_message="the same words, later in the turn")
        # A different response landed (the record is stale); a later text equal to the record is new.
        _append(transcript, [_assistant_msg("m2", "a different response landed here instead"), _user("next"),
                             _assistant_msg("m3", "the same words, later in the turn"), _tool_use("m3"), _tool_result()])
        spoken = _run_hook(transcript, "post_tool_use")
        assert spoken == "the same words, later in the turn"

    def test_the_record_holds_no_text_and_only_this_user_reads_it(self, tmp_path, fake_state_dir):
        import stat

        transcript = self._turn_in_progress(tmp_path, "uuid-N6")
        secret = "the quarterly numbers are in the attached draft"
        _run_hook(transcript, "stop", last_assistant_message=secret)
        pending = next(fake_state_dir.glob("claude_tts_spoken_uuid-N6.pending"))
        assert "quarterly" not in pending.read_text()
        assert stat.S_IMODE(pending.stat().st_mode) == 0o600

    def test_a_record_from_hours_ago_still_skips_its_landed_line(self, tmp_path, fake_state_dir):
        """2026-10-02 18:05 PDT: JMO came back after two hours and the first PostToolUse of the new
        turn re-spoke the whole previous reply (2 min 20 s), because the record that marks it as
        spoken had been aged out at one hour. The landed line comes when the next turn does."""
        import os

        transcript = self._turn_in_progress(tmp_path, "uuid-N7")
        _run_hook(transcript, "stop", last_assistant_message="a response from two hours ago")
        pending = next(fake_state_dir.glob("claude_tts_spoken_uuid-N7.pending"))
        old = pending.stat().st_mtime - 8000
        os.utime(pending, (old, old))
        _append(transcript, [_assistant_msg("m2", "a response from two hours ago"), _user("next"), _tool_use("m3"),
                             _tool_result()])
        assert _run_hook(transcript, "post_tool_use") is None, "the landed reply was spoken two hours ago"
        assert not pending.exists(), "the take of the landed line retires the record"
        _append(transcript, [_assistant_msg("m4", "the new turn's first words"), _tool_use("m4"), _tool_result()])
        assert _run_hook(transcript, "post_tool_use") == "the new turn's first words"

    def test_stop_does_not_speak_an_intermediate_a_lagging_post_tool_use_took(
        self, tmp_path, fake_state_dir, monkeypatch
    ):
        from claude_code_tts import cli

        transcript = self._turn_in_progress(tmp_path, "uuid-N8")
        _append(transcript, [_assistant_msg("m1b", "an intermediate both hooks can see"), _tool_use("m1b"),
                             _tool_result()])
        real_scan = cli._scan_transcript
        taken = {"done": False}

        def scan_then_other_hook_claims(path, watermark, hook_type):
            result = real_scan(path, watermark, hook_type)
            if not taken["done"]:
                taken["done"] = True
                # The lagging PostToolUse claims and speaks the intermediate right after the Stop's read.
                cli._claim_watermark(
                    cli.Path(f"/tmp/claude_tts_spoken_{path.stem}.state"),
                    cli.Path(f"/tmp/claude_tts_wm_{path.stem}.lock"),
                    result[0][0][0], result[1],
                )
            return result

        monkeypatch.setattr("claude_code_tts.cli._scan_transcript", scan_then_other_hook_claims)
        assert _run_hook(transcript, "stop", last_assistant_message="and then the final answer") == (
            "and then the final answer"
        )

    def test_two_stop_hooks_for_one_event_speak_the_input_once(self, tmp_path, fake_state_dir):
        """Claude Code fired Stop twice for one turn end (2026-10-01, this room, three of thirteen);
        both carry the same input. The record is the claim: exactly one speaks."""
        transcript = self._turn_in_progress(tmp_path, "uuid-twice-a")
        first = _run_hook(transcript, "stop", last_assistant_message="the answer both hooks were handed")
        second = _run_hook(transcript, "stop", last_assistant_message="the answer both hooks were handed")
        assert [first, second] == ["the answer both hooks were handed", None]
        # And the landed line is still recognized and skipped afterwards.
        _append(transcript, [_assistant_msg("m2", "the answer both hooks were handed"), _user("next"),
                             _tool_use("m3"), _tool_result()])
        assert _run_hook(transcript, "post_tool_use") is None

    def test_the_stop_that_loses_the_claim_does_nothing_at_all(self, tmp_path, fake_state_dir):
        """Review of design v2 (2026-10-01): the twin that loses the spoken-store claim must not
        rewrite .pending or move the watermark. Until 9.39.6 the second Stop here spoke a late
        intermediate and moved the watermark; now it leaves both files exactly as the winner
        wrote them, and the intermediate waits for the next hook's scan."""
        transcript = self._turn_in_progress(tmp_path, "uuid-twice-b")
        _run_hook(transcript, "stop", last_assistant_message="the answer both hooks were handed")
        state = fake_state_dir / "claude_tts_spoken_uuid-twice-b.state"
        pending = fake_state_dir / "claude_tts_spoken_uuid-twice-b.pending"
        before = (state.read_text(), pending.read_text(), pending.stat().st_mtime_ns)
        _append(transcript, [_assistant_msg("m1b", "a late intermediate only the second hook sees"), _tool_use("m1b"),
                             _tool_result()])
        assert _run_hook(transcript, "stop", last_assistant_message="the answer both hooks were handed") is None
        assert (state.read_text(), pending.read_text(), pending.stat().st_mtime_ns) == before

    def test_two_post_tool_use_hooks_meeting_the_landed_line_speak_it_never(
        self, tmp_path, fake_state_dir, monkeypatch
    ):
        """2026-10-01, 14:00:30: two PostToolUse hooks for one event. The first matched the landed
        line and cleared the record; before it moved the watermark the second read no record,
        took the line for new text and spoke it, the third hearing of one reply."""
        from claude_code_tts import cli

        transcript = self._turn_in_progress(tmp_path, "uuid-meet")
        final = "the reply that was heard three times"
        _run_hook(transcript, "stop", last_assistant_message=final)
        _append(transcript, [_assistant_msg("m2", final), _user("next"), _tool_use("m3"), _tool_result()])

        real_match = cli._landed_match
        inner: list[str | None] = []
        state = {"nested": False}

        def match_then_let_the_other_hook_run(landed, pending):
            result = real_match(landed, pending)
            if not state["nested"]:
                state["nested"] = True
                # The other hook runs to completion between this hook's match and its take.
                inner.append(_run_hook(transcript, "post_tool_use"))
            return result

        monkeypatch.setattr("claude_code_tts.cli._landed_match", match_then_let_the_other_hook_run)
        outer = _run_hook(transcript, "post_tool_use")
        assert inner == [None] and outer is None

    def test_the_hook_that_loses_the_landed_line_still_speaks_newer_text(self, tmp_path, fake_state_dir):
        transcript = self._turn_in_progress(tmp_path, "uuid-newer")
        final = "the reply both hooks find landed"
        _run_hook(transcript, "stop", last_assistant_message=final)
        _append(transcript, [_assistant_msg("m2", final), _user("next"),
                             _assistant_msg("m3", "a newer intermediate of the next turn"), _tool_use("m3"),
                             _tool_result()])
        assert _run_hook(transcript, "post_tool_use") == "a newer intermediate of the next turn"
        assert _run_hook(transcript, "post_tool_use") is None

    def test_taking_the_landed_line_clears_the_record_and_moves_the_watermark_as_one_step(self, tmp_path):
        """The live race (2026-10-01) sat between the clear and the watermark write; they are one call
        now, under the lock, and a unit test cannot sit inside it. scripts/hook-replay.sh is the
        proof with real processes; this pins what the call does."""
        from claude_code_tts import cli

        state, lock, pending = tmp_path / "wm.state", tmp_path / "wm.lock", tmp_path / "wm.pending"
        state.write_text("926")
        cli._write_pending(pending, "the reply")
        cli._take_landed(state, lock, pending, 926, advance_past=True)
        assert not pending.exists() and state.read_text() == "927"
        assert not lock.exists(), "the lock is released"
        # A prefix left to speak keeps the watermark on the line, for the usual claim.
        cli._write_pending(pending, "the reply")
        cli._take_landed(state, lock, pending, 930, advance_past=False)
        assert state.read_text() == "930"
        # It never moves the watermark backwards.
        cli._take_landed(state, lock, pending, 100, advance_past=True)
        assert state.read_text() == "930"

    def test_twin_stops_split_across_input_and_file_speak_once_input_first(self, tmp_path, fake_state_dir):
        """14:19:50, 2026-10-01: the reply landed between the twins' reads. One spoke it from the
        input, the other found it in the file and spoke it again. One claim decides for both."""
        transcript = self._turn_in_progress(tmp_path, "uuid-split-a")
        final = "the reply that landed between the twins"
        assert _run_hook(transcript, "stop", last_assistant_message=final) == final
        _append(transcript, [_assistant_msg("m2", final)])
        assert _run_hook(transcript, "stop", last_assistant_message=final) is None

    def test_twin_stops_split_across_input_and_file_speak_once_file_first(self, tmp_path, fake_state_dir):
        transcript = self._turn_in_progress(tmp_path, "uuid-split-b")
        final = "the reply that landed between the twins"
        _append(transcript, [_assistant_msg("m2", final)])
        assert _run_hook(transcript, "stop", last_assistant_message=final) == final
        # The twin that read before the line landed: same input, nothing new in its view of the file
        # is simulated by a transcript state the first hook already covered; it must not speak.
        assert _run_hook(transcript, "stop", last_assistant_message=final) is None
        # And a later hook does not find the line again.
        _append(transcript, [_user("next"), _tool_use("m3"), _tool_result()])
        assert _run_hook(transcript, "post_tool_use") is None

    def test_a_twin_that_spoke_from_the_file_blocks_the_input_twin(self, tmp_path, fake_state_dir, monkeypatch):
        """The file-path twin runs first and claims; the input-path twin, scanning a file where the
        line is missing, must lose the claim. Simulated by hiding the landed line from the second scan."""
        from claude_code_tts import cli

        transcript = self._turn_in_progress(tmp_path, "uuid-split-c")
        final = "the reply that landed between the twins"
        _append(transcript, [_assistant_msg("m2", final)])
        assert _run_hook(transcript, "stop", last_assistant_message=final) == final
        real_scan = cli._scan_transcript

        def scan_without_the_landed_line(path, watermark, hook_type):
            found, n = real_scan(path, max(watermark - 1, 0), hook_type)
            return [e for e in found if e[2] != final], n

        monkeypatch.setattr("claude_code_tts.cli._scan_transcript", scan_without_the_landed_line)
        monkeypatch.setattr("claude_code_tts.cli._read_watermark", lambda *a: 2)
        assert _run_hook(transcript, "stop", last_assistant_message=final) is None

    def test_first_stop_of_a_transcript_speaks_the_input_not_an_older_text(self, tmp_path, fake_state_dir):
        transcript = tmp_path / "projects" / "-Users-dev" / "uuid-O.jsonl"
        _write_transcript(transcript, [_user("hi"), _assistant_msg("m0", "prose before the tool call"),
                                       _tool_use("m0"), _tool_result()])
        assert _run_hook(transcript, "stop", last_assistant_message="the final answer of the first turn") == (
            "the final answer of the first turn"
        )

    def test_without_the_input_field_the_reread_still_catches_a_late_line(self, tmp_path, fake_state_dir, monkeypatch):
        transcript = self._turn_in_progress(tmp_path, "uuid-P")
        sleeps: list[float] = []

        def late_write(seconds: float) -> None:
            sleeps.append(seconds)
            if len(sleeps) == 3:
                _append(transcript, [_assistant_msg("m2", "the answer that landed after the hook fired")])

        monkeypatch.setattr("claude_code_tts.cli.time.sleep", late_write)
        assert _run_hook(transcript, "stop") == "the answer that landed after the hook fired"

