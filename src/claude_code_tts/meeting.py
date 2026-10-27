"""The meeting hold: while another app has the Mac's microphone open, no voice plays.

A call app (Zoom, Meet in a browser, a note taker that records the call) opens the microphone
for as long as the call runs, and a recorder that takes the system audio too records every
voice the daemon plays into the call. On 2026-10-26 about thirty read-aloud passages landed in
a colleague's transcript that way. So the daemon asks Core Audio which processes are capturing
input right now (the process objects of macOS 14.2 and later; reading them needs no permission)
and, while any app outside the ignore list is, it holds every voice: the rooms a person let
through, the bridge's reads and the lifecycle announcements too. Nothing is dropped for the
meeting; the held replies play after it, the way a person's hold keeps them.

The answer is polled, not listened for: on macOS 26 a listener on the running-input property
registers and never fires. The hold begins on the first poll that sees the app and ends only
after the microphone has been free for release_after_s, so a call app that closes the mic for
a moment (a mute, a device switch) does not let a voice into the next sentence.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import threading
import time
from collections.abc import Callable

from claude_code_tts.audio import detect_platform

# How often Core Audio is asked. One read of the process list is well under a millisecond, and
# the first words of a voice that starts just before a call's mic opens are the leak to keep short.
POLL_EVERY_S = 2.0

# How long the microphone must stay free before the voices play again.
RELEASE_AFTER_S = 10.0

# Handy keeps a recording hold of its own (mic_watcher.py), ended by its stop line; holding for
# it here as well would only add release_after_s of silence after every dictation.
DEFAULT_IGNORE = ("com.pais.handy",)

_last_error = ""


def last_error() -> str:
    """Why the last probe answered nothing, for the log; empty after a probe that answered."""
    return _last_error


def _fourcc(code: str) -> int:
    return int.from_bytes(code.encode("ascii"), "big")


# From CoreAudio's AudioHardware.h (macOS 14.2 SDK).
_SYSTEM_OBJECT = 1
_SCOPE_GLOBAL = _fourcc("glob")
_ELEMENT_MAIN = 0
_PROCESS_OBJECT_LIST = _fourcc("prs#")
_PROCESS_PID = _fourcc("ppid")
_PROCESS_BUNDLE_ID = _fourcc("pbid")
_PROCESS_IS_RUNNING_INPUT = _fourcc("piri")
_UTF8 = 0x08000100


class _Address(ctypes.Structure):
    _fields_ = [("selector", ctypes.c_uint32), ("scope", ctypes.c_uint32), ("element", ctypes.c_uint32)]


class _CoreAudio:
    """The few Core Audio and Core Foundation calls the probe makes, loaded once."""

    def __init__(self) -> None:
        self.ca = ctypes.CDLL(ctypes.util.find_library("CoreAudio") or "/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
        self.cf = ctypes.CDLL(
            ctypes.util.find_library("CoreFoundation")
            or "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        )
        self.libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        addr = ctypes.POINTER(_Address)
        u32p = ctypes.POINTER(ctypes.c_uint32)
        self.ca.AudioObjectHasProperty.argtypes = [ctypes.c_uint32, addr]
        self.ca.AudioObjectHasProperty.restype = ctypes.c_ubyte
        self.ca.AudioObjectGetPropertyDataSize.argtypes = [ctypes.c_uint32, addr, ctypes.c_uint32, ctypes.c_void_p, u32p]
        self.ca.AudioObjectGetPropertyDataSize.restype = ctypes.c_int32
        self.ca.AudioObjectGetPropertyData.argtypes = [
            ctypes.c_uint32, addr, ctypes.c_uint32, ctypes.c_void_p, u32p, ctypes.c_void_p,
        ]
        self.ca.AudioObjectGetPropertyData.restype = ctypes.c_int32
        self.cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
        self.cf.CFStringGetCString.restype = ctypes.c_ubyte
        self.cf.CFRelease.argtypes = [ctypes.c_void_p]
        self.libc.proc_name.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        self.libc.proc_name.restype = ctypes.c_int

    @staticmethod
    def _address(selector: int) -> _Address:
        return _Address(selector, _SCOPE_GLOBAL, _ELEMENT_MAIN)

    def has(self, obj: int, selector: int) -> bool:
        return bool(self.ca.AudioObjectHasProperty(obj, ctypes.byref(self._address(selector))))

    def processes(self) -> list[int]:
        address = self._address(_PROCESS_OBJECT_LIST)
        size = ctypes.c_uint32(0)
        status = self.ca.AudioObjectGetPropertyDataSize(_SYSTEM_OBJECT, ctypes.byref(address), 0, None, ctypes.byref(size))
        if status != 0:
            raise OSError(f"process list size: OSStatus {status}")
        count = size.value // ctypes.sizeof(ctypes.c_uint32)
        ids = (ctypes.c_uint32 * count)()
        status = self.ca.AudioObjectGetPropertyData(_SYSTEM_OBJECT, ctypes.byref(address), 0, None, ctypes.byref(size), ids)
        if status != 0:
            raise OSError(f"process list: OSStatus {status}")
        return list(ids[: size.value // ctypes.sizeof(ctypes.c_uint32)])

    def u32(self, obj: int, selector: int) -> int:
        value = ctypes.c_uint32(0)
        size = ctypes.c_uint32(ctypes.sizeof(value))
        status = self.ca.AudioObjectGetPropertyData(obj, ctypes.byref(self._address(selector)), 0, None, ctypes.byref(size), ctypes.byref(value))
        if status != 0:
            raise OSError(f"property {selector:#x} of {obj}: OSStatus {status}")
        return value.value

    def pid(self, obj: int) -> int:
        value = ctypes.c_int32(0)
        size = ctypes.c_uint32(ctypes.sizeof(value))
        status = self.ca.AudioObjectGetPropertyData(obj, ctypes.byref(self._address(_PROCESS_PID)), 0, None, ctypes.byref(size), ctypes.byref(value))
        return value.value if status == 0 else -1

    def bundle_id(self, obj: int) -> str:
        ref = ctypes.c_void_p(None)
        size = ctypes.c_uint32(ctypes.sizeof(ref))
        status = self.ca.AudioObjectGetPropertyData(obj, ctypes.byref(self._address(_PROCESS_BUNDLE_ID)), 0, None, ctypes.byref(size), ctypes.byref(ref))
        if status != 0 or not ref.value:
            return ""
        try:
            buf = ctypes.create_string_buffer(512)
            ok = self.cf.CFStringGetCString(ref, buf, len(buf), _UTF8)
            return buf.value.decode("utf-8", "replace") if ok else ""
        finally:
            self.cf.CFRelease(ref)

    def process_name(self, pid: int) -> str:
        buf = ctypes.create_string_buffer(256)
        n = self.libc.proc_name(pid, buf, len(buf))
        return buf.value.decode("utf-8", "replace") if n > 0 else ""


_CA: _CoreAudio | None = None


def apps_using_input() -> list[str] | None:
    """Every process capturing audio input now, by bundle id (else name[pid]); None when macOS cannot say.

    Sorted, one entry per app; this process is never in it. See last_error() for a None.
    """
    global _CA, _last_error
    if detect_platform() != "macos":
        _last_error = "not macOS"
        return None
    try:
        if _CA is None:
            _CA = _CoreAudio()
        if not _CA.has(_SYSTEM_OBJECT, _PROCESS_OBJECT_LIST):
            _last_error = "Core Audio has no process list (macOS before 14.2)"
            return None
        own = os.getpid()
        apps: set[str] = set()
        for obj in _CA.processes():
            try:
                if not _CA.u32(obj, _PROCESS_IS_RUNNING_INPUT):
                    continue
            except OSError:
                continue  # the process went away between the list and the read
            pid = _CA.pid(obj)
            if pid == own:
                continue
            name = _CA.bundle_id(obj) or f"{_CA.process_name(pid) or 'pid'}[{pid}]"
            apps.add(name)
    except (OSError, AttributeError, ValueError) as e:
        _last_error = f"Core Audio: {e}"
        return None
    _last_error = ""
    return sorted(apps)


def describe(meeting: object, now: float | None = None) -> str:
    """One line for a status reader: `zoom.us.xos has the microphone, for 12m`; "" when there is no meeting."""
    if not isinstance(meeting, dict) or not meeting.get("apps"):
        return ""
    text = f"{', '.join(str(a) for a in meeting['apps'])} {'has' if len(meeting['apps']) == 1 else 'have'} the microphone"
    since = meeting.get("since")
    if isinstance(since, (int, float)) and not isinstance(since, bool):
        held = max(0.0, (time.time() if now is None else now) - float(since))
        text += f", for {held / 60:.0f}m" if held >= 60 else f", for {held:.0f}s"
    return text


class MeetingWatch:
    """A thread that holds every voice while an app outside `ignore` has the microphone open.

    write_meeting(apps) sets the hold (state.set_meeting) and write_meeting(None) ends it. The
    hold starts on the first poll that sees such an app and ends once none has been seen for
    release_after_s. A probe that cannot answer changes nothing: a hold stands until the
    microphone is seen free, since the leak is the worse way to be wrong. `carried` is a hold
    an earlier daemon left (a restart mid-call); it stands until this watch sees the mic free.
    Starts only on macOS; elsewhere start() returns False and nothing runs.
    """

    def __init__(
        self,
        log_fn: Callable[[str, str], None],
        write_meeting: Callable[[list[str] | None], None],
        ignore: Callable[[], list[str]] = lambda: list(DEFAULT_IGNORE),
        probe: Callable[[], list[str] | None] = apps_using_input,
        platform: Callable[[], str] = detect_platform,
        poll_every_s: float = POLL_EVERY_S,
        release_after_s: float = RELEASE_AFTER_S,
        clock: Callable[[], float] = time.monotonic,
        carried: list[str] | None = None,
    ) -> None:
        self._log = log_fn
        self._write = write_meeting
        self._ignore = ignore
        self._probe = probe
        self._platform = platform
        self._poll_every_s = poll_every_s
        self._release_after_s = release_after_s
        self._clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.apps: list[str] = list(carried or [])
        self._last_seen = clock() if carried else 0.0
        self._blind = False

    def start(self) -> bool:
        """The first poll on the caller's thread, so a start mid-call holds before it says a word."""
        if self._platform() != "macos":
            return False
        try:
            self.poll_once()
        except Exception as e:  # noqa: BLE001  the thread polls again; a start never fails on a probe
            self._log(f"Meeting watch probe failed: {e}", "WARN")
        self._thread = threading.Thread(target=self._run, name="tts-meeting", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def poll_once(self) -> list[str]:
        """One probe and whatever write it calls for; the apps holding the voices after it."""
        seen = self._probe()
        if seen is None:
            if not self._blind:
                self._blind = True
                hold = f"; the meeting hold for {', '.join(self.apps)} stands" if self.apps else ""
                self._log(f"Meeting watch cannot see the microphone ({last_error() or 'no answer'}){hold}", "WARN")
            return self.apps
        if self._blind:
            self._blind = False
            self._log("Meeting watch sees the microphone again", "INFO")
        ignored = set(self._ignore())
        apps = [a for a in seen if a not in ignored]
        if self._stop.is_set():  # a probe that outlived stop() writes nothing
            return self.apps
        if apps:
            self._last_seen = self._clock()
            if apps != self.apps:
                if not self.apps:
                    self._log(f"Meeting: {', '.join(apps)} has the microphone; holding every voice until it is free", "INFO")
                else:
                    self._log(f"Meeting: now {', '.join(apps)}", "INFO")
                self.apps = apps
                self._write(apps)
        elif self.apps and self._clock() - self._last_seen >= self._release_after_s:
            self._log(f"Meeting over: the microphone has been free for {self._release_after_s:.0f}s; voices resume", "INFO")
            self.apps = []
            self._write(None)
        return self.apps

    def _run(self) -> None:
        while not self._stop.wait(self._poll_every_s):
            try:
                self.poll_once()
            except Exception as e:  # noqa: BLE001  a probe must never take the thread down
                self._log(f"Meeting watch probe failed: {e}", "WARN")


def probe_lines(seconds: float = 0.0, every: float = POLL_EVERY_S, ignore: list[str] | None = None) -> int:
    """What the probe sees, one line per poll for `seconds` (one poll at 0); `claude-tts meeting`.

    Reads Core Audio only; nothing is held, written or played. Exit 0 when every poll answered,
    1 when one could not see the microphone.
    """
    ignored = set(DEFAULT_IGNORE if ignore is None else ignore)
    deadline = time.monotonic() + seconds
    blind = False
    while True:
        seen = apps_using_input()
        stamp = time.strftime("%H:%M:%SZ", time.gmtime())
        if seen is None:
            blind = True
            print(f"{stamp} cannot see: {last_error()}", flush=True)
        else:
            held = [a for a in seen if a not in ignored]
            print(f"{stamp} input: {', '.join(seen) or '-'}; would hold: {'yes, for ' + ', '.join(held) if held else 'no'}", flush=True)
        if time.monotonic() + every > deadline:
            return 1 if blind else 0
        time.sleep(every)  # sleep-ok: the poll interval of a probe a person runs by hand
