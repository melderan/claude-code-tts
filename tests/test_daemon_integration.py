"""Integration tests for daemon playback and position-aware resume.

Tests daemon_play_audio() with a real subprocess (fake player),
real WAV files, and real playback state on disk. Exercises the full
pause -> position tracking -> resume -> trim -> skip-near-end flow.
"""

import json
import signal
import stat
import threading
import time
import wave
from pathlib import Path
from unittest.mock import patch

import pytest

import claude_code_tts.daemon as daemon_mod
from claude_code_tts.daemon import (
    NEAR_END_THRESHOLD,
    REWIND_REAL_SECONDS,
    calculate_audio_position,
    daemon_play_audio,
    get_wav_duration,
    read_playback_state,
    rewind_amount,
    trim_wav,
    write_playback_state,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_wav(path: Path, duration_seconds: float, sample_rate: int = 22050) -> Path:
    """Create a WAV file with silence of the given duration."""
    n_frames = int(duration_seconds * sample_rate)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(b"\x00\x00" * n_frames)
    return path


def make_fake_player(tmp_path: Path, duration: float = 10.0) -> Path:
    """Create a fake audio player script that sleeps for a fixed duration.

    Accepts and ignores all arguments (like afplay would receive).
    """
    script = tmp_path / "fake-player"
    script.write_text(f"#!/bin/bash\nsleep {duration}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def daemon_env(tmp_path):
    """Set up isolated daemon state directory and fake player."""
    state_dir = tmp_path / ".claude-tts"
    state_dir.mkdir()
    queue_dir = state_dir / "queue"
    queue_dir.mkdir()

    playback_file = state_dir / "playback.json"
    heartbeat_file = state_dir / "daemon.heartbeat"
    log_file = state_dir / "daemon.log"

    pid_file = state_dir / "daemon.pid"
    lock_file = state_dir / "daemon.lock"
    version_file = state_dir / "daemon.version"
    respawn_marker = state_dir / "daemon.respawn"

    with patch.object(daemon_mod, "PLAYBACK_STATE_FILE", playback_file), \
         patch.object(daemon_mod, "HEARTBEAT_FILE", heartbeat_file), \
         patch.object(daemon_mod, "LOG_FILE", log_file), \
         patch.object(daemon_mod, "TTS_QUEUE_DIR", queue_dir), \
         patch.object(daemon_mod, "PID_FILE", pid_file), \
         patch.object(daemon_mod, "LOCK_FILE", lock_file), \
         patch.object(daemon_mod, "VERSION_FILE", version_file), \
         patch.object(daemon_mod, "RESPAWN_MARKER", respawn_marker):
        yield {
            "state_dir": state_dir,
            "queue_dir": queue_dir,
            "playback_file": playback_file,
            "heartbeat_file": heartbeat_file,
            "log_file": log_file,
            "respawn_marker": respawn_marker,
            "tmp_path": tmp_path,
        }


# ---------------------------------------------------------------------------
# daemon_play_audio integration tests
# ---------------------------------------------------------------------------


class TestDaemonPlayAudioIntegration:
    """Test daemon_play_audio with a real subprocess."""

    def test_normal_playback_returns_elapsed(self, daemon_env):
        """Normal playback completes and returns elapsed time."""
        tmp = daemon_env["tmp_path"]
        wav = make_wav(tmp / "test.wav", 5.0)
        fake = make_fake_player(tmp, duration=0.3)

        with patch.object(daemon_mod, "detect_player", return_value=[str(fake)]):
            success, was_killed, elapsed = daemon_play_audio(wav, speed=1.0)

        assert success is True
        assert was_killed is False
        assert 0.2 <= elapsed <= 1.0  # ~0.3s sleep with tolerance

    def test_pause_kills_and_returns_elapsed(self, daemon_env):
        """Setting paused=True kills the player and returns elapsed time."""
        tmp = daemon_env["tmp_path"]
        wav = make_wav(tmp / "test.wav", 5.0)
        fake = make_fake_player(tmp, duration=10.0)

        def pause_after_delay():
            time.sleep(0.3)
            write_playback_state(paused=True, paused_by="mic")

        pause_thread = threading.Thread(target=pause_after_delay)

        with patch.object(daemon_mod, "detect_player", return_value=[str(fake)]):
            pause_thread.start()
            success, was_killed, elapsed = daemon_play_audio(wav, speed=1.0)
            pause_thread.join()

        assert success is False
        assert was_killed is True
        assert 0.2 <= elapsed <= 1.0  # ~0.3s before pause triggered

        # Clean up pause state
        write_playback_state(paused=False, paused_by=None)

    def test_audio_pid_tracked_during_playback(self, daemon_env):
        """audio_pid is set while playing and cleared after."""
        tmp = daemon_env["tmp_path"]
        wav = make_wav(tmp / "test.wav", 5.0)
        fake = make_fake_player(tmp, duration=0.5)

        pids_seen = []

        def capture_pid():
            time.sleep(0.1)
            state = read_playback_state()
            pids_seen.append(state.get("audio_pid"))

        spy = threading.Thread(target=capture_pid)

        with patch.object(daemon_mod, "detect_player", return_value=[str(fake)]):
            spy.start()
            daemon_play_audio(wav, speed=1.0)
            spy.join()

        # Should have captured a real PID during playback
        assert len(pids_seen) == 1
        assert pids_seen[0] is not None
        assert isinstance(pids_seen[0], int)

        # After completion, PID should be cleared
        state = read_playback_state()
        assert state.get("audio_pid") is None

    def test_elapsed_scales_with_real_time(self, daemon_env):
        """Elapsed time reflects actual wall clock, not audio duration."""
        tmp = daemon_env["tmp_path"]
        wav = make_wav(tmp / "test.wav", 30.0)  # 30s WAV
        fake = make_fake_player(tmp, duration=0.5)  # But player runs 0.5s

        with patch.object(daemon_mod, "detect_player", return_value=[str(fake)]):
            _, _, elapsed = daemon_play_audio(wav, speed=1.0)

        # Elapsed should be ~0.5s (real time), not 30s (WAV duration)
        assert 0.3 <= elapsed <= 1.5


# ---------------------------------------------------------------------------
# Position tracking integration
# ---------------------------------------------------------------------------


class TestPositionTrackingIntegration:
    """Test that audio position is calculated and stored correctly."""

    def test_position_stored_on_pause(self, daemon_env):
        """When paused, audio_position should be stored in current_message."""
        tmp = daemon_env["tmp_path"]
        wav = make_wav(tmp / "test.wav", 30.0)
        fake = make_fake_player(tmp, duration=10.0)

        # Simulate what the daemon loop does:
        # 1. Write current_message before playing
        msg_info = {
            "session_id": "test",
            "project": "test-project",
            "text": "Hello world, this is a test message.",
            "persona": "claude-prime",
            "speed": 2.0,
            "speed_method": "playback",
            "voice_kokoro": "",
            "voice_kokoro_blend": "",
        }
        write_playback_state(current_message=msg_info)

        def pause_after_delay():
            time.sleep(0.4)
            write_playback_state(paused=True, paused_by="user")

        pause_thread = threading.Thread(target=pause_after_delay)

        with patch.object(daemon_mod, "detect_player", return_value=[str(fake)]):
            pause_thread.start()
            _, was_killed, elapsed = daemon_play_audio(wav, speed=2.0)
            pause_thread.join()

        assert was_killed is True

        # Calculate position like daemon does
        audio_pos = calculate_audio_position(elapsed, 2.0, "playback")
        msg_info["audio_position"] = audio_pos
        write_playback_state(current_message=msg_info)

        # Verify stored state
        state = read_playback_state()
        stored_msg = state["current_message"]
        assert "audio_position" in stored_msg
        # At 2x speed, ~0.4s real = ~0.8s audio position
        assert stored_msg["audio_position"] > 0.0

        # Clean up
        write_playback_state(paused=False, paused_by=None, current_message=None)

    def test_position_accumulates_across_pauses(self, daemon_env):
        """Multiple pause/resume cycles accumulate position correctly."""
        tmp = daemon_env["tmp_path"]
        wav = make_wav(tmp / "test.wav", 60.0)
        fake = make_fake_player(tmp, duration=10.0)
        speed = 2.0
        speed_method = "playback"

        # First playback: pause after ~0.3s
        def pause_1():
            time.sleep(0.3)
            write_playback_state(paused=True, paused_by="mic")

        t1 = threading.Thread(target=pause_1)
        with patch.object(daemon_mod, "detect_player", return_value=[str(fake)]):
            t1.start()
            _, was_killed, elapsed_1 = daemon_play_audio(wav, speed=speed)
            t1.join()

        assert was_killed is True
        pos_1 = calculate_audio_position(elapsed_1, speed, speed_method)
        assert pos_1 > 0

        # Simulate resume: trim and play from (pos_1 - REWIND)
        resume_from = max(0.0, pos_1 - REWIND_REAL_SECONDS)
        write_playback_state(paused=False, paused_by=None)

        # Second playback: pause after ~0.3s
        def pause_2():
            time.sleep(0.3)
            write_playback_state(paused=True, paused_by="mic")

        t2 = threading.Thread(target=pause_2)
        with patch.object(daemon_mod, "detect_player", return_value=[str(fake)]):
            t2.start()
            _, was_killed_2, elapsed_2 = daemon_play_audio(wav, speed=speed)
            t2.join()

        assert was_killed_2 is True
        pos_2 = resume_from + calculate_audio_position(elapsed_2, speed, speed_method)

        # Position should have advanced
        assert pos_2 > pos_1 * 0.5  # At least somewhere past half of first pos
        # And both positions should be positive
        assert pos_1 > 0
        assert pos_2 > 0

        write_playback_state(paused=False, paused_by=None, current_message=None)


# ---------------------------------------------------------------------------
# Resume flow integration (WAV trimming)
# ---------------------------------------------------------------------------


class TestResumeFlowIntegration:
    """Test the full resume flow: regenerate WAV, check position, trim, play."""

    def test_resume_trims_wav(self, daemon_env):
        """On resume, the WAV is trimmed to resume_from position."""
        tmp = daemon_env["tmp_path"]
        full_wav = make_wav(tmp / "full.wav", 30.0)
        full_duration = get_wav_duration(full_wav)

        # Simulate: interrupted at 20s audio position
        audio_position = 20.0
        resume_from = max(0.0, audio_position - REWIND_REAL_SECONDS)  # 15.0

        trimmed = tmp / "trimmed.wav"
        assert trim_wav(full_wav, trimmed, resume_from) is True

        trimmed_duration = get_wav_duration(trimmed)
        expected = full_duration - resume_from  # 15.0
        assert abs(trimmed_duration - expected) < 0.1

    def test_resume_from_early_position_plays_from_start(self, daemon_env):
        """If interrupted early (< REWIND_REAL_SECONDS), play from start."""
        tmp = daemon_env["tmp_path"]
        full_wav = make_wav(tmp / "full.wav", 30.0)

        audio_position = 3.0  # Less than REWIND_REAL_SECONDS (5.0)
        resume_from = max(0.0, audio_position - REWIND_REAL_SECONDS)

        assert resume_from == 0.0  # Should play from beginning

    def test_near_end_skips_replay(self, daemon_env):
        """If interrupted near the end, skip replay entirely."""
        tmp = daemon_env["tmp_path"]
        wav = make_wav(tmp / "test.wav", 30.0)
        wav_duration = get_wav_duration(wav)

        # Interrupted at 29s of a 30s clip
        audio_position = 29.0
        remaining = wav_duration - audio_position

        assert remaining <= NEAR_END_THRESHOLD
        # Daemon would skip replay here

    def test_near_end_with_zero_position_still_plays(self, daemon_env):
        """First interruption (position=0) should always replay, never skip."""
        wav_duration = 30.0
        audio_position = 0.0

        # The daemon checks: if prev_audio_pos > 0 and remaining <= threshold
        # With position 0, it should NOT skip
        should_skip = audio_position > 0 and (wav_duration - audio_position) <= NEAR_END_THRESHOLD
        assert should_skip is False

    def test_trim_and_play_integration(self, daemon_env):
        """Full cycle: create WAV, trim, play trimmed version."""
        tmp = daemon_env["tmp_path"]
        full_wav = make_wav(tmp / "full.wav", 10.0, sample_rate=22050)
        trimmed_wav = tmp / "trimmed.wav"
        fake = make_fake_player(tmp, duration=0.2)

        # Trim from 5s mark
        assert trim_wav(full_wav, trimmed_wav, 5.0) is True

        # Play the trimmed file
        with patch.object(daemon_mod, "detect_player", return_value=[str(fake)]):
            success, was_killed, elapsed = daemon_play_audio(trimmed_wav)

        assert success is True
        assert was_killed is False

        # Verify trimmed WAV is ~5s
        assert abs(get_wav_duration(trimmed_wav) - 5.0) < 0.1


# ---------------------------------------------------------------------------
# Ghost replay prevention
# ---------------------------------------------------------------------------


class TestGhostReplayPrevention:
    """Test scenarios where audio finishes but pause triggers immediately after."""

    def test_completed_audio_not_replayed(self, daemon_env):
        """Audio that completed naturally shouldn't be replayed on resume."""
        tmp = daemon_env["tmp_path"]
        wav = make_wav(tmp / "test.wav", 5.0)
        fake = make_fake_player(tmp, duration=0.3)

        # Play to completion
        with patch.object(daemon_mod, "detect_player", return_value=[str(fake)]):
            success, was_killed, elapsed = daemon_play_audio(wav)

        assert success is True
        assert was_killed is False

        # Now check: if someone pauses AFTER completion,
        # was_killed=False means the daemon won't store current_message
        # So there's nothing to replay

    def test_near_end_interruption_skipped(self, daemon_env):
        """Interruption with only 1s left should be skipped."""
        wav_duration = 30.0
        audio_position = 29.5  # Only 0.5s remaining

        remaining = wav_duration - audio_position
        assert remaining <= NEAR_END_THRESHOLD  # 2.0

        # This is the skip condition
        should_skip = audio_position > 0 and remaining <= NEAR_END_THRESHOLD
        assert should_skip is True

    def test_mid_message_interruption_not_skipped(self, daemon_env):
        """Interruption with plenty of audio left should NOT be skipped."""
        wav_duration = 30.0
        audio_position = 15.0

        remaining = wav_duration - audio_position
        assert remaining > NEAR_END_THRESHOLD

        should_skip = audio_position > 0 and remaining <= NEAR_END_THRESHOLD
        assert should_skip is False


# ---------------------------------------------------------------------------
# Daemon loop message flow (thread-based)
# ---------------------------------------------------------------------------


class TestDaemonLoopMessageFlow:
    """Test the actual daemon loop with enqueued messages.

    Runs daemon_loop in a background thread, enqueues messages via
    the queue directory, and verifies behavior by reading state files.
    """

    @pytest.fixture(autouse=True)
    def _patch_daemon_for_thread(self, daemon_env):
        """Extra patches needed to run daemon_loop in a thread."""
        tmp = daemon_env["tmp_path"]
        state_dir = daemon_env["state_dir"]

        with patch.object(daemon_mod, "PID_FILE", state_dir / "daemon.pid"), \
             patch.object(daemon_mod, "LOCK_FILE", state_dir / "daemon.lock"), \
             patch.object(daemon_mod, "VERSION_FILE", state_dir / "daemon.version"), \
             patch.object(daemon_mod, "RESPAWN_MARKER", state_dir / "daemon.respawn"), \
             patch("signal.signal"):  # signal.signal fails in non-main threads
            yield

    def _enqueue_message(self, queue_dir: Path, text: str, **kwargs) -> Path:
        """Write a queue message file."""
        import secrets
        ts = time.time()
        msg_id = secrets.token_hex(8)
        msg = {
            "id": msg_id,
            "timestamp": ts,
            "session_id": kwargs.get("session_id", "test-session"),
            "project": kwargs.get("project", "test-project"),
            "text": text,
            "persona": kwargs.get("persona", "claude-prime"),
            "speed": kwargs.get("speed", 2.0),
            "speed_method": kwargs.get("speed_method", "playback"),
            "voice_kokoro": "",
            "voice_kokoro_blend": "",
        }
        path = queue_dir / f"{ts}_{msg_id}.json"
        path.write_text(json.dumps(msg))
        return path

    def test_message_plays_and_clears(self, daemon_env):
        """Enqueued message should be played and current_message cleared."""
        tmp = daemon_env["tmp_path"]
        queue_dir = daemon_env["queue_dir"]
        fake = make_fake_player(tmp, duration=0.2)
        wav_path = tmp / "tts_queue_test-session.wav"

        # Pre-create the WAV that generate_speech would create
        make_wav(wav_path, 5.0)

        def fake_generate(text, persona, output_file, **kw):
            make_wav(output_file, 5.0)
            return True

        # Enqueue a message
        msg_file = self._enqueue_message(queue_dir, "Hello world")

        with patch.object(daemon_mod, "detect_player", return_value=[str(fake)]), \
             patch.object(daemon_mod, "daemon_generate_speech", side_effect=fake_generate), \
             patch.object(daemon_mod, "acquire_lock", return_value=True), \
             patch.object(daemon_mod, "release_lock"), \
             patch.object(daemon_mod, "speak_announcement"), \
             patch.object(daemon_mod, "get_queue_config", return_value={
                 "max_depth": 20, "max_age_seconds": 300,
                 "speaker_transition": "none", "coalesce_rapid_ms": 500,
                 "idle_poll_ms": 50,
             }), \
             patch.object(daemon_mod, "load_raw_config", return_value={}):

            # Run daemon in thread, stop after processing
            def run_daemon():
                daemon_mod._shutdown_requested = False
                daemon_mod._daemon_mode = True
                daemon_mod.daemon_loop()

            def stop_after_processing():
                # Wait for message to be consumed
                for _ in range(50):
                    time.sleep(0.1)
                    if not msg_file.exists():
                        break
                time.sleep(0.3)  # Let clear_current_message run
                daemon_mod._shutdown_requested = True

            stopper = threading.Thread(target=stop_after_processing)
            runner = threading.Thread(target=run_daemon, daemon=True)

            stopper.start()
            runner.start()
            stopper.join(timeout=10)
            runner.join(timeout=3)

        # Message file should be consumed
        assert not msg_file.exists()

        # current_message should be cleared after successful playback
        state = read_playback_state()
        assert state.get("current_message") is None

    def test_pause_stores_position(self, daemon_env):
        """Pausing during playback stores audio_position in current_message."""
        tmp = daemon_env["tmp_path"]
        queue_dir = daemon_env["queue_dir"]
        fake = make_fake_player(tmp, duration=10.0)

        def fake_generate(text, persona, output_file, **kw):
            make_wav(output_file, 30.0)
            return True

        msg_file = self._enqueue_message(queue_dir, "A longer message to test pause")

        with patch.object(daemon_mod, "detect_player", return_value=[str(fake)]), \
             patch.object(daemon_mod, "daemon_generate_speech", side_effect=fake_generate), \
             patch.object(daemon_mod, "acquire_lock", return_value=True), \
             patch.object(daemon_mod, "release_lock"), \
             patch.object(daemon_mod, "speak_announcement"), \
             patch.object(daemon_mod, "get_queue_config", return_value={
                 "max_depth": 20, "max_age_seconds": 300,
                 "speaker_transition": "none", "coalesce_rapid_ms": 500,
                 "idle_poll_ms": 50,
             }), \
             patch.object(daemon_mod, "load_raw_config", return_value={}):

            def run_daemon():
                daemon_mod._shutdown_requested = False
                daemon_mod._daemon_mode = True
                daemon_mod.daemon_loop()

            def pause_then_stop():
                # Wait for playback to start
                for _ in range(50):
                    time.sleep(0.05)
                    state = read_playback_state()
                    if state.get("audio_pid"):
                        break

                # Let it play for a bit, then pause
                time.sleep(0.3)
                write_playback_state(paused=True, paused_by="user")

                # Wait for daemon to process the pause
                time.sleep(0.5)

                # Check state before shutting down
                daemon_mod._shutdown_requested = True

            controller = threading.Thread(target=pause_then_stop)
            runner = threading.Thread(target=run_daemon, daemon=True)

            controller.start()
            runner.start()
            controller.join(timeout=10)
            runner.join(timeout=3)

        # current_message should exist with audio_position
        state = read_playback_state()
        msg = state.get("current_message")
        assert msg is not None, "current_message should be stored after pause"
        assert "audio_position" in msg, "audio_position should be set"
        assert msg["audio_position"] > 0, "audio_position should be positive"

        # Clean up
        write_playback_state(paused=False, paused_by=None, current_message=None)

    def test_resume_after_pause_plays_trimmed(self, daemon_env):
        """After pause and resume, daemon should trim and play from position."""
        tmp = daemon_env["tmp_path"]
        queue_dir = daemon_env["queue_dir"]
        fake = make_fake_player(tmp, duration=0.3)

        generate_calls = []

        def fake_generate(text, persona, output_file, **kw):
            generate_calls.append(str(output_file))
            make_wav(output_file, 30.0)
            return True

        # Pre-set an interrupted message with a known position
        interrupted_msg = {
            "session_id": "test-session",
            "project": "test-project",
            "text": "Previously interrupted message",
            "persona": "claude-prime",
            "speed": 2.0,
            "speed_method": "playback",
            "voice_kokoro": "",
            "voice_kokoro_blend": "",
            "audio_position": 20.0,  # Was at 20s when interrupted
        }
        write_playback_state(
            paused=False,
            paused_by=None,
            current_message=interrupted_msg,
        )
        # Mark as controlled restart so daemon preserves current_message
        daemon_env["respawn_marker"].write_text(str(time.time()))

        with patch.object(daemon_mod, "detect_player", return_value=[str(fake)]), \
             patch.object(daemon_mod, "daemon_generate_speech", side_effect=fake_generate), \
             patch.object(daemon_mod, "acquire_lock", return_value=True), \
             patch.object(daemon_mod, "release_lock"), \
             patch.object(daemon_mod, "speak_announcement"), \
             patch.object(daemon_mod, "get_queue_config", return_value={
                 "max_depth": 20, "max_age_seconds": 300,
                 "speaker_transition": "none", "coalesce_rapid_ms": 500,
                 "idle_poll_ms": 50,
             }), \
             patch.object(daemon_mod, "load_raw_config", return_value={}):

            def run_daemon():
                daemon_mod._shutdown_requested = False
                daemon_mod._daemon_mode = True
                daemon_mod.daemon_loop()

            def stop_after_replay():
                # Wait for the replay to complete
                for _ in range(50):
                    time.sleep(0.1)
                    state = read_playback_state()
                    # Once current_message is cleared, replay completed
                    if state.get("current_message") is None:
                        break
                time.sleep(0.2)
                daemon_mod._shutdown_requested = True

            stopper = threading.Thread(target=stop_after_replay)
            runner = threading.Thread(target=run_daemon, daemon=True)

            stopper.start()
            runner.start()
            stopper.join(timeout=10)
            runner.join(timeout=3)

        # Speech was regenerated for the interrupted message
        assert len(generate_calls) >= 1

        # After successful replay, current_message should be cleared
        state = read_playback_state()
        assert state.get("current_message") is None

    def test_near_end_interrupt_skips_replay(self, daemon_env):
        """Message interrupted near the end should be skipped on resume."""
        tmp = daemon_env["tmp_path"]
        fake = make_fake_player(tmp, duration=0.3)

        generate_calls = []

        def fake_generate(text, persona, output_file, **kw):
            generate_calls.append(True)
            make_wav(output_file, 30.0)  # 30s WAV
            return True

        # Pre-set interrupted message at 29.5s of ~30s audio
        interrupted_msg = {
            "session_id": "test-session",
            "project": "test-project",
            "text": "Almost finished message",
            "persona": "claude-prime",
            "speed": 1.0,
            "speed_method": "playback",
            "voice_kokoro": "",
            "voice_kokoro_blend": "",
            "audio_position": 29.5,  # Only 0.5s left
        }
        write_playback_state(
            paused=False,
            paused_by=None,
            current_message=interrupted_msg,
        )
        # Mark as controlled restart so daemon preserves current_message
        daemon_env["respawn_marker"].write_text(str(time.time()))

        with patch.object(daemon_mod, "detect_player", return_value=[str(fake)]), \
             patch.object(daemon_mod, "daemon_generate_speech", side_effect=fake_generate), \
             patch.object(daemon_mod, "acquire_lock", return_value=True), \
             patch.object(daemon_mod, "release_lock"), \
             patch.object(daemon_mod, "speak_announcement"), \
             patch.object(daemon_mod, "get_queue_config", return_value={
                 "max_depth": 20, "max_age_seconds": 300,
                 "speaker_transition": "none", "coalesce_rapid_ms": 500,
                 "idle_poll_ms": 50,
             }), \
             patch.object(daemon_mod, "load_raw_config", return_value={}):

            # Track whether daemon_play_audio was called
            original_play = daemon_mod.daemon_play_audio
            play_calls = []

            def tracking_play(*args, **kwargs):
                play_calls.append(True)
                return original_play(*args, **kwargs)

            with patch.object(daemon_mod, "daemon_play_audio", side_effect=tracking_play):
                def run_daemon():
                    daemon_mod._shutdown_requested = False
                    daemon_mod._daemon_mode = True
                    daemon_mod.daemon_loop()

                def stop_after_skip():
                    # Wait for the skip to happen
                    for _ in range(30):
                        time.sleep(0.1)
                        state = read_playback_state()
                        if state.get("current_message") is None:
                            break
                    time.sleep(0.2)
                    daemon_mod._shutdown_requested = True

                stopper = threading.Thread(target=stop_after_skip)
                runner = threading.Thread(target=run_daemon, daemon=True)

                stopper.start()
                runner.start()
                stopper.join(timeout=10)
                runner.join(timeout=3)

        # WAV was regenerated (to check duration)
        assert len(generate_calls) >= 1

        # But playback should NOT have been called (skipped)
        assert len(play_calls) == 0, \
            f"Expected 0 play calls (near-end skip), got {len(play_calls)}"

        # Message should be cleared (skipped, not stuck)
        state = read_playback_state()
        assert state.get("current_message") is None
