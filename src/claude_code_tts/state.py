"""The daemon's state on disk: playback.json, heartbeat, pid, lock and the restart markers.

One module reads and writes these files, so every process that asks "is the daemon alive",
"is playback paused" or "what was playing when we stopped" gets the answer the daemon wrote,
and the in-process lock around playback.json covers every writer (play loop, mic watcher,
bridge). It imports only the standard library and config, so the CLI's status and pause
commands use it without loading the daemon. The hook shim's own liveness check stays in
audio.daemon_healthy: it resolves HOME on every call, which a shim started by Claude Code
needs and a module constant cannot give.

Moved out of daemon.py as the first cut of the 10.x plan (docs/redesign-10.md). Tests that
redirect these files patch the names here; daemon re-exports the functions but not the
paths, so a patch aimed at the old home fails loudly instead of doing nothing.
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import threading
import time
from collections.abc import Callable
from io import TextIOWrapper
from pathlib import Path

from claude_code_tts import __version__
from claude_code_tts.config import TTS_CONFIG_DIR, atomic_write_json

PID_FILE = TTS_CONFIG_DIR / "daemon.pid"
LOCK_FILE = TTS_CONFIG_DIR / "daemon.lock"
HEARTBEAT_FILE = TTS_CONFIG_DIR / "daemon.heartbeat"
PLAYBACK_STATE_FILE = TTS_CONFIG_DIR / "playback.json"
# The control-protocol tag, not a release; write_release_marker records the release beside it.
VERSION_FILE = TTS_CONFIG_DIR / "daemon.version"
RESPAWN_MARKER = TTS_CONFIG_DIR / "daemon.respawn"
# The daemon's spoken store (spoken.py): one claim per queue message id, so a replayed or
# doubled message plays once. It lives in the daemon's state directory because that is where
# the daemon's files live and this module is their one reader and writer; on the daemon's
# machine it is local disk, which O_EXCL, rename and flock need. Hooks never claim here: in a
# sandbox this directory is a shared mount, so they claim in their own $TMPDIR. Anyone who can
# see the mount sees only sha256 file names and "<ttl> <token>" contents, never text.
SPOKEN_DIR = TTS_CONFIG_DIR / "spoken"

CONTROL_PROTOCOL = "control-v1"
# A heartbeat older than this is nobody's: the daemon refreshes it every second while
# playing and every poll while idle.
HEARTBEAT_MAX_AGE_S = 30.0
# A respawn marker older than this was left by a restart that never came back: a cold start.
RESPAWN_WINDOW_S = 30.0

_lock_fd: TextIOWrapper | None = None


# --- Lock File ---


def acquire_lock(lockpick: bool = False, log: Callable[[str, str], None] | None = None) -> bool:
    """Acquire exclusive lock to prevent duplicate daemons.

    `log(msg, level)` is the daemon's logger for the failure that is not another daemon
    holding the lock; without one that failure is only the False return.
    """
    global _lock_fd

    try:
        LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        _lock_fd = open(LOCK_FILE, "w")

        if lockpick:
            try:
                fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if PID_FILE.exists():
                    try:
                        old_pid = int(PID_FILE.read_text().strip())
                        os.kill(old_pid, signal.SIGTERM)
                        time.sleep(1)
                    except (ValueError, ProcessLookupError):
                        pass
                fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        _lock_fd.write(str(os.getpid()))
        _lock_fd.flush()
        return True

    except BlockingIOError:
        if _lock_fd:
            _lock_fd.close()
            _lock_fd = None
        return False
    except Exception as e:
        if log is not None:
            log(f"Lock acquisition failed: {e}", "ERROR")
        if _lock_fd:
            _lock_fd.close()
            _lock_fd = None
        return False


def release_lock() -> None:
    """Release the daemon lock."""
    global _lock_fd
    if _lock_fd:
        try:
            fcntl.flock(_lock_fd, fcntl.LOCK_UN)
            _lock_fd.close()
        except Exception:
            pass
        _lock_fd = None


# --- Pid and heartbeat ---


def write_pid() -> None:
    """Record this process as the daemon."""
    PID_FILE.write_text(str(os.getpid()))


def clear_pid() -> None:
    """Forget the daemon pid; missing is fine."""
    PID_FILE.unlink(missing_ok=True)


def pid_alive(pid: int) -> bool:
    """True when a process with this pid exists and is visible to us."""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def write_heartbeat() -> None:
    """Touch the heartbeat file so hooks know we're alive."""
    try:
        HEARTBEAT_FILE.write_text(str(time.time()))
    except Exception:
        pass


def clear_heartbeat() -> None:
    """Stop claiming to be alive; the daemon does this on its way out."""
    HEARTBEAT_FILE.unlink(missing_ok=True)


def heartbeat_fresh() -> bool:
    """True when a daemon touched the heartbeat within HEARTBEAT_MAX_AGE_S."""
    try:
        return time.time() - float(HEARTBEAT_FILE.read_text().strip()) <= HEARTBEAT_MAX_AGE_S
    except (OSError, ValueError):
        return False


def is_daemon_running() -> tuple[bool, int | None]:
    """Check if daemon is running. Returns (is_running, pid)."""
    if not PID_FILE.exists():
        return False, None
    try:
        pid = int(PID_FILE.read_text().strip())
    except ValueError:
        PID_FILE.unlink(missing_ok=True)
        return False, None
    # A fresh heartbeat wins: a sandbox sharing this directory cannot see the host pid.
    if heartbeat_fresh():
        return True, pid
    try:
        os.kill(pid, 0)
        return True, pid
    except (ProcessLookupError, PermissionError):
        PID_FILE.unlink(missing_ok=True)
        return False, None


# --- Restart markers ---


def write_protocol_marker() -> None:
    """Say which control protocol this daemon speaks."""
    VERSION_FILE.write_text(CONTROL_PROTOCOL)


def write_release_marker() -> Path:
    """Record which release this daemon is, next to the protocol marker.

    daemon.version holds the control-protocol tag ("control-v1"), not a release,
    so `just up --if-changed` compared 9.x against it and never skipped. The
    release goes in daemon.release, derived from VERSION_FILE so tests that
    redirect one redirect both.
    """
    path = VERSION_FILE.with_name("daemon.release")
    path.write_text(__version__)
    return path


def write_respawn_marker() -> None:
    """Tell the next daemon that this exit is a controlled restart, not a crash."""
    RESPAWN_MARKER.write_text(str(time.time()))


def take_respawn_marker() -> bool:
    """True when a recent respawn marker exists; the marker is consumed either way.

    A recent marker means a controlled restart (upgrade, config reload) and the
    interrupted message may resume. Missing or old means a cold start (reboot,
    crash, manual start) and whatever the state file remembers is history.
    """
    is_respawn = False
    if RESPAWN_MARKER.exists():
        try:
            marker_age = time.time() - float(RESPAWN_MARKER.read_text().strip())
            is_respawn = marker_age < RESPAWN_WINDOW_S
        except (ValueError, OSError):
            pass
        RESPAWN_MARKER.unlink(missing_ok=True)
    return is_respawn


# --- Playback State (pause/resume) ---


def read_playback_state() -> dict:
    """Read current playback state; the default when the file is missing or unreadable."""
    try:
        return json.loads(PLAYBACK_STATE_FILE.read_text("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {"paused": False, "audio_pid": None, "current_message": None}


# One writer at a time inside this process: the play loop, the mic watcher and the bridge
# all update the state, and a read-modify-write from two threads loses one of the updates.
# Another process (the CLI's pause toggle) still races the daemon; its writes are one flag.
_PLAYBACK_STATE_LOCK = threading.Lock()


UNSET = object()


def write_playback_state(
    audio_pid: int | None | object = UNSET,
    paused: bool | None = None,
    paused_by: str | None | object = UNSET,
    current_message: dict | None | object = UNSET,
) -> None:
    """Update playback state atomically.

    paused_by tracks who paused: "user" (manual toggle) or "mic" (mic watcher).
    This prevents mic-unpause from overriding a manual pause.
    """
    with _PLAYBACK_STATE_LOCK:
        _write_playback_state_locked(audio_pid, paused, paused_by, current_message)


def _write_playback_state_locked(
    audio_pid: int | None | object,
    paused: bool | None,
    paused_by: str | None | object,
    current_message: dict | None | object,
) -> None:
    state = read_playback_state()
    if audio_pid is not UNSET:
        state["audio_pid"] = audio_pid
    if paused is not None:
        # paused_since is the moment the hold began, kept across every later write
        # (updated_at moves on each); a restart reads the hold's true age from it.
        if paused and not state.get("paused"):
            state["paused_since"] = time.time()
        elif not paused:
            state.pop("paused_since", None)
        state["paused"] = paused
    if paused_by is not UNSET:
        state["paused_by"] = paused_by
    if current_message is not UNSET:
        state["current_message"] = current_message
    state["updated_at"] = time.time()
    atomic_write_json(PLAYBACK_STATE_FILE, state)


def set_paused(paused: bool, by: str = "user") -> dict:
    """Hold or release the whole queue, the way the pause hotkey does.

    Only the flag is written. The play loop polls it every 50 ms and stops the
    player itself, treating the stop as a pause (rewind, replay on resume), so
    nothing here needs to know a pid. A release clears paused_by, so a person
    resuming from a page wins over a mic hold exactly as `claude-tts pause` does.
    Returns the state as written.
    """
    if paused:
        write_playback_state(paused=True, paused_by=by)
    else:
        write_playback_state(paused=False, paused_by=None)
    return read_playback_state()


def clear_current_message() -> None:
    """Clear the current message (called after successful playback)."""
    write_playback_state(current_message=None)


def get_interrupted_message() -> dict | None:
    """Get the interrupted message if any, and clear it."""
    state = read_playback_state()
    msg = state.get("current_message")
    if msg:
        write_playback_state(current_message=None)
    return msg
