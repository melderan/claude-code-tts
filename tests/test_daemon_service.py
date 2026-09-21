"""Service environment and liveness as the daemon sees them."""

import os
import time
from pathlib import Path

from claude_code_tts import audio as audio_mod
from claude_code_tts import daemon as daemon_mod


class TestServicePathEnv:
    def test_includes_tool_dirs_then_system(self, monkeypatch):
        monkeypatch.setattr(daemon_mod.shutil, "which", lambda n: "/x/tools/piper" if n == "piper" else None)
        env = daemon_mod.service_path_env("/y/bin/claude-tts")
        assert env.split(":")[:2] == ["/y/bin", "/x/tools"]
        assert env.endswith("/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")

    def test_no_duplicates(self, monkeypatch):
        monkeypatch.setattr(daemon_mod.shutil, "which", lambda n: "/y/bin/piper")
        assert daemon_mod.service_path_env("/y/bin/claude-tts").count("/y/bin") == 1

    def test_launchd_plist_carries_path(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(daemon_mod.shutil, "which", lambda n: f"/y/bin/{n}")
        daemon_mod._install_launchd()
        plist = (tmp_path / "Library" / "LaunchAgents" / "com.claude-tts.daemon.plist").read_text()
        assert "<key>PATH</key>" in plist and "/y/bin:" in plist
        assert f"<string>{tmp_path}</string>" in plist


class TestIsDaemonRunning:
    def _files(self, tmp_path, monkeypatch, pid, beat_age):
        pid_file = tmp_path / "daemon.pid"
        hb = tmp_path / "daemon.heartbeat"
        pid_file.write_text(str(pid))
        if beat_age is not None:
            hb.write_text(str(time.time() - beat_age))
        monkeypatch.setattr(daemon_mod, "PID_FILE", pid_file)
        monkeypatch.setattr(daemon_mod, "HEARTBEAT_FILE", hb)
        return pid_file

    def test_fresh_heartbeat_keeps_pid_file_for_invisible_pid(self, tmp_path, monkeypatch):
        pid_file = self._files(tmp_path, monkeypatch, 2**22 - 1, 1)
        assert daemon_mod.is_daemon_running() == (True, 2**22 - 1)
        assert pid_file.exists()

    def test_stale_heartbeat_dead_pid_clears(self, tmp_path, monkeypatch):
        pid_file = self._files(tmp_path, monkeypatch, 2**22 - 1, 120)
        assert daemon_mod.is_daemon_running() == (False, None)
        assert not pid_file.exists()

    def test_no_heartbeat_live_pid(self, tmp_path, monkeypatch):
        self._files(tmp_path, monkeypatch, os.getpid(), None)
        assert daemon_mod.is_daemon_running() == (True, os.getpid())


class TestLastError:
    def test_piper_missing_is_explained(self, tmp_path, monkeypatch):
        monkeypatch.setattr(audio_mod.shutil, "which", lambda n: None)
        assert audio_mod.generate_speech("hi", voice_path=tmp_path / "v.onnx", output_path=tmp_path / "o.wav") is None
        assert audio_mod.last_error().startswith("piper not on PATH")

    def test_voice_missing_is_explained(self, tmp_path, monkeypatch):
        monkeypatch.setattr(audio_mod.shutil, "which", lambda n: "/usr/bin/true" if n == "piper" else None)
        assert audio_mod.generate_speech("hi", voice_path=tmp_path / "v.onnx", output_path=tmp_path / "o.wav") is None
        assert audio_mod.last_error().startswith("voice model missing")
