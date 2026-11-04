"""A pid a stopped daemon left behind is not a daemon after a reboot.

2026-11-04, JMO's Mac: the daemon stopped cleanly at 11:13 PT and left daemon.pid and
daemon.lock reading 927. After the reboot another process held 927, and the service's start
printed "Background daemon is running (PID: 927)" and quit: no daemon, no heartbeat, no voice
until a hand restart. The same refusal is in the log once before, with 851. Two fixes, each
tested here against real processes: a graceful stop removes its own pid file and blanks the
lock's pid, and a live pid with no heartbeat counts as the daemon only when it is another
process whose command line is a daemon's (the program and `daemon start|restart|foreground`).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from claude_code_tts import state as st
from tests import test_restart_handoff as handoff

daemon_env = handoff.daemon_env


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(st, "PID_FILE", tmp_path / "daemon.pid")
    monkeypatch.setattr(st, "LOCK_FILE", tmp_path / "daemon.lock")
    monkeypatch.setattr(st, "HEARTBEAT_FILE", tmp_path / "daemon.heartbeat")
    return tmp_path


def _spawn(argv: list[str]) -> Iterator[int]:
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        yield proc.pid
    finally:
        proc.kill()
        proc.wait(timeout=5)


@pytest.fixture
def stranger() -> Iterator[int]:
    """A live process that is not a daemon: what holds the old pid after a reboot."""
    yield from _spawn(["sleep", "60"])


@pytest.fixture
def daemon_like() -> Iterator[int]:
    """A live process whose command line is a daemon's (`python -m claude_code_tts.cli ...`)."""
    script = "import time; time.sleep(60)"
    yield from _spawn([sys.executable, "-c", script, "-m", "claude_code_tts.cli", "daemon", "foreground"])


def _wait_for_command(pid: int, word: str) -> None:
    """ps shows the exec'd command line, not the forked parent's, once exec has run."""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        out = subprocess.run(["ps", "-ww", "-p", str(pid), "-o", "command="], capture_output=True, text=True).stdout
        if word in out:
            return
        time.sleep(0.02)  # sleep-ok: polling ps for an exec to land, bounded by the deadline
    pytest.fail(f"pid {pid} never showed {word!r} in ps")


class TestALiveStrangerIsNotTheDaemon:
    def test_the_reboot_case_no_heartbeat_and_a_reused_pid(self, state_dir, stranger):
        """The 927 case. Through 9.85.0 this said (True, 927) and the start quit."""
        _wait_for_command(stranger, "sleep")
        st.PID_FILE.write_text(str(stranger))
        assert st.is_daemon_running() == (False, None)
        assert not st.PID_FILE.exists(), "the stale pid file is forgotten, so the next start is clean"

    def test_an_old_readable_heartbeat_and_a_reused_pid(self, state_dir, stranger):
        _wait_for_command(stranger, "sleep")
        st.HEARTBEAT_FILE.write_text("1.0")
        st.PID_FILE.write_text(str(stranger))
        assert st.is_daemon_running() == (False, None)

    def test_an_unreadable_heartbeat_says_nothing_and_keeps_the_old_answer(self, state_dir, stranger):
        """A heartbeat read empty through a shared mount is not evidence either way (9.49.3)."""
        _wait_for_command(stranger, "sleep")
        st.HEARTBEAT_FILE.write_text("")
        st.PID_FILE.write_text(str(stranger))
        assert st.is_daemon_running() == (True, stranger)
        assert st.PID_FILE.exists()

    def test_a_daemon_before_its_first_heartbeat_is_running(self, state_dir, daemon_like):
        """`daemon start`'s parent asks 0.5 s after the fork, before any heartbeat."""
        _wait_for_command(daemon_like, "claude_code_tts")
        st.PID_FILE.write_text(str(daemon_like))
        assert st.is_daemon_running() == (True, daemon_like)
        assert st.PID_FILE.exists()

    def test_pid_is_daemon_reads_the_command_line(self, stranger, daemon_like):
        _wait_for_command(stranger, "sleep")
        _wait_for_command(daemon_like, "claude_code_tts")
        assert st.pid_is_daemon(stranger) is False
        assert st.pid_is_daemon(daemon_like) is True

    def test_a_narrow_terminal_does_not_hide_the_daemon_verb(self, daemon_like, monkeypatch):
        """CI, ubuntu py3.14, 2026-11-04: procps cut the line at $COLUMNS, before the verb that
        follows a long interpreter path, and a real daemon read as a stranger."""
        _wait_for_command(daemon_like, "claude_code_tts")
        monkeypatch.setenv("COLUMNS", "40")
        assert st.pid_is_daemon(daemon_like) is True

    def test_a_claude_tts_client_is_not_a_daemon(self, state_dir):
        """A hook's `claude-tts speak` holds the program's name and no daemon verb."""
        argv = [sys.executable, "-c", "import time; time.sleep(60)", "claude-tts", "speak", "--from-hook"]
        for pid in _spawn(argv):
            _wait_for_command(pid, "--from-hook")
            assert st.pid_is_daemon(pid) is False
            st.PID_FILE.write_text(str(pid))
            assert st.is_daemon_running() == (False, None)

    def test_a_new_daemon_that_drew_the_old_pid_is_not_running_yet(self, tmp_path):
        """Haley's case: launchd gives the new `daemon foreground` the old daemon's pid, so it
        finds its own pid in the stale file before its first heartbeat. Run in a process whose
        command line is a daemon's, because that is where the name check alone said yes."""
        check = (
            "import os, sys; from pathlib import Path; import claude_code_tts.state as st; "
            "d = Path(os.environ['STALE_DIR']); st.PID_FILE = d / 'daemon.pid'; "
            "st.LOCK_FILE = d / 'daemon.lock'; st.HEARTBEAT_FILE = d / 'daemon.heartbeat'; "
            "st.PID_FILE.write_text(str(os.getpid())); print(st.is_daemon_running())"
        )
        src = str(Path(st.__file__).resolve().parents[1])
        env = {**os.environ, "STALE_DIR": str(tmp_path), "PYTHONPATH": src}
        argv = [sys.executable, "-c", check, "-m", "claude_code_tts.cli", "daemon", "foreground"]
        out = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=30)
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == "(False, None)"

    def test_the_parent_of_this_process_is_not_another_daemon(self, daemon_like, monkeypatch):
        _wait_for_command(daemon_like, "claude_code_tts")
        monkeypatch.setattr(st.os, "getppid", lambda: daemon_like)
        assert st.pid_is_daemon(daemon_like) is False

    def test_no_ps_is_unknown_and_keeps_the_old_answer(self, state_dir, stranger, monkeypatch):
        def no_ps(*a, **k):
            raise FileNotFoundError("ps")

        monkeypatch.setattr(st.subprocess, "run", no_ps)
        assert st.pid_is_daemon(stranger) is None
        st.PID_FILE.write_text(str(stranger))
        assert st.is_daemon_running() == (True, stranger)


class TestAStaleFileNeverSignalsAStranger:
    """`daemon stop` and `restart` sent SIGTERM, then SIGKILL, to whatever the files named."""

    def test_stop_refuses_a_live_pid_that_is_not_a_daemon(self, state_dir, stranger, capsys):
        from claude_code_tts import daemon as daemon_mod

        _wait_for_command(stranger, "sleep")
        st.write_heartbeat(force=True)  # fresh: is_daemon_running answers by the heartbeat alone
        st.PID_FILE.write_text(str(stranger))
        assert daemon_mod.stop_daemon() is False
        assert "not a claude-tts daemon" in capsys.readouterr().out
        assert st.pid_alive(stranger), "the stranger was signalled"


class TestARefusedForegroundStartIsAFailure:
    def test_foreground_refusal_exits_1_so_launchd_tries_again(self, monkeypatch):
        """KeepAlive SuccessfulExit false: an exit 0 refusal kept the daemon down for an hour."""
        from claude_code_tts import cli
        from claude_code_tts import daemon as daemon_mod

        monkeypatch.setattr(daemon_mod, "is_daemon_running", lambda: (True, 4242))
        monkeypatch.setattr(daemon_mod, "log", lambda *a, **k: None)
        monkeypatch.setattr(sys, "argv", ["claude-tts", "daemon", "foreground"])
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 1


class TestAGracefulStopLeavesNoPid:
    def test_clear_own_pid_removes_only_this_process(self, state_dir):
        st.PID_FILE.write_text(str(os.getpid()))
        st.clear_own_pid()
        assert not st.PID_FILE.exists()
        st.PID_FILE.write_text("4242")  # a takeover's daemon wrote this; it stays
        st.clear_own_pid()
        assert st.PID_FILE.read_text() == "4242"

    def test_a_released_lock_names_no_pid(self, state_dir):
        assert st.acquire_lock() is True
        try:
            assert st.lock_pid() == os.getpid()
        finally:
            st.release_lock()
        assert st.lock_pid() is None
        assert st.LOCK_FILE.exists(), "the flock file stays; only its pid goes"

    def test_the_daemon_loop_removes_its_pid_file_on_shutdown(self, daemon_env):
        """launchd runs `daemon foreground`, which has no atexit clear_pid: the 927 file."""
        handoff._run_loop(daemon_env, lambda *a, **k: True, play_duration=0.1,
                          stop_when=lambda: st.PID_FILE.exists())
        assert not st.PID_FILE.exists()

    def test_a_takeovers_pid_file_survives_the_old_daemons_shutdown(self, daemon_env):
        handoff._run_loop(daemon_env, lambda *a, **k: True, play_duration=0.1,
                          stop_when=lambda: st.PID_FILE.exists(),
                          before_stop=lambda: st.PID_FILE.write_text("4242"))
        assert st.PID_FILE.read_text() == "4242"
