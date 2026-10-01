"""state.py owns the daemon's files on disk; daemon.py re-exports the functions, not the paths.

The move out of daemon.py (9.36.2) had one trap: a path constant imported into another
module is a copy, so a test that patched `daemon.PLAYBACK_STATE_FILE` would have redirected
nothing and written into the real state directory. These tests pin the shape that keeps the
trap shut, and the promise that the CLI can ask about the daemon without importing it.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

import claude_code_tts.daemon as daemon_mod
import claude_code_tts.state as state_mod

STATE_PATHS = (
    "PID_FILE",
    "LOCK_FILE",
    "HEARTBEAT_FILE",
    "PLAYBACK_STATE_FILE",
    "VERSION_FILE",
    "RESPAWN_MARKER",
)
REEXPORTED = (
    "acquire_lock",
    "release_lock",
    "write_heartbeat",
    "read_playback_state",
    "write_playback_state",
    "set_paused",
    "clear_current_message",
    "get_interrupted_message",
    "is_daemon_running",
    "write_release_marker",
)


@pytest.mark.parametrize("name", STATE_PATHS)
def test_daemon_does_not_carry_a_copy_of_the_path(name: str) -> None:
    """A patch of the old name must fail, not silently miss."""
    assert hasattr(state_mod, name)
    assert not hasattr(daemon_mod, name), f"daemon.{name} would be a copy a patch cannot reach"


@pytest.mark.parametrize("name", REEXPORTED)
def test_daemon_reexports_the_same_function(name: str) -> None:
    assert getattr(daemon_mod, name) is getattr(state_mod, name)


def test_every_state_file_lives_in_the_config_dir() -> None:
    for name in STATE_PATHS:
        assert getattr(state_mod, name).parent == state_mod.TTS_CONFIG_DIR


def test_state_imports_no_engine_or_daemon_module(tmp_path: Path) -> None:
    """`claude-tts status` and `pause` load state without the daemon, audio or bridge."""
    home = tmp_path / "claude-tts-test-home-probe"
    home.mkdir()
    probe = (
        "import sys, claude_code_tts.state; "
        "print(sorted(m for m in sys.modules if m.startswith('claude_code_tts')))"
    )
    r = subprocess.run(
        [sys.executable, "-c", probe],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin", "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
        capture_output=True,
        text=True,
        check=True,
    )
    loaded = set(eval(r.stdout))  # noqa: S307  (our own repr of a list of module names)
    heavy = {m for m in loaded if m.split(".")[-1] in ("daemon", "audio", "bridge", "handy", "install", "cli")}
    assert heavy == set(), f"state pulled in {sorted(heavy)}"


class TestRespawnMarker:
    """The marker was read inline in daemon_loop; the move made it a function with a test."""

    @pytest.fixture(autouse=True)
    def _marker(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(state_mod, "RESPAWN_MARKER", tmp_path / "daemon.respawn")

    def test_fresh_marker_is_a_respawn_and_is_consumed(self) -> None:
        state_mod.write_respawn_marker()
        assert state_mod.take_respawn_marker() is True
        assert not state_mod.RESPAWN_MARKER.exists()
        assert state_mod.take_respawn_marker() is False, "the second reader sees a cold start"

    def test_old_marker_is_a_cold_start_and_is_consumed(self) -> None:
        state_mod.RESPAWN_MARKER.write_text(str(time.time() - state_mod.RESPAWN_WINDOW_S - 1))
        assert state_mod.take_respawn_marker() is False
        assert not state_mod.RESPAWN_MARKER.exists()

    def test_garbage_marker_is_a_cold_start_and_is_consumed(self) -> None:
        state_mod.RESPAWN_MARKER.write_text("not a timestamp")
        assert state_mod.take_respawn_marker() is False
        assert not state_mod.RESPAWN_MARKER.exists()


def test_acquire_lock_reports_a_non_lock_failure_only_through_the_given_logger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lock lives below the daemon's logger, so the daemon hands it one."""
    monkeypatch.setattr(state_mod, "LOCK_FILE", tmp_path / "not-a-dir" / "daemon.lock")
    (tmp_path / "not-a-dir").write_text("a file where the directory should be")
    assert state_mod.acquire_lock() is False
    seen: list[tuple[str, str]] = []
    assert state_mod.acquire_lock(log=lambda m, level: seen.append((m, level))) is False
    assert len(seen) == 1 and seen[0][1] == "ERROR" and "Lock acquisition failed" in seen[0][0]


def test_paused_since_marks_the_start_of_a_hold_and_survives_later_writes(tmp_path, monkeypatch):
    """A restart reads the hold's true age from paused_since; updated_at moves on every write
    (review of e6be5c9: two restarts in a row granted a second full cap)."""
    import time as _time

    from claude_code_tts import state as st

    monkeypatch.setattr(st, "PLAYBACK_STATE_FILE", tmp_path / "playback.json")
    st.write_playback_state(paused=True, paused_by="mic")
    since = st.read_playback_state()["paused_since"]
    _time.sleep(0.01)
    st.write_playback_state(audio_pid=None)
    s = st.read_playback_state()
    assert s["paused_since"] == since and s["updated_at"] > since
    st.write_playback_state(paused=True, paused_by="mic")  # a repeated pause write does not restart the clock
    assert st.read_playback_state()["paused_since"] == since
    st.write_playback_state(paused=False, paused_by=None)
    assert "paused_since" not in st.read_playback_state()

