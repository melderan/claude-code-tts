"""Daemon liveness and atomic queue writes, as seen from a hook writer."""

import json
import os
import time

from claude_code_tts import audio as audio_mod
from claude_code_tts.audio import daemon_healthy, write_queue_message
from claude_code_tts.config import TTSConfig


def _state(tmp_path, monkeypatch, *, pid=None, beat_age=None):
    monkeypatch.setenv("HOME", str(tmp_path))
    d = tmp_path / ".claude-tts"
    d.mkdir()
    if pid is not None:
        (d / "daemon.pid").write_text(str(pid))
    if beat_age is not None:
        (d / "daemon.heartbeat").write_text(str(time.time() - beat_age))
    return d


class TestDaemonHealthy:
    def test_fresh_heartbeat_with_invisible_pid(self, tmp_path, monkeypatch):
        # A container or sandbox sharing ~/.claude-tts cannot see the host pid.
        _state(tmp_path, monkeypatch, pid=2**22 - 1, beat_age=1)
        assert daemon_healthy() is True

    def test_stale_heartbeat_with_live_pid(self, tmp_path, monkeypatch):
        _state(tmp_path, monkeypatch, pid=os.getpid(), beat_age=120)
        assert daemon_healthy() is False

    def test_no_heartbeat_falls_back_to_live_pid(self, tmp_path, monkeypatch):
        _state(tmp_path, monkeypatch, pid=os.getpid())
        assert daemon_healthy() is True

    def test_no_heartbeat_dead_pid(self, tmp_path, monkeypatch):
        _state(tmp_path, monkeypatch, pid=2**22 - 1)
        assert daemon_healthy() is False

    def test_nothing_on_disk(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        assert daemon_healthy() is False


class TestWriteQueueMessage:
    def test_atomic_and_valid_json(self, tmp_path, monkeypatch):
        qdir = tmp_path / "queue"
        monkeypatch.setattr(audio_mod, "TTS_QUEUE_DIR", qdir)
        cfg = TTSConfig(mode="queue", session_id="s1", project_name="p1")
        out = write_queue_message("hello there", cfg)
        assert out.suffix == ".json"
        assert sorted(p.name for p in qdir.iterdir()) == [out.name]
        assert json.loads(out.read_text())["text"] == "hello there"
