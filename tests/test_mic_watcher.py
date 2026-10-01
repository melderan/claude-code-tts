"""Tests for mic-aware pause (Handy log watcher)."""

import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import claude_code_tts.mic_watcher as mw
from claude_code_tts.mic_watcher import (
    _RE_RECORDING_START,
    _RE_RECORDING_STOP,
    RESUME_DELAY_MS,
    MicWatcher,
    handy_file_log_level,
    handy_log_level_hides_recording,
    log_line_age_s,
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

    def test_cancel_counts_as_stop(self):
        """Escape during a dictation: Handy logs the cancellation, never a stop line."""
        line = "[2026-09-30][19:19:12][handy_app_lib::utils][INFO] Initiating operation cancellation..."
        assert _RE_RECORDING_STOP.search(line)
        assert not _RE_RECORDING_START.search(line)

    def test_cancel_completed_line_is_not_a_second_stop(self):
        line = "[2026-09-30][19:19:12][handy_app_lib::utils][INFO] Operation cancellation completed - returned to idle state"
        assert not _RE_RECORDING_STOP.search(line)

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


class TestCheckInitialMicState:
    """Test log-tail scanning for current mic state on startup."""

    def _make_watcher(self, log_file, monkeypatch):
        monkeypatch.setattr("claude_code_tts.mic_watcher.HANDY_LOG", log_file)
        return MicWatcher(
            log_fn=MagicMock(),
            read_playback_state=MagicMock(return_value={}),
            write_playback_state=MagicMock(),
        )

    def test_mic_currently_recording(self, tmp_path, monkeypatch):
        """Last event is recording start -> mic is open."""
        log_file = tmp_path / "handy.log"
        log_file.write_text(
            "[10:55:00][DEBUG] Some unrelated log line\n"
            "[10:55:33][handy_app_lib::managers::audio][DEBUG] Recording started for binding transcribe\n"
        )
        w = self._make_watcher(log_file, monkeypatch)
        assert w._check_initial_mic_state() is True

    def test_mic_not_recording(self, tmp_path, monkeypatch):
        """Last event is recording stop -> mic is closed."""
        log_file = tmp_path / "handy.log"
        log_file.write_text(
            "[10:55:33][handy_app_lib::managers::audio][DEBUG] Recording started for binding transcribe\n"
            "[10:55:52][handy_app_lib::actions][DEBUG] Recording stopped and samples retrieved in 35ms\n"
        )
        w = self._make_watcher(log_file, monkeypatch)
        assert w._check_initial_mic_state() is False

    def test_start_then_cancel_is_not_recording(self, tmp_path, monkeypatch):
        """A daemon started after a cancelled dictation must not pause on the orphan start."""
        log_file = tmp_path / "handy.log"
        log_file.write_text(
            "[19:18:59][handy_app_lib::actions][DEBUG] TranscribeAction::start called for binding: transcribe\n"
            "[19:19:12][handy_app_lib::utils][INFO] Initiating operation cancellation...\n"
            "[19:19:12][handy_app_lib::utils][INFO] Operation cancellation completed - returned to idle state\n"
        )
        w = self._make_watcher(log_file, monkeypatch)
        assert w._check_initial_mic_state() is False

    def _stamp(self, age_s: float, utc: bool) -> str:
        from datetime import datetime, timedelta, timezone

        t = datetime.now(timezone.utc) - timedelta(seconds=age_s)
        if not utc:
            t = t.astimezone()
        return t.strftime("[%Y-%m-%d][%H:%M:%S]")

    def test_old_start_with_no_stop_is_over(self, tmp_path, monkeypatch):
        """2026-09-30 19:18:59: one orphan start paused two restarted daemons, 3 and 17 min later."""
        log_file = tmp_path / "handy.log"
        log_file.write_text(
            f"{self._stamp(17 * 60, utc=False)}[handy_app_lib::actions][DEBUG] TranscribeAction::start called for binding: transcribe\n"
            f"{self._stamp(17 * 60 - 1, utc=False)}[handy_app_lib::actions][DEBUG] Microphone mode - always_on: false\n"
        )
        w = self._make_watcher(log_file, monkeypatch)
        assert w._check_initial_mic_state() is False
        assert "treating that recording as over" in str(w._log.call_args_list)

    def test_recent_start_with_no_stop_is_recording_whatever_clock_handy_uses(self, tmp_path, monkeypatch):
        for utc in (True, False):
            log_file = tmp_path / "handy.log"
            log_file.write_text(
                f"{self._stamp(20, utc=utc)}[handy_app_lib::actions][DEBUG] TranscribeAction::start called for binding: transcribe\n"
            )
            w = self._make_watcher(log_file, monkeypatch)
            assert w._check_initial_mic_state() is True, f"utc={utc}"

    def test_a_start_buried_under_two_minutes_of_debug_chatter_is_found(self, tmp_path, monkeypatch):
        """2026-10-01: a daemon restarted 139 s into a dictation read an 8 KB tail, saw no start,
        and spoke over the person dictating. The walk goes back by time, not by a byte count."""
        from datetime import datetime, timedelta

        start_at = datetime.now() - timedelta(seconds=139)
        stamp = start_at.strftime("[%Y-%m-%d][%H:%M:%S]")
        chatter = "".join(
            f"{stamp}[handy_app_lib::audio_toolkit::vad][DEBUG] frame {i} energy 0.0123 speech=true\n"
            for i in range(600)
        )  # about 50 KB after the start line
        log_file = tmp_path / "handy.log"
        log_file.write_text(
            f"{stamp}[handy_app_lib::managers::audio][DEBUG] Recording started for binding transcribe\n" + chatter
        )
        assert log_file.stat().st_size > 8192 * 4
        w = self._make_watcher(log_file, monkeypatch)
        assert w._check_initial_mic_state() is True

    def test_an_old_start_far_back_still_does_not_count(self, tmp_path, monkeypatch):
        """However big the file, an old start with no stop is over by its age."""
        log_file = tmp_path / "handy.log"
        log_file.write_text(
            "[2020-01-01][10:00:00][handy_app_lib::managers::audio][DEBUG] Recording started for binding transcribe\n"
            + "".join(f"[2020-01-01][10:00:01][x][DEBUG] old chatter {i}\n" for i in range(50))
        )
        w = self._make_watcher(log_file, monkeypatch)
        assert w._check_initial_mic_state() is False

    def test_stale_start_limit_zero_disables_the_age_check(self, tmp_path, monkeypatch):
        log_file = tmp_path / "handy.log"
        log_file.write_text(
            f"{self._stamp(3600, utc=False)}[handy_app_lib::actions][DEBUG] TranscribeAction::start called for binding: transcribe\n"
        )
        monkeypatch.setattr("claude_code_tts.mic_watcher.HANDY_LOG", log_file)
        w = MicWatcher(
            log_fn=MagicMock(),
            read_playback_state=MagicMock(return_value={}),
            write_playback_state=MagicMock(),
            stale_start_s=0,
        )
        assert w._check_initial_mic_state() is True

    def test_start_without_a_timestamp_still_counts(self, tmp_path, monkeypatch):
        """The old fixtures have no date; a line the clock cannot read keeps the old behaviour."""
        log_file = tmp_path / "handy.log"
        log_file.write_text("[10:55:33][handy_app_lib::managers::audio][DEBUG] Recording started for binding transcribe\n")
        w = self._make_watcher(log_file, monkeypatch)
        assert w._check_initial_mic_state() is True

    def test_no_recording_events(self, tmp_path, monkeypatch):
        """No recording events in log -> not recording."""
        log_file = tmp_path / "handy.log"
        log_file.write_text(
            "[10:55:00][DEBUG] App started\n"
            "[10:55:01][DEBUG] Model loaded\n"
        )
        w = self._make_watcher(log_file, monkeypatch)
        assert w._check_initial_mic_state() is False

    def test_empty_log(self, tmp_path, monkeypatch):
        """Empty log -> not recording."""
        log_file = tmp_path / "handy.log"
        log_file.write_text("")
        w = self._make_watcher(log_file, monkeypatch)
        assert w._check_initial_mic_state() is False

    def test_missing_log(self, tmp_path, monkeypatch):
        """Missing log -> not recording."""
        log_file = tmp_path / "nonexistent.log"
        w = self._make_watcher(log_file, monkeypatch)
        assert w._check_initial_mic_state() is False

    def test_multiple_cycles_last_is_start(self, tmp_path, monkeypatch):
        """Multiple start/stop cycles, last event is start -> recording."""
        log_file = tmp_path / "handy.log"
        log_file.write_text(
            "[10:55:33][handy_app_lib::managers::audio][DEBUG] Recording started for binding transcribe\n"
            "[10:55:52][handy_app_lib::actions][DEBUG] Recording stopped and samples retrieved\n"
            "[10:56:10][handy_app_lib::managers::audio][DEBUG] Recording started for binding transcribe\n"
            "[10:56:25][handy_app_lib::actions][DEBUG] Recording stopped and samples retrieved\n"
            "[10:57:00][handy_app_lib::managers::audio][DEBUG] Recording started for binding transcribe\n"
        )
        w = self._make_watcher(log_file, monkeypatch)
        assert w._check_initial_mic_state() is True

    def test_multiple_cycles_last_is_stop(self, tmp_path, monkeypatch):
        """Multiple start/stop cycles, last event is stop -> not recording."""
        log_file = tmp_path / "handy.log"
        log_file.write_text(
            "[10:55:33][handy_app_lib::managers::audio][DEBUG] Recording started for binding transcribe\n"
            "[10:55:52][handy_app_lib::actions][DEBUG] Recording stopped and samples retrieved\n"
            "[10:56:10][handy_app_lib::managers::audio][DEBUG] Recording started for binding transcribe\n"
            "[10:56:25][handy_app_lib::actions][DEBUG] Recording stopped and samples retrieved\n"
        )
        w = self._make_watcher(log_file, monkeypatch)
        assert w._check_initial_mic_state() is False


class TestStartPausesIfMicOpen:
    """Test that start() pauses the daemon if mic is currently recording."""

    def test_start_pauses_when_mic_open(self, tmp_path, monkeypatch):
        log_file = tmp_path / "handy.log"
        log_file.write_text(
            "[10:57:00][handy_app_lib::managers::audio][DEBUG] Recording started for binding transcribe\n"
        )
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
        )
        w.start()
        # Mic was open -> should be paused immediately
        assert state["paused"] is True
        assert state["paused_by"] == "mic"
        assert w.recording is True
        w.stop()

    def test_a_carried_mic_hold_starts_as_recording_and_the_stop_line_releases_it(self, tmp_path, monkeypatch):
        """The daemon kept the previous daemon's mic hold; the watcher starts as recording even
        though the log tail shows nothing, and Handy's stop line resumes the queue."""
        log_file = tmp_path / "handy.log"
        log_file.write_text("[10:57:00][x][DEBUG] nothing about a recording here\n")
        monkeypatch.setattr("claude_code_tts.mic_watcher.HANDY_LOG", log_file)
        state = {"paused": True, "paused_by": "mic"}

        def read_state():
            return dict(state)

        def write_state(**kwargs):
            state.update(kwargs)

        w = MicWatcher(log_fn=MagicMock(), read_playback_state=read_state, write_playback_state=write_state,
                       resume_delay_ms=10)
        w.start(carried_recording=True)
        try:
            assert w.recording is True and state["paused"] is True
            time.sleep(0.2)
            with log_file.open("a") as f:
                f.write("[10:59:10][handy_app_lib::actions][DEBUG] Recording stopped and samples retrieved\n")
            for _ in range(100):
                if not state["paused"]:
                    break
                time.sleep(0.02)
            assert state["paused"] is False and w.recording is False
        finally:
            w.stop()

    def test_start_does_not_pause_when_mic_closed(self, tmp_path, monkeypatch):
        log_file = tmp_path / "handy.log"
        log_file.write_text(
            "[10:55:33][handy_app_lib::managers::audio][DEBUG] Recording started for binding transcribe\n"
            "[10:55:52][handy_app_lib::actions][DEBUG] Recording stopped and samples retrieved\n"
        )
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
        )
        w.start()
        assert state["paused"] is False
        assert w.recording is False
        w.stop()


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



class TestHandy097Patterns:
    """Handy 0.9.7 logs recording events from TranscribeAction at DEBUG level."""

    def test_new_start_line_matches(self):
        line = "[2026-09-22][11:36:02][handy_app_lib::actions][DEBUG] TranscribeAction::start called for binding: transcribe"
        assert _RE_RECORDING_START.search(line)

    def test_new_stop_line_matches(self):
        line = "[2026-09-22][11:36:05][handy_app_lib::actions][DEBUG] TranscribeAction::stop called for binding: transcribe"
        assert _RE_RECORDING_STOP.search(line)

    def test_empty_recording_counts_as_stop(self):
        assert _RE_RECORDING_STOP.search("[DEBUG] Recording produced no audio samples; skipping persistence")
        assert _RE_RECORDING_STOP.search("[DEBUG] No samples retrieved from recording stop")

    def test_post_process_binding_also_pauses(self):
        line = "[DEBUG] TranscribeAction::start called for binding: transcribe_with_post_process"
        assert _RE_RECORDING_START.search(line)

    def test_duplicate_stop_lines_do_not_block_the_tail(self, tmp_path, monkeypatch):
        """After the first stop line resumed us, later stop lines must not wait again."""
        log_file = tmp_path / "handy.log"
        log_file.write_text("")
        monkeypatch.setattr(mw, "HANDY_LOG", log_file)
        monkeypatch.setattr(mw, "HANDY_SETTINGS", tmp_path / "missing.json")
        state = {"paused": False}
        writes = []
        logs = []

        def write_state(**kw):
            writes.append(kw)
            state.update({k: v for k, v in kw.items() if k in ("paused", "paused_by")})

        w = mw.MicWatcher(
            log_fn=lambda msg, *a, **k: logs.append(msg),
            read_playback_state=lambda: dict(state),
            write_playback_state=write_state,
            resume_delay_ms=50,
        )
        assert w.start()
        try:
            # The tail seeks to the end when its thread opens the file; lines written
            # before that would be skipped. Wait for the watcher to say it is tailing.
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not any("tailing" in m for m in logs):
                time.sleep(0.01)
            assert any("tailing" in m for m in logs)
            with log_file.open("a") as f:
                f.write("[DEBUG] TranscribeAction::start called for binding: transcribe\n")
                f.write("[DEBUG] TranscribeAction::stop called for binding: transcribe\n")
                f.write("[DEBUG] Recording stopped and samples retrieved in 30ms, sample count: 1\n")
                f.write("[DEBUG] No samples retrieved from recording stop\n")

            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and len(writes) < 2:
                time.sleep(0.02)
        finally:
            w.stop()
        assert writes[0] == {"paused": True, "paused_by": "mic"}
        assert writes[1] == {"paused": False, "paused_by": None}
        assert len(writes) == 2


class TestHandyLogLevel:
    def test_string_levels(self):
        from claude_code_tts.mic_watcher import (
            handy_file_log_level,
        )

        assert handy_file_log_level({"log_level": "Info"}) == "info"
        assert handy_log_level_hides_recording("info") is True
        assert handy_log_level_hides_recording("debug") is False
        assert handy_log_level_hides_recording("trace") is False

    def test_legacy_numeric_levels(self):
        assert handy_file_log_level({"log_level": 2}) == "debug"
        assert handy_file_log_level({"log_level": 3}) == "info"
        assert handy_file_log_level({"log_level": 9}) is None
        assert handy_file_log_level({}) is None

    def test_reads_tauri_store_shape(self, tmp_path, monkeypatch):
        store = tmp_path / "settings_store.json"
        store.write_text(json.dumps({"settings": {"log_level": "info", "mute_while_recording": True}}))
        monkeypatch.setattr(mw, "HANDY_SETTINGS", store)
        assert mw.handy_file_log_level() == "info"
        assert mw.handy_settings()["mute_while_recording"] is True

    def test_start_warns_when_level_hides_recording(self, tmp_path, monkeypatch):
        log_file = tmp_path / "handy.log"
        log_file.write_text("")
        store = tmp_path / "settings_store.json"
        store.write_text(json.dumps({"settings": {"log_level": "info"}}))
        monkeypatch.setattr(mw, "HANDY_LOG", log_file)
        monkeypatch.setattr(mw, "HANDY_SETTINGS", store)
        logs = []
        w = mw.MicWatcher(
            log_fn=lambda msg, level="INFO": logs.append((level, msg)),
            read_playback_state=lambda: {"paused": False},
            write_playback_state=lambda **kw: None,
        )
        assert w.start()
        w.stop()
        warns = [m for lvl, m in logs if lvl == "WARN"]
        assert any("Log Level > Debug" in m for m in warns)


class TestWatchLoopIsALoop:
    """Reopening after a rotation or an error must not add a stack frame (9.33.2)."""

    def test_reopen_runs_in_place_until_stopped(self, monkeypatch):
        w = MicWatcher(
            log_fn=MagicMock(),
            read_playback_state=lambda: {"paused": False},
            write_playback_state=lambda **kw: None,
        )
        calls = {"n": 0}

        def once() -> None:
            calls["n"] += 1
            if calls["n"] == 3:
                w._stop_event.set()

        monkeypatch.setattr(w, "_watch_once", once)
        monkeypatch.setattr(mw.time, "sleep", lambda s: None)
        w._watch_loop()
        assert calls["n"] == 3


class TestRecordingThatNeverBegan:
    """Handy logs "start called" and returns early when the microphone or model is missing.

    src-tauri/src/actions.rs (read 2026-09-30): "Failed to start recording: {}" and "Not starting
    recording: no model can transcribe it" are the two early returns after the start line, and
    neither path ever logs a stop. On 2026-09-30 at 08:12Z one of them held the queue for six
    minutes until the daemon was restarted by hand.
    """

    @pytest.mark.parametrize(
        "line",
        [
            "[2026-09-30][01:12:20][handy_app_lib::actions][ERROR] Failed to start recording: device busy",
            "[2026-09-30][01:12:20][handy_app_lib::actions][WARN] Not starting recording: no model can transcribe it (en)",
        ],
    )
    def test_start_failure_counts_as_stop(self, line):
        assert _RE_RECORDING_STOP.search(line)
        assert not _RE_RECORDING_START.search(line)

    def test_failed_start_releases_the_hold(self, tmp_path, monkeypatch):
        log_file = tmp_path / "handy.log"
        log_file.write_text("")
        monkeypatch.setattr("claude_code_tts.mic_watcher.HANDY_LOG", log_file)
        state = {"paused": False, "paused_by": None}

        def write_state(**kwargs):
            state.update(kwargs)

        w = MicWatcher(
            log_fn=MagicMock(),
            read_playback_state=lambda: dict(state),
            write_playback_state=write_state,
            resume_delay_ms=50,
        )
        w.start()
        time.sleep(0.1)
        with open(log_file, "a") as f:
            f.write("[DEBUG] TranscribeAction::start called for binding: transcribe\n")
        time.sleep(0.2)
        assert state["paused"] is True and state["paused_by"] == "mic"
        with open(log_file, "a") as f:
            f.write("[ERROR] Failed to start recording: no input device\n")
        time.sleep(0.3)
        assert state["paused"] is False and state["paused_by"] is None
        w.stop()


class TestLogLineAge:
    def test_reads_utc_and_local_and_keeps_the_smaller(self):
        now = 1_790_821_139.0  # 2026-10-01 02:18:59Z, 2026-09-30 19:18:59 in UTC-7
        assert log_line_age_s("[2026-10-01][02:18:59][x][DEBUG] start", now=now) == 0.0
        age = log_line_age_s("[2026-10-01][02:00:59][x][DEBUG] start", now=now)
        assert age == 18 * 60

    def test_no_timestamp_is_none(self):
        assert log_line_age_s("[10:55:33][x][DEBUG] start") is None
        assert log_line_age_s("plain line") is None

    def test_bad_date_is_none(self):
        assert log_line_age_s("[2026-13-45][99:00:00][x] start") is None
