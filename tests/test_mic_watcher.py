"""Tests for mic-aware pause (Handy log watcher)."""

import time
from pathlib import Path
from unittest.mock import MagicMock

from claude_code_tts.mic_watcher import (
    MicWatcher,
    RESUME_DELAY_MS,
    _RE_RECORDING_START,
    _RE_RECORDING_STOP,
)


# --- Regex tests ---


class TestRegexPatterns:
    def test_start_pattern_matches(self):
        line = "[2026-03-15][10:55:33][handy_app_lib::managers::audio][DEBUG] Recording started for binding transcribe"
        assert _RE_RECORDING_START.search(line)

    def test_stop_pattern_matches(self):
        line = "[2026-03-15][10:55:52][handy_app_lib::actions][DEBUG] Recording stopped and samples retrieved in 35.977584ms, sample count: 285120"
        assert _RE_RECORDING_STOP.search(line)

    def test_start_pattern_no_false_positive(self):
        line = "[2026-03-15][10:55:33][handy_app_lib::actions][DEBUG] Recording started in 23.213584ms"
        # This is the secondary "started" line — we match on "binding transcribe" specifically
        assert not _RE_RECORDING_START.search(line)

    def test_stop_pattern_no_false_positive(self):
        line = "[2026-03-15][10:55:33][handy_app_lib::actions][DEBUG] Recording completed"
        assert not _RE_RECORDING_STOP.search(line)

    def test_unrelated_line_no_match(self):
        line = "[2026-03-15][11:08:26][handy_app_lib::clipboard][INFO] Using paste method: CtrlV, delay: 60ms"
        assert not _RE_RECORDING_START.search(line)
        assert not _RE_RECORDING_STOP.search(line)


# --- MicWatcher unit tests ---


class TestMicWatcherInit:
    def test_default_resume_delay(self):
        w = MicWatcher(
            log_fn=MagicMock(),
            read_playback_state=MagicMock(),
            write_playback_state=MagicMock(),
        )
        assert w._resume_delay == RESUME_DELAY_MS / 1000.0

    def test_custom_resume_delay(self):
        w = MicWatcher(
            log_fn=MagicMock(),
            read_playback_state=MagicMock(),
            write_playback_state=MagicMock(),
            resume_delay_ms=1500,
        )
        assert w._resume_delay == 1.5

    def test_not_active_before_start(self):
        w = MicWatcher(
            log_fn=MagicMock(),
            read_playback_state=MagicMock(),
            write_playback_state=MagicMock(),
        )
        assert not w.active
        assert not w.recording


class TestMicWatcherPauseLogic:
    """Test pause/resume logic without actually tailing a file."""

    def _make_watcher(self):
        state = {"paused": False, "paused_by": None}

        def read_state():
            return dict(state)

        def write_state(**kwargs):
            for k, v in kwargs.items():
                state[k] = v

        w = MicWatcher(
            log_fn=MagicMock(),
            read_playback_state=read_state,
            write_playback_state=write_state,
        )
        return w, state

    def test_pause_for_mic(self):
        w, state = self._make_watcher()
        w._pause_for_mic()
        assert state["paused"] is True
        assert state["paused_by"] == "mic"

    def test_resume_after_mic(self):
        w, state = self._make_watcher()
        w._pause_for_mic()
        w._resume_after_mic()
        assert state["paused"] is False
        assert state["paused_by"] is None

    def test_mic_does_not_override_manual_pause(self):
        w, state = self._make_watcher()
        # User manually pauses
        state["paused"] = True
        state["paused_by"] = "user"
        # Mic tries to pause — should be a no-op (already paused)
        w._pause_for_mic()
        assert state["paused_by"] == "user"

    def test_mic_does_not_unpause_manual(self):
        w, state = self._make_watcher()
        # User manually pauses
        state["paused"] = True
        state["paused_by"] = "user"
        # Mic tries to resume — should stay paused
        w._resume_after_mic()
        assert state["paused"] is True
        assert state["paused_by"] == "user"

    def test_resume_when_already_unpaused(self):
        w, state = self._make_watcher()
        # Not paused, mic resume is a no-op
        w._resume_after_mic()
        assert state["paused"] is False


class TestMicWatcherStartStop:
    """Test start/stop with a real temp file."""

    def test_start_fails_without_log_file(self, monkeypatch):
        monkeypatch.setattr(
            "claude_code_tts.mic_watcher.HANDY_LOG",
            Path("/nonexistent/handy.log"),
        )
        w = MicWatcher(
            log_fn=MagicMock(),
            read_playback_state=MagicMock(return_value={}),
            write_playback_state=MagicMock(),
        )
        assert not w.start()
        assert not w.active

    def test_start_succeeds_with_log_file(self, tmp_path, monkeypatch):
        log_file = tmp_path / "handy.log"
        log_file.write_text("")
        monkeypatch.setattr("claude_code_tts.mic_watcher.HANDY_LOG", log_file)

        w = MicWatcher(
            log_fn=MagicMock(),
            read_playback_state=MagicMock(return_value={}),
            write_playback_state=MagicMock(),
        )
        assert w.start()
        assert w.active
        w.stop()
        assert not w.active


class TestMicWatcherIntegration:
    """Test the full watcher with a real file being tailed."""

    def test_recording_cycle_pauses_and_resumes(self, tmp_path, monkeypatch):
        log_file = tmp_path / "handy.log"
        log_file.write_text("")
        monkeypatch.setattr("claude_code_tts.mic_watcher.HANDY_LOG", log_file)

        state = {"paused": False, "paused_by": None}

        def read_state():
            return dict(state)

        def write_state(**kwargs):
            for k, v in kwargs.items():
                state[k] = v

        w = MicWatcher(
            log_fn=MagicMock(),
            read_playback_state=read_state,
            write_playback_state=write_state,
            resume_delay_ms=50,  # Short delay for tests
        )
        w.start()
        time.sleep(0.1)  # Let thread start

        # Simulate recording start
        with open(log_file, "a") as f:
            f.write(
                "[2026-03-15][10:55:33][handy_app_lib::managers::audio][DEBUG] "
                "Recording started for binding transcribe\n"
            )
            f.flush()

        time.sleep(0.2)
        assert state["paused"] is True
        assert state["paused_by"] == "mic"
        assert w.recording

        # Simulate recording stop
        with open(log_file, "a") as f:
            f.write(
                "[2026-03-15][10:55:52][handy_app_lib::actions][DEBUG] "
                "Recording stopped and samples retrieved in 35ms, sample count: 285120\n"
            )
            f.flush()

        time.sleep(0.3)  # > resume_delay_ms (50ms)
        assert state["paused"] is False
        assert state["paused_by"] is None
        assert not w.recording

        w.stop()

    def test_manual_pause_survives_recording_cycle(self, tmp_path, monkeypatch):
        log_file = tmp_path / "handy.log"
        log_file.write_text("")
        monkeypatch.setattr("claude_code_tts.mic_watcher.HANDY_LOG", log_file)

        state = {"paused": True, "paused_by": "user"}

        def read_state():
            return dict(state)

        def write_state(**kwargs):
            for k, v in kwargs.items():
                state[k] = v

        w = MicWatcher(
            log_fn=MagicMock(),
            read_playback_state=read_state,
            write_playback_state=write_state,
            resume_delay_ms=50,
        )
        w.start()
        time.sleep(0.1)

        # Recording start — already paused by user, should stay as user
        with open(log_file, "a") as f:
            f.write(
                "[2026-03-15][10:55:33][handy_app_lib::managers::audio][DEBUG] "
                "Recording started for binding transcribe\n"
            )
            f.flush()

        time.sleep(0.2)
        assert state["paused"] is True
        assert state["paused_by"] == "user"

        # Recording stop — should NOT unpause because user paused
        with open(log_file, "a") as f:
            f.write(
                "[2026-03-15][10:55:52][handy_app_lib::actions][DEBUG] "
                "Recording stopped and samples retrieved in 35ms, sample count: 285120\n"
            )
            f.flush()

        time.sleep(0.3)
        assert state["paused"] is True
        assert state["paused_by"] == "user"

        w.stop()
