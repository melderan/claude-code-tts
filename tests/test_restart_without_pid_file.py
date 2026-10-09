"""A daemon that lost its pid file can still be stopped, restarted and told apart from its successor.

On 2026-10-09 the daemon on the machine had been running 9.49.1 for a day and a half while
9.49.3, 9.50.0 and 9.51.0 were installed under it: a status check had removed its pid file,
`daemon restart` crashed on `assert pid is not None`, `daemon start` then said "already
running" with exit 0, and every scheduled `just up` wrote `daemon=running`. These tests pin
each link of that chain: the lock file names the pid, a stop without a visible pid says so
instead of crashing, a restart that could not stop reports failure, the CLI exits 1, and the
deploy script calls an older daemon STALE rather than running.
"""

from __future__ import annotations

import importlib.util
import os
import signal
import time
from pathlib import Path

import pytest

from claude_code_tts import cli as cli_mod
from claude_code_tts import daemon as daemon_mod
from claude_code_tts import state as st

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "up.py"
spec = importlib.util.spec_from_file_location("up_for_restart_tests", SCRIPT)
assert spec is not None and spec.loader is not None
up = importlib.util.module_from_spec(spec)
spec.loader.exec_module(up)

INVISIBLE_PID = 2**22 - 1


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(st, "PID_FILE", tmp_path / "daemon.pid")
    monkeypatch.setattr(st, "LOCK_FILE", tmp_path / "daemon.lock")
    monkeypatch.setattr(st, "HEARTBEAT_FILE", tmp_path / "daemon.heartbeat")
    monkeypatch.setattr(st, "PLAYBACK_STATE_FILE", tmp_path / "playback.json")
    st.write_heartbeat(force=True)
    return tmp_path


class TestTheLockFileNamesThePid:
    def test_a_fresh_heartbeat_and_no_pid_file_falls_back_to_the_lock_pid(self, state_dir):
        st.LOCK_FILE.write_text(str(os.getpid()))
        assert st.is_daemon_running() == (True, os.getpid())
        assert not st.PID_FILE.exists(), "a reader never writes the pid file; the daemon does"

    def test_a_lock_pid_this_process_cannot_see_is_not_offered(self, state_dir):
        st.LOCK_FILE.write_text(str(INVISIBLE_PID))
        assert st.is_daemon_running() == (True, None)

    def test_an_empty_or_missing_lock_file_is_no_pid(self, state_dir):
        assert st.lock_pid() is None
        st.LOCK_FILE.write_text("")
        assert st.lock_pid() is None
        assert st.is_daemon_running() == (True, None)

    def test_the_pid_file_wins_when_both_exist(self, state_dir):
        st.PID_FILE.write_text("4242")
        st.LOCK_FILE.write_text(str(os.getpid()))
        assert st.daemon_pid() == 4242
        assert st.is_daemon_running() == (True, 4242)

    def test_a_second_daemon_that_fails_the_lock_leaves_the_holders_pid_in_it(self, state_dir):
        """open(LOCK_FILE, "w") truncated the file before the flock was tried, so a loser
        blanked the holder's pid; the holder's pid is the whole point of the file."""
        assert st.acquire_lock() is True
        holder_fd = st._lock_fd
        try:
            st._lock_fd = None  # a second process: its own descriptor, the same inode
            assert st.acquire_lock() is False
            assert st.LOCK_FILE.read_text() == str(os.getpid())
        finally:
            st._lock_fd = holder_fd
            st.release_lock()

    def test_lockpick_signals_the_lock_pid_when_the_pid_file_is_gone(self, state_dir, monkeypatch):
        sent: list[tuple[int, int]] = []
        monkeypatch.setattr(st.os, "kill", lambda pid, sig: sent.append((pid, sig)))
        monkeypatch.setattr(st.time, "sleep", lambda s: None)
        assert st.acquire_lock() is True
        holder_fd = st._lock_fd
        try:
            st._lock_fd = None
            assert not st.PID_FILE.exists()
            assert st.acquire_lock(lockpick=True) is False  # the holder is still here, patched kill or not
            assert sent == [(os.getpid(), signal.SIGTERM)]
        finally:
            st._lock_fd = holder_fd
            st.release_lock()


class TestStopWithoutAPidFile:
    def test_stop_signals_the_lock_pid(self, state_dir, monkeypatch, capsys):
        st.LOCK_FILE.write_text(str(os.getpid()))
        sent: list[tuple[int, int]] = []

        def fake_kill(pid: int, sig: int) -> None:
            if sig == 0 and any(s == signal.SIGTERM for _, s in sent):
                raise ProcessLookupError  # gone after the SIGTERM
            if sig != 0:
                sent.append((pid, sig))

        monkeypatch.setattr(daemon_mod.os, "kill", fake_kill)
        assert daemon_mod.stop_daemon() is True
        assert sent == [(os.getpid(), signal.SIGTERM)]
        assert "stopped gracefully" in capsys.readouterr().out

    def test_stop_with_no_visible_pid_says_so_and_does_not_crash(self, state_dir, capsys):
        st.LOCK_FILE.write_text(str(INVISIBLE_PID))
        assert daemon_mod.stop_daemon() is False
        out = capsys.readouterr().out
        assert "pid is not visible from here" in out and "heartbeat 0s old" in out

    def test_restart_does_not_start_next_to_a_daemon_it_could_not_stop(self, state_dir, monkeypatch, capsys):
        st.LOCK_FILE.write_text(str(INVISIBLE_PID))
        monkeypatch.setattr(daemon_mod, "start_daemon", lambda lockpick=False: pytest.fail("start_daemon called"))
        assert daemon_mod.daemon_restart() is False
        assert "Restart abandoned" in capsys.readouterr().out

    def test_restart_starts_when_the_stop_succeeded(self, state_dir, monkeypatch):
        calls: list[str] = []
        monkeypatch.setattr(daemon_mod, "stop_daemon", lambda: calls.append("stop") or True)
        monkeypatch.setattr(daemon_mod, "start_daemon", lambda lockpick=False: calls.append("start") or True)
        assert daemon_mod.daemon_restart() is True
        assert calls == ["stop", "start"]


class TestTheCliExitCodes:
    def _run(self, command: str, monkeypatch) -> int:
        import argparse

        args = argparse.Namespace(daemon_command=command, lockpick=False)
        try:
            cli_mod.cmd_daemon(args)
        except SystemExit as e:
            return int(e.code or 0)
        return 0

    def test_a_failed_restart_exits_1(self, monkeypatch):
        monkeypatch.setattr(daemon_mod, "daemon_restart", lambda lockpick=False: False)
        assert self._run("restart", monkeypatch) == 1

    def test_a_failed_stop_exits_1(self, monkeypatch):
        monkeypatch.setattr(daemon_mod, "stop_daemon", lambda: False)
        assert self._run("stop", monkeypatch) == 1

    def test_a_good_restart_exits_0(self, monkeypatch):
        monkeypatch.setattr(daemon_mod, "daemon_restart", lambda lockpick=False: True)
        assert self._run("restart", monkeypatch) == 0

    def test_start_next_to_a_running_daemon_is_the_state_asked_for(self, monkeypatch):
        monkeypatch.setattr(daemon_mod, "is_daemon_running", lambda: (True, 1))
        monkeypatch.setattr(daemon_mod, "start_daemon", lambda lockpick=False: False)
        assert self._run("start", monkeypatch) == 0

    def test_start_that_could_not_start_exits_1(self, monkeypatch):
        monkeypatch.setattr(daemon_mod, "is_daemon_running", lambda: (False, None))
        monkeypatch.setattr(daemon_mod, "start_daemon", lambda lockpick=False: False)
        assert self._run("start", monkeypatch) == 1


class TestTheDeployScriptTellsAnOldDaemonFromTheNewOne:
    def test_the_restart_step_has_no_start_fallback(self):
        import inspect

        src = inspect.getsource(up.main)
        assert 'step("restart", ["claude-tts", "daemon", "restart"])' in src
        assert '["claude-tts", "daemon", "start"]' not in src

    @pytest.mark.parametrize(
        "fresh, have, expect",
        [
            (True, "9.51.1", "running"),
            (True, "9.49.1", "STALE(v9.49.1)"),
            (True, "", "STALE(v?)"),
            (False, "9.51.1", "DOWN"),
        ],
    )
    def test_daemon_state(self, monkeypatch, fresh, have, expect):
        monkeypatch.setattr(up, "heartbeat_fresh", lambda: fresh)
        monkeypatch.setattr(up, "daemon_version", lambda: have)
        assert up.daemon_state("9.51.1") == expect

    def test_the_timeline_pid_falls_back_to_the_lock_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(up, "TTS_DIR", tmp_path)
        assert up.daemon_pid() == "-"
        (tmp_path / "daemon.lock").write_text("446")
        assert up.daemon_pid() == "446"
        (tmp_path / "daemon.pid").write_text("447")
        assert up.daemon_pid() == "447"
        (tmp_path / "daemon.pid").write_text("")
        assert up.daemon_pid() == "446"

    def test_heartbeat_fresh_reads_the_file_this_test_wrote(self, tmp_path, monkeypatch):
        monkeypatch.setattr(up, "TTS_DIR", tmp_path)
        (tmp_path / "daemon.heartbeat").write_text(str(time.time()))
        assert up.heartbeat_fresh() is True


class TestTheHoldLineNamesWhatIsDirty:
    def test_up_to_three_paths_then_a_count(self, monkeypatch):
        answers = {
            ("status", "--porcelain"): " M src/x.py\n?? .mcp.json\n?? a\n?? b\n",
            ("tag", "--points-at", "HEAD"): "v9.51.2",
        }
        monkeypatch.setattr(up, "git", lambda *a: answers[a])
        assert up.not_released("9.51.2") == (
            "working tree has uncommitted changes: M src/x.py, ?? .mcp.json, ?? a and 1 more"
        )

    def test_one_path_is_named_alone(self, monkeypatch):
        answers = {("status", "--porcelain"): "?? .mcp.json\n", ("tag", "--points-at", "HEAD"): ""}
        monkeypatch.setattr(up, "git", lambda *a: answers[a])
        assert up.not_released("9.51.2") == "working tree has uncommitted changes: ?? .mcp.json"
