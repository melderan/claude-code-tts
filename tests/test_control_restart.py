"""A control restart must bring a daemon back however it was started."""

from __future__ import annotations

import sys
from unittest.mock import patch

import pytest

import claude_code_tts.daemon as daemon_mod
from claude_code_tts.daemon import _supervised, handle_control_message


def test_supervised_detects_launchd_and_systemd(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XPC_SERVICE_NAME", raising=False)
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    assert _supervised() is False
    monkeypatch.setenv("XPC_SERVICE_NAME", "0")
    assert _supervised() is False
    monkeypatch.setenv("XPC_SERVICE_NAME", "com.claude-tts.daemon")
    assert _supervised() is True
    monkeypatch.delenv("XPC_SERVICE_NAME")
    monkeypatch.setenv("INVOCATION_ID", "abc")
    assert _supervised() is True


def test_restart_under_launchd_exits_3(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XPC_SERVICE_NAME", "com.claude-tts.daemon")
    daemon_mod.TTS_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with patch.object(daemon_mod.os, "execv") as execv, pytest.raises(SystemExit) as exc:
        handle_control_message({"post_action": "restart"})
    assert exc.value.code == 3
    execv.assert_not_called()
    assert daemon_mod.RESPAWN_MARKER.exists()


def test_restart_without_supervisor_reexecs_in_place(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XPC_SERVICE_NAME", raising=False)
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    daemon_mod.TTS_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with patch.object(daemon_mod.os, "execv") as execv:
        handle_control_message({"post_action": "restart"})
    execv.assert_called_once()
    exe, argv = execv.call_args.args
    assert exe == sys.executable
    assert argv[-3:] == ["daemon", "foreground", "--lockpick"]
    assert daemon_mod.RESPAWN_MARKER.exists()
