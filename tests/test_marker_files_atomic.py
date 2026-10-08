"""The daemon's marker files are written whole, and a held daemon keeps its announcements to itself.

2026-10-08 00:49Z: a status probe on another machine read ~/.claude-tts/daemon.heartbeat empty.
Path.write_text truncates, then writes, and a reader between the two sees nothing. Every marker the
daemon writes (heartbeat, pid, protocol, release, respawn) now goes through a temp file and a rename,
so a reader sees the old value or the new one. And a daemon that restarts under a hold (a scheduled
upgrade at night) logs "Voice daemon online" instead of saying it through the speakers.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import claude_code_tts.daemon as d
import claude_code_tts.state as st


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    for name, fname in [("HEARTBEAT_FILE", "daemon.heartbeat"), ("PID_FILE", "daemon.pid"), ("VERSION_FILE", "daemon.version"),
                        ("RESPAWN_MARKER", "daemon.respawn"), ("PLAYBACK_STATE_FILE", "playback.json")]:
        monkeypatch.setattr(st, name, tmp_path / fname)
    return tmp_path


class TestAtomicMarkers:
    def test_a_reader_never_sees_an_empty_heartbeat(self, state_dir):
        stop = threading.Event()
        empties = {"n": 0, "reads": 0}

        def reader() -> None:
            while not stop.is_set():
                try:
                    text = st.HEARTBEAT_FILE.read_text()
                except FileNotFoundError:
                    continue
                empties["reads"] += 1
                if text.strip() == "":
                    empties["n"] += 1

        t = threading.Thread(target=reader)
        st.write_heartbeat(force=True)
        t.start()
        for _ in range(500):
            st.write_heartbeat(force=True)
        stop.set()
        t.join()
        assert empties["reads"] > 0
        assert empties["n"] == 0
        assert st.heartbeat_fresh()

    def test_every_marker_goes_through_the_temp_and_rename(self, state_dir):
        replaced: list[str] = []
        real_replace = os.replace

        def spy(src, dst):
            replaced.append(Path(dst).name)
            real_replace(src, dst)

        with patch.object(st.os, "replace", spy):
            st.write_pid()
            st.write_heartbeat(force=True)
            st.write_protocol_marker()
            st.write_release_marker()
            st.write_respawn_marker()
        assert sorted(replaced) == ["daemon.heartbeat", "daemon.pid", "daemon.release", "daemon.respawn", "daemon.version"]
        assert st.PID_FILE.read_text() == str(os.getpid())
        assert st.VERSION_FILE.read_text() == st.CONTROL_PROTOCOL
        assert st.VERSION_FILE.with_name("daemon.release").read_text() == st.__version__
        assert float(st.RESPAWN_MARKER.read_text()) <= time.time()
        assert [p.name for p in state_dir.glob(".*.tmp")] == []

    def test_a_failed_write_leaves_no_temp_file(self, state_dir):
        with patch.object(st.os, "replace", side_effect=OSError("disk full")), pytest.raises(OSError):
            st.write_pid()
        assert [p.name for p in state_dir.glob(".*.tmp")] == []
        assert not st.PID_FILE.exists()

    def test_heartbeat_swallows_errors_as_before(self, state_dir):
        with patch.object(st, "_write_text_atomic", side_effect=OSError("read-only")):
            assert st.write_heartbeat(force=True) is False  # no raise

    def test_heartbeat_writes_once_a_second_unless_forced(self, state_dir, monkeypatch):
        monkeypatch.setattr(st, "_LAST_HEARTBEAT", 0.0)
        assert st.write_heartbeat() is True
        assert st.write_heartbeat() is False
        assert st.write_heartbeat(force=True) is True
        monkeypatch.setattr(st, "_LAST_HEARTBEAT", st.time.monotonic() - st.HEARTBEAT_EVERY_S - 0.01)
        assert st.write_heartbeat() is True


class TestAnnouncementUnderHold:
    def test_held_daemon_logs_instead_of_speaking(self, state_dir):
        st.hold([])
        said: list[str] = []
        with patch.object(d, "daemon_generate_speech", lambda *a, **k: pytest.fail("synthesized under a hold")), \
             patch.object(d, "log", lambda m, level="INFO": said.append(m)):
            d.speak_announcement("Voice daemon online. Ready when you are.")
        assert said == ["Held, so not spoken: Voice daemon online. Ready when you are."]
        assert st.read_playback_state()["paused"] is True

    def test_a_mic_hold_is_a_hold_too(self, state_dir):
        st.set_paused(True, by="mic")
        with patch.object(d, "daemon_generate_speech", lambda *a, **k: pytest.fail("synthesized under a mic hold")), \
             patch.object(d, "log", lambda m, level="INFO": None):
            d.speak_announcement("hello")

    def test_unheld_daemon_speaks(self, state_dir):
        st.set_paused(False)
        played: list[str] = []
        with patch.object(d, "daemon_generate_speech", lambda text, persona, path: Path(path).write_text("wav") or True), \
             patch.object(d, "daemon_play_audio", lambda path, *a, **k: played.append(Path(path).name)), \
             patch.object(d, "get_persona_config", lambda p: {"speed": 2.0, "speed_method": "playback"}):
            d.speak_announcement("hello")
        assert played == ["tts_daemon_announce.wav"]
