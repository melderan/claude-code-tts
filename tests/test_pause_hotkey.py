"""`claude-tts pause`, the hotkey command, must be seen by the daemon as a pause, not a finish.

Through 9.33.0 the command wrote the flag and then SIGTERMed the player itself. The play loop
checks `proc.poll()` before it reads the flag, so it saw a dead player, reported a natural end,
and the message was cleared instead of held for resume (five of five runs). The daemon's own
`set_paused` never lost a message; the command now goes through it.
"""

from __future__ import annotations

import argparse
import threading
import time
from pathlib import Path
from unittest.mock import patch

import claude_code_tts.cli as cli
import claude_code_tts.daemon as daemon_mod
from claude_code_tts.daemon import daemon_play_audio, read_playback_state, write_playback_state
from tests.test_daemon_integration import make_fake_player, make_wav


def _play_then(tmp_path: Path, pause) -> tuple[bool, bool, float]:
    (Path.home() / ".claude-tts").mkdir(parents=True, exist_ok=True)
    write_playback_state(paused=False, paused_by=None, audio_pid=None, current_message=None)
    fake = make_fake_player(tmp_path, duration=4.0)
    wav = make_wav(tmp_path / "w.wav", 4.0)
    out: dict = {}

    def run() -> None:
        out["r"] = daemon_play_audio(wav)

    with patch.object(daemon_mod, "detect_player", return_value=[str(fake)]):
        t = threading.Thread(target=run)
        t.start()
        time.sleep(0.6)
        pause()
        t.join(15)
    return out["r"]


def test_cmd_pause_is_a_pause_to_the_daemon(tmp_path):
    calls: list = []
    with patch.object(cli.subprocess, "run", lambda *a, **k: calls.append(a)):
        _, was_killed, elapsed = _play_then(tmp_path, lambda: cli.cmd_pause(argparse.Namespace()))
    assert was_killed is True, "the daemon must treat the stop as a pause, not a finished message"
    assert 0.4 < elapsed < 2.0
    state = read_playback_state()
    assert state["paused"] is True and state["paused_by"] == "user"


def test_cmd_pause_toggles_back_to_resumed(tmp_path):
    (Path.home() / ".claude-tts").mkdir(parents=True, exist_ok=True)
    write_playback_state(paused=True, paused_by="mic")
    with patch.object(cli.subprocess, "run", lambda *a, **k: None):
        cli.cmd_pause(argparse.Namespace())
    state = read_playback_state()
    assert state["paused"] is False and state["paused_by"] is None


def test_cmd_pause_does_not_call_osascript_off_macos(tmp_path, monkeypatch):
    (Path.home() / ".claude-tts").mkdir(parents=True, exist_ok=True)
    write_playback_state(paused=False)
    monkeypatch.setattr(cli.sys, "platform", "linux")
    calls: list = []
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: calls.append(a))
    cli.cmd_pause(argparse.Namespace())
    assert calls == []
    assert read_playback_state()["paused"] is True
