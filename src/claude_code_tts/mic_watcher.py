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
_RE_RECORDING_STOP = re.compile(
    r"TranscribeAction::stop called for binding"
    r"|Recording stopped and samples retrieved"
    r"|Recording produced no audio samples"
    r"|No samples retrieved from recording stop"
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
    ) -> None:
        self._log = log_fn
        self._read_state = read_playback_state
        self._write_state = write_playback_state
        self._resume_delay = resume_delay_ms / 1000.0
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
        """Scan the tail of the Handy log to determine if mic is currently recording.

        Reads the last ~50 lines and finds the most recent recording event.
        Returns True if the mic appears to be actively recording.
        """
        try:
            with open(HANDY_LOG) as f:
                # Read last 8KB -- enough for ~50 lines
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 8192))
                tail = f.read()
        except OSError:
            return False

        # Find the last recording start and stop events
        last_start = -1
        last_stop = -1
        for i, line in enumerate(tail.splitlines()):
            if _RE_RECORDING_START.search(line):
                last_start = i
            elif _RE_RECORDING_STOP.search(line):
                last_stop = i

        if last_start > last_stop:
            self._log("Mic watcher: Handy log shows mic is currently recording")
            return True
        return False

    def start(self) -> bool:
        """Start the watcher thread. Returns False if log file not found."""
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
        if self._check_initial_mic_state():
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

    def _watch_loop(self) -> None:
        """Tail the Handy log file, watching for recording events."""
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

        # If we broke out (rotation or error), restart unless stopping
        if not self._stop_event.is_set():
            self._log("Mic watcher: restarting after rotation/error")
            time.sleep(0.5)
            self._watch_loop()
