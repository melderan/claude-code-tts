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
import tempfile
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
# The Mac's default sound output as the daemon last saw it. Its own file, not a playback.json
# field: that file is read-modify-written by the CLI from another process (hold, kraken), and
# a periodic writer there would race a hold on exactly the night the hold matters.
OUTPUT_DEVICE_FILE = TTS_CONFIG_DIR / "output-device.json"
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


def _write_text_atomic(path: Path, text: str) -> None:
    """Write a small text file whole: a temp file in the same directory, then rename.

    Path.write_text truncates first and writes second, so a reader in between sees an empty
    file; a status probe reading the heartbeat through a shared mount saw exactly that
    (2026-10-08, "daemon.heartbeat empty"). Every marker file the daemon writes goes this way.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def write_pid() -> None:
    """Record this process as the daemon."""
    _write_text_atomic(PID_FILE, str(os.getpid()))


def ensure_pid_file() -> bool:
    """Write the pid file again if it is gone; True when it had to. Called by the daemon loop.

    A status check in a sandbox sharing the directory cannot see the host's pid; before 9.49.3
    it removed the pid file when the heartbeat happened to read empty, and status said "not
    running" about a daemon that was speaking.
    """
    if PID_FILE.exists():
        return False
    write_pid()
    return True


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


# The loop passes every 100 ms and readers accept a heartbeat up to HEARTBEAT_MAX_AGE_S old, so
# one write a second says the same thing for a tenth of the disk work (a temp file and a rename
# each). The loop's other per-pass file work rides on the same cadence (see ensure_pid_file).
HEARTBEAT_EVERY_S = 1.0
_LAST_HEARTBEAT = 0.0


def write_heartbeat(force: bool = False) -> bool:
    """Touch the heartbeat file so hooks know we're alive; True when it was written this call.

    Writes at most once per HEARTBEAT_EVERY_S unless force is set (the daemon's first pass, a
    test that needs every write). Written whole, so a reader never sees it empty.
    """
    global _LAST_HEARTBEAT
    now = time.monotonic()
    if not force and now - _LAST_HEARTBEAT < HEARTBEAT_EVERY_S:
        return False
    try:
        _write_text_atomic(HEARTBEAT_FILE, str(time.time()))
    except Exception:
        return False
    _LAST_HEARTBEAT = now
    return True


def clear_heartbeat() -> None:
    """Stop claiming to be alive; the daemon does this on its way out."""
    HEARTBEAT_FILE.unlink(missing_ok=True)


def heartbeat_age() -> float | None:
    """Seconds since the daemon last touched the heartbeat; None when the file is missing or
    cannot be read as a number (a shared mount mid-write, a bad disk), which is not the same as old."""
    try:
        return time.time() - float(HEARTBEAT_FILE.read_text().strip())
    except (OSError, ValueError):
        return None


def heartbeat_fresh() -> bool:
    """True when a daemon touched the heartbeat within HEARTBEAT_MAX_AGE_S."""
    age = heartbeat_age()
    return age is not None and age <= HEARTBEAT_MAX_AGE_S


def is_daemon_running() -> tuple[bool, int | None]:
    """Check if daemon is running. Returns (is_running, pid).

    A fresh heartbeat is the daemon, pid file or not: a sandbox sharing this directory cannot
    see the host's pid, and the pid file can be missing for a moment. A pid this process cannot
    see is forgotten (the pid file removed) only when the heartbeat is readable and old, or
    there is none: a heartbeat that cannot be read right now says nothing about the daemon
    (2026-10-08: one empty read through a shared mount deleted a speaking daemon's pid file).
    """
    pid: int | None = None
    try:
        pid = int(PID_FILE.read_text().strip())
    except FileNotFoundError:
        pid = None
    except (ValueError, OSError):
        pid = None
    age = heartbeat_age()
    if age is not None and age <= HEARTBEAT_MAX_AGE_S:
        return True, pid
    heartbeat_says_dead = age is not None or not HEARTBEAT_FILE.exists()
    if pid is None:
        if PID_FILE.exists() and heartbeat_says_dead:
            PID_FILE.unlink(missing_ok=True)  # unreadable pid, and the heartbeat agrees nobody is here
        return False, None
    try:
        os.kill(pid, 0)
        return True, pid
    except (ProcessLookupError, PermissionError):
        if heartbeat_says_dead:
            PID_FILE.unlink(missing_ok=True)
        return False, None


# --- Restart markers ---


def write_protocol_marker() -> None:
    """Say which control protocol this daemon speaks."""
    _write_text_atomic(VERSION_FILE, CONTROL_PROTOCOL)


def write_release_marker() -> Path:
    """Record which release this daemon is, next to the protocol marker.

    daemon.version holds the control-protocol tag ("control-v1"), not a release,
    so `just up --if-changed` compared 9.x against it and never skipped. The
    release goes in daemon.release, derived from VERSION_FILE so tests that
    redirect one redirect both.
    """
    path = VERSION_FILE.with_name("daemon.release")
    _write_text_atomic(path, __version__)
    return path


def daemon_release() -> str | None:
    """The release the running daemon wrote at start (daemon.release), None when unknown."""
    try:
        text = VERSION_FILE.with_name("daemon.release").read_text().strip()
    except OSError:
        return None
    return text or None


def release_at_least(release: str | None, floor: str) -> bool:
    """Whether a release string ("9.49.0") is floor or newer; an unknown release is not."""
    if not release:
        return False
    try:
        have = tuple(int(p) for p in release.split(".")[:3])
        want = tuple(int(p) for p in floor.split(".")[:3])
    except ValueError:
        return False
    return have >= want


def write_output_device(device: dict | None) -> None:
    """Record the default output device ({"name", "transport"}) with a checked_at stamp; None removes the file."""
    if isinstance(device, dict) and device.get("name"):
        atomic_write_json(
            OUTPUT_DEVICE_FILE,
            {"name": str(device["name"]), "transport": str(device.get("transport") or ""), "checked_at": time.time()},
        )
    else:
        OUTPUT_DEVICE_FILE.unlink(missing_ok=True)


def read_output_device() -> dict | None:
    """The recorded output device, or None when there is none or the file is unreadable."""
    try:
        data = json.loads(OUTPUT_DEVICE_FILE.read_text("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict) or not data.get("name"):
        return None
    return data


def write_respawn_marker() -> None:
    """Tell the next daemon that this exit is a controlled restart, not a crash."""
    _write_text_atomic(RESPAWN_MARKER, str(time.time()))


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
    let_through: list[str] | None | object = UNSET,
    mic_held: bool | None = None,
) -> None:
    """Update playback state atomically.

    paused_by tracks who paused: "user" (manual toggle) or "mic" (mic watcher).
    This prevents mic-unpause from overriding a manual pause. let_through is the
    list of rooms a person's hold lets play (see may_play); mic_held marks a
    recording that began under a person's hold, so those rooms wait for it too.
    A release (paused=False) clears both.
    """
    with _PLAYBACK_STATE_LOCK:
        _write_playback_state_locked(audio_pid, paused, paused_by, current_message, let_through, mic_held)


def _write_playback_state_locked(
    audio_pid: int | None | object,
    paused: bool | None,
    paused_by: str | None | object,
    current_message: dict | None | object,
    let_through: list[str] | None | object = UNSET,
    mic_held: bool | None = None,
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
            state.pop("let_through", None)
            state.pop("mic_held", None)
        state["paused"] = paused
    if paused_by is not UNSET:
        state["paused_by"] = paused_by
    if let_through is not UNSET:
        rooms = [str(r) for r in let_through] if isinstance(let_through, list) else []
        if rooms:
            state["let_through"] = rooms
        else:
            state.pop("let_through", None)
    if mic_held is not None:
        if mic_held:
            state["mic_held"] = True
        else:
            state.pop("mic_held", None)
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


# --- The selective hold (JMO 2026-10-02): hold everyone, let named rooms through ---


def room_tag(session_id: str) -> str:
    """The room a session id names: the part after the last "--", minus a leading "claude-code-".

    alice--claude--claude-code-tts is the tts room, alice--claude--notes is notes,
    bob--claude--k8s is k8s; a plain id is its own tag. This is the key a
    person uses for a friend, in the CLI and on a page.
    """
    tag = session_id.rsplit("--", 1)[-1]
    return tag.removeprefix("claude-code-") or tag


def lets_through(session_id: str, let_through: list[str] | None) -> bool:
    """True when the let-through list names this session: by full id, room tag or last segment."""
    if not let_through:
        return False
    last = session_id.rsplit("--", 1)[-1]
    tag = room_tag(session_id)
    return any(entry in (session_id, tag, last) for entry in let_through)


def may_play(state: dict, session_id: str) -> bool:
    """Whether a message from session_id may play under this state.

    Not paused: yes. A mic hold (paused_by "mic"), or a recording under a person's hold
    (mic_held): nobody, the person is speaking. A person's hold: only the rooms let through.
    """
    if not state.get("paused"):
        return True
    if state.get("paused_by") != "user" or state.get("mic_held"):
        return False
    return lets_through(session_id, state.get("let_through"))


def hold(let: list[str] | None = None) -> dict:
    """A person's hold on everyone, letting the rooms in `let` play on; the state as written.

    Replaces the let-through list. A recording in progress (a mic hold, or mic_held already)
    stays a recording: the rooms let through wait for Handy's stop like everyone else.
    """
    before = read_playback_state()
    recording = bool(before.get("paused") and before.get("paused_by") == "mic") or bool(before.get("mic_held"))
    write_playback_state(paused=True, paused_by="user", let_through=list(let or []), mic_held=recording)
    return read_playback_state()


def let_rooms(rooms: list[str]) -> dict:
    """Add rooms to the let-through list of the current hold; holds everyone first if not held."""
    before = read_playback_state()
    if not (before.get("paused") and before.get("paused_by") == "user"):
        return hold(rooms)
    current = list(before.get("let_through") or [])
    merged = current + [r for r in rooms if r not in current]
    write_playback_state(let_through=merged)
    return read_playback_state()


def release() -> dict:
    """Release the kraken: everyone speaks again. Clears the hold, the list and the mic flag."""
    return set_paused(False)


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
