"""Mic-aware pause — auto-pause TTS when voice-to-text is recording.

Watches Handy's log file for recording start/stop events and toggles
the daemon's pause state accordingly. Manual pause always takes priority.

Runs as a daemon thread started from daemon_loop() when mic_aware_pause
is enabled in config.json.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any

# Handy log location (macOS). Handy writes here through tauri-plugin-log,
# filtered to the level in its own settings (Settings > Debug > Log Level).
HANDY_LOG = Path.home() / "Library" / "Logs" / "com.pais.handy" / "handy.log"
HANDY_SETTINGS = (
    Path.home() / "Library" / "Application Support" / "com.pais.handy" / "settings_store.json"
)

# Patterns to match in the log. Every recording event Handy emits is at DEBUG
# level (src-tauri/src/actions.rs, TranscribeAction::start/stop; verified against
# Handy 0.9.7, 2026-09-22), so the file log must be set to Debug or these lines
# never appear. The older strings are kept for Handy versions before the
# TranscribeAction refactor.
_RE_RECORDING_START = re.compile(
    r"TranscribeAction::start called for binding"
    r"|Recording started for binding"
)
# A recording that never began ends the same way: Handy logs "start called" and then
# returns early (no microphone, no model) without ever logging a stop. Through 9.36.5
# that left the queue paused until the daemon was restarted (2026-09-30, six minutes).
# A cancelled recording (the cancel shortcut, Escape by default) never reaches
# TranscribeAction::stop: CancelAction calls utils::cancel_current_operation, which logs
# "Initiating operation cancellation..." at INFO and discards the samples, so no stop line
# and no WAV follow (Handy src-tauri/src/utils.rs, read 2026-10-01). Through 9.37.0 the
# watcher took that for a recording still open and held the queue until the cap or a restart,
# and the restarted daemon's tail scan re-read the same start and paused again (19:18:59).
_RE_RECORDING_STOP = re.compile(
    r"TranscribeAction::stop called for binding"
    r"|Recording stopped and samples retrieved"
    r"|Recording produced no audio samples"
    r"|No samples retrieved from recording stop"
    r"|Failed to start recording"
    r"|Not starting recording"
    r"|Initiating operation cancellation"
)

# Handy's log_level setting: string since 0.7, numeric 1-5 before that
# (settings.rs LogLevel deserializer: 1 trace, 2 debug, 3 info, 4 warn, 5 error).
_HANDY_NUMERIC_LEVELS = {1: "trace", 2: "debug", 3: "info", 4: "warn", 5: "error"}
_LEVELS_THAT_SHOW_RECORDING = ("trace", "debug")


def handy_settings() -> dict:
    """Handy's saved settings, or {} if unreadable."""
    try:
        store = json.loads(HANDY_SETTINGS.read_text())
        settings = store.get("settings", store)
        return settings if isinstance(settings, dict) else {}
    except (OSError, ValueError, AttributeError):
        return {}


def handy_file_log_level(settings: dict | None = None) -> str | None:
    """Handy's file log level as a lower-case word, or None if unknown."""
    if settings is None:
        settings = handy_settings()
    raw = settings.get("log_level")
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return _HANDY_NUMERIC_LEVELS.get(raw)
    if isinstance(raw, str) and raw:
        return raw.lower()
    return None


def handy_log_level_hides_recording(level: str | None) -> bool:
    """True when Handy's file log will never contain recording start/stop lines."""
    return level is not None and level not in _LEVELS_THAT_SHOW_RECORDING

# tauri-plugin-log stamps every line "[YYYY-MM-DD][HH:MM:SS]". Whether that clock is UTC or
# local is the app's choice, so an age is read both ways and the smaller wins: a recording
# that is really open is recent under one of them, and a start from twenty minutes ago is old
# under both (the readings differ by the UTC offset, hours, never minutes).
_RE_LOG_TIMESTAMP = re.compile(r"^\[(\d{4}-\d\d-\d\d)\]\[(\d\d:\d\d:\d\d)\]")

# A recording start this old with no stop after it is over, whatever Handy failed to log.
# The daemon passes mic_pause_max_s so the startup scan and the live cap agree; 0 disables.
STALE_START_S = 600.0
# How far back the startup scan reads at most. 8 KB missed a start 139 s back on 2026-10-01.
INITIAL_SCAN_BYTES = 8 * 1024 * 1024


def log_line_age_s(line: str, now: float | None = None, clock: str | None = None) -> float | None:
    """Seconds since the line's timestamp, or None when the line carries none.

    clock "utc" or "local" says which clock the log uses (see handy_log_clock);
    None keeps the smaller of the two readings, which is right for a line a few
    minutes old and wrong for one exactly the UTC offset old.
    """
    m = _RE_LOG_TIMESTAMP.match(line)
    if not m:
        return None
    try:
        stamp = datetime.strptime(f"{m[1]} {m[2]}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    t = time.time() if now is None else now
    as_local = stamp.timestamp()
    as_utc = stamp.replace(tzinfo=timezone.utc).timestamp()
    if clock == "utc":
        return abs(t - as_utc)
    if clock == "local":
        return abs(t - as_local)
    return min(abs(t - as_local), abs(t - as_utc))


def handy_log_clock(lines: list[str], mtime: float) -> str | None:
    """Which clock stamps the log, "utc" or "local", read from its last stamped line.

    The last line was written about when the file was last modified, so the
    reading that lands within minutes of mtime is the log's clock. None when
    the two readings agree (a UTC machine) or neither fits.
    """
    for line in reversed(lines):
        m = _RE_LOG_TIMESTAMP.match(line)
        if not m:
            continue
        try:
            stamp = datetime.strptime(f"{m[1]} {m[2]}", "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        d_local = abs(mtime - stamp.timestamp())
        d_utc = abs(mtime - stamp.replace(tzinfo=timezone.utc).timestamp())
        if d_local == d_utc:
            return None
        best, other = (("local", d_local), ("utc", d_utc)) if d_local < d_utc else (("utc", d_utc), ("local", d_local))
        return best[0] if best[1] <= 600 and other[1] > 600 else None
    return None


# How long to wait after recording stops before resuming TTS (ms).
# Gives Handy time to transcribe + paste before TTS resumes.
RESUME_DELAY_MS = 1500

# How often to check for log rotation (seconds).
# Prevents false positives from inode races during active writes.
ROTATION_CHECK_INTERVAL = 2.0


class MicWatcher:
    _last_rotation_check: float
    """Watches Handy's log for recording events, pauses/resumes TTS."""

    def __init__(
        self,
        log_fn: Callable[..., Any],
        read_playback_state: Callable[..., Any],
        write_playback_state: Callable[..., Any],
        resume_delay_ms: int = RESUME_DELAY_MS,
        stale_start_s: float = STALE_START_S,
    ) -> None:
        self._log = log_fn
        self._read_state = read_playback_state
        self._write_state = write_playback_state
        self._resume_delay = resume_delay_ms / 1000.0
        self._stale_start_s = stale_start_s
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._recording = False

    @property
    def active(self) -> bool:
        """True if the watcher thread is running."""
        return self._thread is not None and self._thread.is_alive()

    @property
    def recording(self) -> bool:
        """True if mic is currently recording."""
        return self._recording

    def _check_initial_mic_state(self) -> bool:
        """True if Handy's log says the mic is recording now (see _initial_mic_event)."""
        return self._initial_mic_event() == "recording"

    def _initial_mic_event(self) -> str:
        """What the tail of Handy's log says: "recording", "stopped" or "none".

        Walks the recent lines backwards to the last recording event. Handy at
        debug writes far more than 8 KB in two minutes of recording: on
        2026-10-01 a daemon restarted 139 s into a dictation read an 8 KB tail,
        found no start in it, and spoke over the person dictating. The walk
        covers INITIAL_SCAN_BYTES; a line cut by that boundary is dropped; the
        age of the start it finds decides, read on the clock the log uses.
        """
        try:
            with open(HANDY_LOG, errors="replace") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - INITIAL_SCAN_BYTES))
                tail = f.read()
            mtime = os.path.getmtime(HANDY_LOG)
        except OSError:
            return "none"

        lines = tail.splitlines()
        if size > INITIAL_SCAN_BYTES and lines:
            lines = lines[1:]  # the first line is cut by the window; a half start line is not a start
        last_start = -1
        last_stop = -1
        start_line = ""
        for i in range(len(lines) - 1, -1, -1):
            line = lines[i]
            if _RE_RECORDING_START.search(line):
                last_start = i
                start_line = line
                break
            if _RE_RECORDING_STOP.search(line):
                last_stop = i
                break

        if last_start > last_stop:
            # A start with no stop after it is either a recording in progress or one whose
            # end Handy never logged. 2026-09-30: one such start (19:18:59) paused two
            # successive daemons at startup, 3 and 17 minutes after the fact. Its age decides.
            age = log_line_age_s(start_line, clock=handy_log_clock(lines, mtime))
            if age is not None and 0 < self._stale_start_s < age:
                self._log(
                    f"Mic watcher: Handy log ends in a recording start from {age:.0f}s ago with no "
                    "stop after it; treating that recording as over"
                )
                return "stopped"
            self._log("Mic watcher: Handy log shows mic is currently recording")
            return "recording"
        if last_stop >= 0:
            return "stopped"
        return "none"

    def start(self, carried_recording: bool = False) -> bool:
        """Start the watcher thread. Returns False if log file not found.

        carried_recording: the previous daemon was paused by the mic moments ago
        (the daemon keeps that hold across a restart); start as recording, so the
        stop line Handy logs next resumes the queue, whatever the log tail shows.
        """
        if not HANDY_LOG.exists():
            self._log(f"Mic watcher: Handy log not found at {HANDY_LOG}", "WARN")
            return False

        # Handy only logs recording events at debug. Say so once, loudly, rather
        # than tailing a file that will never mention a recording (2026-09-22:
        # "it pauses then immediately plays" was Handy's own mute, not us).
        settings = handy_settings()
        level = handy_file_log_level(settings)
        if handy_log_level_hides_recording(level):
            self._log(
                f"Mic watcher: Handy's log level is '{level}', but Handy only logs "
                "recording start/stop at debug, so mic-aware pause will never fire. "
                "In Handy: Settings > Debug > Log Level > Debug (applies at once).",
                "WARN",
            )
        elif level is None:
            self._log(
                f"Mic watcher: could not read Handy's log level from {HANDY_SETTINGS}; "
                "mic-aware pause needs it set to Debug",
                "WARN",
            )
        if settings.get("mute_while_recording"):
            self._log(
                "Mic watcher: Handy's own mute_while_recording is on; Handy will also mute "
                "the Mac's output during recording, independent of this pause"
            )

        # Check if mic is currently recording before we start tailing.
        # This handles the case where the daemon restarts mid-recording.
        event = self._initial_mic_event()
        if carried_recording and event == "stopped":
            # Handy logged the stop during the restart gap; the tail below opens at the
            # end of the file and would never see it (review of e6be5c9: silent to the cap).
            self._log("Mic watcher: the previous daemon's mic hold ended during the restart; resuming")
            self._recording = False
            self._resume_after_mic()
        elif carried_recording or event == "recording":
            if carried_recording and event == "none":
                self._log("Mic watcher: the previous daemon was paused by the mic; holding until Handy logs the stop")
            self._recording = True
            self._pause_for_mic()

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._watch_loop,
            name="mic-watcher",
            daemon=True,
        )
        self._thread.start()
        self._log(f"Mic watcher started (resume delay: {self._resume_delay * 1000:.0f}ms)")
        return True

    def stop(self) -> None:
        """Signal the watcher to stop."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._log("Mic watcher stopped")

    def _pause_for_mic(self) -> None:
        """Pause TTS for mic recording."""
        state = self._read_state()
        if state.get("paused"):
            # Already paused (manual or mic) — don't overwrite paused_by
            self._log("Mic watcher: already paused, noting mic active")
            return
        self._write_state(paused=True, paused_by="mic")
        self._log("Mic watcher: paused for recording")

    def _resume_after_mic(self) -> None:
        """Resume TTS after mic recording, respecting manual pause."""
        state = self._read_state()
        if not state.get("paused"):
            return  # Already unpaused

        paused_by = state.get("paused_by", "user")
        if paused_by != "mic":
            # Manual pause — don't override
            self._log("Mic watcher: recording done, but manual pause active — staying paused")
            return

        self._write_state(paused=False, paused_by=None)
        self._log("Mic watcher: resumed after recording")

    def _check_rotation(self, f: IO[Any]) -> bool:
        """Check if the log file was rotated. Debounced to avoid false positives."""
        now = time.monotonic()
        if now - self._last_rotation_check < ROTATION_CHECK_INTERVAL:
            return False
        self._last_rotation_check = now
        try:
            current_inode = os.stat(HANDY_LOG).st_ino
            fd_inode = os.fstat(f.fileno()).st_ino
            if current_inode != fd_inode:
                self._log("Mic watcher: log rotated, reopening")
                return True
        except OSError:
            pass
        return False

    def _watch_once(self) -> None:
        """Tail the Handy log file until it rotates, errors, or a stop is requested."""
        self._last_rotation_check = time.monotonic()

        try:
            # Open and seek to end — we only care about new events
            with open(HANDY_LOG) as f:
                f.seek(0, os.SEEK_END)
                self._log(f"Mic watcher: tailing {HANDY_LOG}")

                while not self._stop_event.is_set():
                    line = f.readline()
                    if not line:
                        if self._check_rotation(f):
                            break  # Will restart in outer loop
                        self._stop_event.wait(0.05)
                        continue

                    line = line.strip()
                    if not line:
                        continue

                    if _RE_RECORDING_START.search(line):
                        self._recording = True
                        self._pause_for_mic()

                    elif _RE_RECORDING_STOP.search(line):
                        if not self._recording:
                            # Handy logs several stop-side lines per recording;
                            # the first one already resumed us.
                            continue
                        self._recording = False
                        # Wait for transcription + paste before resuming
                        self._stop_event.wait(self._resume_delay)
                        if not self._stop_event.is_set():
                            self._resume_after_mic()

        except Exception as e:
            self._log(f"Mic watcher error: {e}", "ERROR")

    def _watch_loop(self) -> None:
        """Tail the log until stopped; reopen after a rotation or an error.

        A loop, not a recursive call: through 9.33.1 every rotation or error added a stack
        frame, and a log that kept failing to open would have ended in RecursionError.
        """
        while True:
            self._watch_once()
            if self._stop_event.is_set():
                return
            self._log("Mic watcher: restarting after rotation/error")
            time.sleep(0.5)
