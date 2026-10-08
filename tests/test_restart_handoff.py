"""A daemon restart finishes the current message instead of talking over it.

JMO, 2026-09-29, after `just up`: "That restart caused two people to talk at
once!" The stopper force-killed the daemon 15 s into a long message, its
afplay lived on as an orphan, and the new daemon replayed the same message
from the start on top of it. JMO's rule: graceful above all. If we are
speaking, we finish, however long it takes; what matters is never losing the
place and never two voices. So the stopper waits while the daemon is speaking
and forces only when it is idle and stuck; the new daemon kills a player a
crash left alive and resumes a message sentence streaming stopped at a boundary.
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import claude_code_tts.daemon as daemon_mod
import tests.test_daemon_integration as _integ

daemon_env = _integ.daemon_env  # the shared fixture, registered under its own name in this module
make_fake_player, make_wav, read_playback_state = _integ.make_fake_player, _integ.make_wav, _integ.read_playback_state

QUEUE_CONFIG = {
    "max_depth": 20, "max_age_seconds": 300, "speaker_transition": "none",
    "coalesce_rapid_ms": 500, "idle_poll_ms": 50, "prefetch_next": False,
}


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _enqueue(queue_dir: Path, text: str) -> Path:
    ts = time.time()
    msg_id = secrets.token_hex(8)
    msg = {
        "id": msg_id, "timestamp": ts, "session_id": "test-session", "project": "test-project",
        "text": text, "persona": "claude-prime", "speed": 2.0, "speed_method": "playback",
        "voice_kokoro": "", "voice_kokoro_blend": "",
    }
    path = queue_dir / f"{ts:.6f}_{msg_id}.json"
    tmp = path.with_suffix(".tmp")  # whole or not at all: the loop deletes a half-written file
    tmp.write_text(json.dumps(msg))
    tmp.rename(path)
    return path


def _run_loop(daemon_env, fake_generate, play_duration, stop_when, before_stop=None, extra_patches=()):
    tmp = daemon_env["tmp_path"]
    fake = make_fake_player(tmp, duration=play_duration)
    patches = [
        patch.object(daemon_mod, "detect_player", return_value=[str(fake)]),
        patch.object(daemon_mod, "daemon_generate_speech", side_effect=fake_generate),
        patch.object(daemon_mod, "acquire_lock", return_value=True),
        patch.object(daemon_mod, "release_lock"),
        patch.object(daemon_mod, "speak_announcement"),
        patch.object(daemon_mod, "get_queue_config", return_value=dict(QUEUE_CONFIG)),
        patch.object(daemon_mod, "load_raw_config", return_value={}),
        patch("signal.signal"),
        *extra_patches,
    ]
    for pt in patches:
        pt.start()
    try:
        def run_daemon():
            daemon_mod._shutdown_requested = False
            daemon_mod._daemon_mode = True
            daemon_mod.daemon_loop()

        def stopper():
            for _ in range(200):
                time.sleep(0.05)
                if stop_when():
                    break
            if before_stop:
                before_stop()
            daemon_mod._shutdown_requested = True

        st = threading.Thread(target=stopper)
        rn = threading.Thread(target=run_daemon, daemon=True)
        st.start()
        rn.start()
        st.join(timeout=20)
        rn.join(timeout=5)
        assert not rn.is_alive(), "daemon loop did not exit after the shutdown request"
    finally:
        for pt in patches:
            pt.stop()


class TestShutdownFinishesTheMessage:
    def test_shutdown_request_lets_the_player_finish(self, daemon_env):
        queue_dir = daemon_env["queue_dir"]

        def fake_generate(text, persona, output_file, **kw):
            make_wav(output_file, 30.0)
            return True

        msg_file = _enqueue(queue_dir, "a message a restart must not cut")
        started = time.monotonic()
        _run_loop(daemon_env, fake_generate, play_duration=1.5,
                  stop_when=lambda: read_playback_state().get("audio_pid") is not None)
        assert time.monotonic() - started >= 1.4, "the loop cut the message instead of finishing it"
        state = read_playback_state()
        assert state.get("audio_pid") is None and state.get("current_message") is None
        assert not msg_file.exists()
        assert "Audio killed" not in daemon_env["log_file"].read_text()


class TestStartupHandoff:
    def _state(self, daemon_env, **fields):
        state = {"paused": False, "audio_pid": None, "current_message": None, "updated_at": time.time()}
        state.update(fields)
        daemon_env["state_dir"].joinpath("playback.json").write_text(json.dumps(state))

    def test_orphaned_player_is_killed_and_the_message_resumes(self, daemon_env):
        orphan = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            interrupted = {
                "session_id": "test-session", "project": "test-project", "persona": "claude-prime",
                "text": "the message the old daemon was cut off in", "speed": 2.0,
                "speed_method": "playback", "audio_position": 5.0,
            }
            self._state(daemon_env, audio_pid=orphan.pid, current_message=interrupted)
            spoken: list[str] = []

            def fake_generate(text, persona, output_file, **kw):
                spoken.append(text)
                make_wav(output_file, 30.0)
                return True

            _run_loop(daemon_env, fake_generate, play_duration=0.2, stop_when=lambda: bool(spoken))
            assert spoken == ["the message the old daemon was cut off in"]  # resumed, not dropped
            time.sleep(0.2)
            assert not _alive(orphan.pid) or orphan.poll() is not None, "the orphaned player kept talking"
            log_text = daemon_env["log_file"].read_text()
            assert "Killed the previous daemon's player" in log_text
            assert "Resuming the message the previous daemon stopped in" in log_text
        finally:
            if orphan.poll() is None:
                orphan.kill()
            orphan.wait()

    def test_message_without_a_position_is_still_cleared(self, daemon_env):
        """It never started to play, so its queue file will play it; keeping it would speak it twice."""
        never_played = {"session_id": "s", "project": "p", "persona": "claude-prime", "text": "x", "speed": 2.0}
        self._state(daemon_env, current_message=never_played)

        def fake_generate(text, persona, output_file, **kw):
            make_wav(output_file, 1.0)
            return True

        _run_loop(daemon_env, fake_generate, play_duration=0.1,
                  stop_when=lambda: "Cleared stale state" in daemon_env["log_file"].read_text())
        assert "current_message" in daemon_env["log_file"].read_text().split("Cleared stale state")[1].splitlines()[0]

    def _mic_message(self, daemon_env, text="held through the restart"):
        queue_dir = daemon_env["queue_dir"]
        msg = {"id": "m1", "timestamp": time.time(), "session_id": "s", "project": "p", "text": text,
               "persona": "claude-prime", "speed": 2.0, "speed_method": "playback"}
        (queue_dir / f"{time.time():.6f}_m1.json").write_text(json.dumps(msg))

    def test_a_recent_mic_hold_survives_the_restart_until_handy_logs_the_stop(self, daemon_env, tmp_path):
        """2026-10-01: an upgrade restart 139 s into a dictation; the old daemon's mic pause was
        called stale and the new one spoke over the person dictating. Younger than the cap and with
        the watcher on, the hold is kept, and Handy's stop line, tailed by the watcher, ends it."""
        from datetime import datetime

        import claude_code_tts.mic_watcher as mw

        stamp = datetime.now().strftime("[%Y-%m-%d][%H:%M:%S]")
        handy_log = tmp_path / "handy.log"
        handy_log.write_text(f"{stamp}[a][DEBUG] Recording started for binding transcribe\n")
        self._state(daemon_env, paused=True, paused_by="mic", paused_since=time.time() - 139,
                    updated_at=time.time() - 3)
        self._mic_message(daemon_env)
        spoken: list[str] = []

        def fake_generate(text, persona, output_file, **kw):
            spoken.append(text)
            make_wav(output_file, 1.0)
            return True

        def handy_stops():
            assert spoken == [], "nothing may speak while the carried mic hold stands"
            with handy_log.open("a") as f:
                f.write(f"{stamp}[a][DEBUG] Recording stopped and samples retrieved\n")
            for _ in range(100):
                if spoken:
                    break
                time.sleep(0.05)

        with patch.object(mw, "HANDY_LOG", handy_log), patch.object(mw, "handy_settings", return_value={"log_level": "debug"}):
            _run_loop(daemon_env, fake_generate, play_duration=0.1,
                      stop_when=lambda: "Keeping the previous daemon's mic hold" in daemon_env["log_file"].read_text(),
                      before_stop=handy_stops,
                      extra_patches=[patch.object(daemon_mod, "load_raw_config",
                                                  return_value={"mic_aware_pause": True, "mic_resume_delay_ms": 10})])
        log = daemon_env["log_file"].read_text()
        assert "Keeping the previous daemon's mic hold from 139s ago" in log, "paused_since, not updated_at, is the age"
        assert "mic-pause" not in log
        assert spoken == ["held through the restart"]

    def test_a_mic_hold_is_not_kept_when_the_watcher_is_off(self, daemon_env):
        self._state(daemon_env, paused=True, paused_by="mic", updated_at=time.time() - 139)

        def fake_generate(text, persona, output_file, **kw):
            make_wav(output_file, 1.0)
            return True

        _run_loop(daemon_env, fake_generate, play_duration=0.1,
                  stop_when=lambda: "Cleared stale state" in daemon_env["log_file"].read_text())
        assert "mic-pause" in daemon_env["log_file"].read_text().split("Cleared stale state")[1].splitlines()[0]

    def test_a_mic_hold_is_released_when_the_watcher_cannot_start(self, daemon_env, tmp_path):
        import claude_code_tts.mic_watcher as mw

        self._state(daemon_env, paused=True, paused_by="mic", updated_at=time.time() - 139)
        self._mic_message(daemon_env, "spoken once the hold is released")
        spoken: list[str] = []

        def fake_generate(text, persona, output_file, **kw):
            spoken.append(text)
            make_wav(output_file, 1.0)
            return True

        with patch.object(mw, "HANDY_LOG", tmp_path / "missing-handy.log"):
            _run_loop(daemon_env, fake_generate, play_duration=0.1,
                      stop_when=lambda: bool(spoken),
                      extra_patches=[patch.object(daemon_mod, "load_raw_config", return_value={"mic_aware_pause": True})])
        log = daemon_env["log_file"].read_text()
        assert "Released the previous daemon's mic hold: no watcher to end it" in log
        assert spoken == ["spoken once the hold is released"]

    def test_an_old_mic_hold_is_still_cleared(self, daemon_env):
        self._state(daemon_env, paused=True, paused_by="mic", updated_at=time.time() - 3600)

        def fake_generate(text, persona, output_file, **kw):
            make_wav(output_file, 1.0)
            return True

        _run_loop(daemon_env, fake_generate, play_duration=0.1,
                  stop_when=lambda: "Cleared stale state" in daemon_env["log_file"].read_text())
        assert "mic-pause" in daemon_env["log_file"].read_text().split("Cleared stale state")[1].splitlines()[0]

    def test_old_interrupted_message_is_history(self, daemon_env):
        stale = {"session_id": "s", "project": "p", "persona": "claude-prime", "text": "ancient", "speed": 2.0, "audio_position": 3.0}
        self._state(daemon_env, current_message=stale, updated_at=time.time() - 3600)
        spoken: list[str] = []

        def fake_generate(text, persona, output_file, **kw):
            spoken.append(text)
            make_wav(output_file, 1.0)
            return True

        _run_loop(daemon_env, fake_generate, play_duration=0.1,
                  stop_when=lambda: "Cleared stale state" in daemon_env["log_file"].read_text())
        assert spoken == []


class TestStopDaemonWaitsWhileSpeaking:
    def _run(self, daemon_mod_, alive_for_checks: int, playing_for_checks: int, capsys):
        """The daemon answers `kill -0` alive for N checks; its player is alive for M checks."""
        checks = {"pid": 0, "player": 0}
        kills: list[tuple[int, int]] = []

        def fake_kill(pid, sig):
            kills.append((pid, sig))
            if pid == 4242 and sig == 0:
                checks["pid"] += 1
                if checks["pid"] > alive_for_checks:
                    raise ProcessLookupError
            if pid == 777 and sig == 0:
                checks["player"] += 1
                if checks["player"] > playing_for_checks:
                    raise ProcessLookupError

        clock = {"t": 0.0}

        def fake_sleep(s):
            clock["t"] += s

        with patch.object(daemon_mod_, "is_daemon_running", return_value=(True, 4242)), \
             patch.object(daemon_mod_, "read_playback_state", return_value={"audio_pid": 777}), \
             patch.object(daemon_mod_.os, "kill", side_effect=fake_kill), \
             patch.object(daemon_mod_.time, "sleep", fake_sleep), \
             patch.object(daemon_mod_.time, "monotonic", lambda: clock["t"]):
            ok = daemon_mod_.stop_daemon()
        return ok, kills, capsys.readouterr().out

    def test_a_speaking_daemon_is_waited_for_well_past_the_old_15s(self, daemon_env, capsys):
        # Player alive for 60 s of checks (600 ticks), daemon exits right after: no force.
        ok, kills, out = self._run(daemon_mod, alive_for_checks=602, playing_for_checks=600, capsys=capsys)
        assert ok and (4242, signal.SIGKILL) not in kills
        assert "Waiting for the current message to finish" in out and "Daemon stopped gracefully" in out

    def test_an_idle_stuck_daemon_is_forced_after_the_grace(self, daemon_env, capsys):
        ok, kills, out = self._run(daemon_mod, alive_for_checks=10_000, playing_for_checks=0, capsys=capsys)
        assert ok and (4242, signal.SIGKILL) in kills
        assert "not speaking and did not exit" in out
        # It was idle for ~15 s of ticks, not 60: forced promptly once idle.
        assert sum(1 for p, sig in kills if p == 4242 and sig == 0) < 200
