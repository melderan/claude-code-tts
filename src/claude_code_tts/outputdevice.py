"""The sound output device the Mac plays through, as macOS reports it.

The daemon polls this on macOS and writes the answer into playback.json (`output_device`), so a
room that cannot hear the Mac can still read which device is the default output: the built-in
speakers, a Bluetooth headset, a USB interface. The ask (the night watch, 2026-10-06): a person's
headphones leaving the Mac is the tell that the house should fall silent, and the watcher needs
to see that tell from a room. Only the system profiler is asked, nothing is changed.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

from claude_code_tts.audio import detect_platform

# How often the daemon asks macOS which device is the default output. system_profiler takes
# about a second, off the play loop, so every 15 s is cheap and a headset change is seen soon.
POLL_EVERY_S = 15.0

# system_profiler is slow when a Bluetooth stack is busy; past this we report nothing this round.
PROFILER_TIMEOUT_S = 20.0

_DEFAULT_OUTPUT = "coreaudio_default_audio_output_device"
_TRANSPORT = "coreaudio_device_transport"
_YES = "spaudio_yes"
# The profiler's transport values carry a prefix (`spaudio_builtin`; one reviewer remembers
# `coreaudio_device_type_builtin` on some releases); either goes, the word stays.
_TRANSPORT_PREFIX = re.compile(r"^(spaudio_|coreaudio_device_type_|coreaudio_)")
_UNSEEN = object()  # the watch has not probed yet, so the first answer, even None, is logged

# The daemon runs as a launchd service whose PATH has no /usr/sbin (9.49.0 asked by name and
# never found it); the absolute path is where macOS keeps it, the bare name is the fallback.
PROFILER = "/usr/sbin/system_profiler"

_last_error = ""


def last_error() -> str:
    """Why the last probe answered nothing, for the log; empty after a probe that answered."""
    return _last_error


def parse_default_output(data: object) -> dict | None:
    """The default output device in a `system_profiler SPAudioDataType -json` document.

    Returns {"name", "transport"} for the item flagged as the default output; transport is
    the profiler's word without its `spaudio_` prefix (`builtin`, `bluetooth`, `usb`, ...),
    empty when the profiler gave none. None when no item is flagged or the shape is off.
    """
    if not isinstance(data, dict):
        return None
    sections = data.get("SPAudioDataType")
    if not isinstance(sections, list):
        return None
    for section in sections:
        if not isinstance(section, dict):
            continue
        items = section.get("_items")
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict) or item.get(_DEFAULT_OUTPUT) != _YES:
                continue
            name = item.get("_name")
            if not isinstance(name, str) or not name.strip():
                continue
            transport = item.get(_TRANSPORT)
            transport = transport if isinstance(transport, str) else ""
            return {"name": name.strip(), "transport": _TRANSPORT_PREFIX.sub("", transport)}
    return None


def current_output_device(timeout_s: float = PROFILER_TIMEOUT_S) -> dict | None:
    """Ask macOS for the default output device; None anywhere else or when it cannot say (see last_error)."""
    global _last_error
    if detect_platform() != "macos":
        _last_error = "not macOS"
        return None
    binary = PROFILER if Path(PROFILER).exists() else (shutil.which("system_profiler") or PROFILER)
    try:
        proc = subprocess.run(
            [binary, "SPAudioDataType", "-json"],
            capture_output=True, text=True, timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        _last_error = f"{binary} took over {timeout_s:.0f}s"
        return None
    except OSError as e:
        _last_error = f"{binary} did not run: {e}"
        return None
    if proc.returncode != 0:
        _last_error = f"{binary} exit {proc.returncode}: {proc.stderr.strip()[-200:]}"
        return None
    if not proc.stdout.strip():
        _last_error = f"{binary} printed nothing"
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        _last_error = f"{binary} output is not JSON: {e}"
        return None
    device = parse_default_output(data)
    _last_error = "" if device else "no item is flagged as the default output"
    return device


def describe(device: dict | None, now: float | None = None) -> str:
    """One line for a status reader: `MacBook Pro Speakers (builtin), checked 4s ago`."""
    if not device or not device.get("name"):
        return "unknown"
    text = str(device["name"])
    if device.get("transport"):
        text += f" ({device['transport']})"
    checked = device.get("checked_at")
    if isinstance(checked, (int, float)) and not isinstance(checked, bool):
        t = time.time() if now is None else now
        text += f", checked {max(0.0, t - float(checked)):.0f}s ago"
    return text


class OutputDeviceWatch:
    """A thread that asks macOS for the default output device and records it (state.write_output_device).

    The probe runs off the play loop (system_profiler takes about a second). Every answer is
    written so `checked_at` says how fresh it is; a probe that gives nothing writes nothing, so
    the last device seen stands with its own stamp (the profiler is slowest while a Bluetooth
    headset connects, which is exactly the change to show). A change is one log line.
    Starts only on macOS; elsewhere start() returns False and nothing runs.
    """

    def __init__(
        self,
        log_fn: Callable[[str, str], None],
        write_state: Callable[[dict | None], None],
        poll_every_s: float = POLL_EVERY_S,
        probe: Callable[[], dict | None] = current_output_device,
        platform: Callable[[], str] = detect_platform,
    ) -> None:
        self._log = log_fn
        self._write_state = write_state
        self._poll_every_s = poll_every_s
        self._probe = probe
        self._platform = platform
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last: dict | None | object = _UNSEEN

    def start(self) -> bool:
        if self._platform() != "macos":
            return False
        self._thread = threading.Thread(target=self._run, name="tts-output-device", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def poll_once(self) -> dict | None:
        """One probe and, when it answered, one write; the device as seen (None when macOS could not say)."""
        device = self._probe()
        if device != self.last:
            if device:
                self._log(f"Output device: {device['name']} ({device.get('transport') or 'unknown transport'})", "INFO")
            else:
                self._log(f"Output device: unknown this round ({last_error() or 'the probe answered nothing'}); the last one seen stands", "WARN")
            self.last = device
        if device and not self._stop.is_set():  # a probe that outlived stop() writes nothing
            self._write_state(device)
        return device

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as e:  # noqa: BLE001  a probe must never take the thread down
                self._log(f"Output device probe failed: {e}", "WARN")
            if self._stop.wait(self._poll_every_s):
                return
