"""The Mac's output volume, raised for one summon and put back after.

`claude-tts summon` queues a message with a "volume" field (1-100). The daemon raises the Mac's
output volume to at least that level and unmutes it while that message plays, then sets back the
level and mute it found. Only a summon does this; nothing else touches the person's volume.

Before raising it, the daemon writes what it found to ~/.claude-tts/volume-restore.json and
deletes the file once the old level is back. A daemon that dies mid-summon leaves the file, and
the next daemon puts the old level back at start, so a crash never leaves the Mac loud.

Only macOS is handled (osascript's `get volume settings`); elsewhere the message plays at the
volume the machine already has, and the daemon log says so.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

# The daemon runs under launchd, whose PATH may not hold /usr/bin tools by name.
OSASCRIPT = "/usr/bin/osascript"
OSASCRIPT_TIMEOUT_S = 5.0


def restore_file() -> Path:
    """Where the level a summon raised from is kept; read at call time so HOME decides."""
    return Path.home() / ".claude-tts" / "volume-restore.json"


_SETTINGS = re.compile(r"output volume:(\d+),.*output muted:(true|false)")

Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


@dataclass(frozen=True)
class Output:
    volume: int
    muted: bool


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, timeout=OSASCRIPT_TIMEOUT_S, check=False)


def parse_settings(text: str) -> Output | None:
    """`output volume:44, input volume:58, alert volume:100, output muted:false` -> Output(44, False).

    A device with no volume control answers `output volume:missing value`, which is None.
    """
    m = _SETTINGS.search(text)
    if not m:
        return None
    return Output(int(m.group(1)), m.group(2) == "true")


def get_output(run: Runner = _run) -> Output | None:
    """The output volume and mute as macOS reports them, or None if it cannot be read."""
    try:
        r = run([OSASCRIPT, "-e", "get volume settings"])
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_settings(r.stdout) if r.returncode == 0 else None


def set_output(out: Output, run: Runner = _run) -> bool:
    """Set the output volume and mute; True when osascript accepted it.

    Two statements, each the form Apple documents on its own, rather than one combined line.
    """
    mute = "with" if out.muted else "without"
    try:
        r = run([OSASCRIPT, "-e", f"set volume output volume {out.volume}", "-e", f"set volume {mute} output muted"])
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def clamp(level: object) -> int | None:
    """A summon's volume as an int 1-100, or None when the field is absent or not a number."""
    if isinstance(level, bool) or not isinstance(level, (int, float)):
        return None
    return max(1, min(100, int(level)))


def _write_restore(out: Output, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"volume": out.volume, "muted": out.muted}))
    tmp.rename(path)


def restore_left_over(
    log: Callable[[str, str], None], *, run: Runner = _run, path: Path | None = None
) -> bool:
    """Put back the level a summon raised if a daemon died before it could; True if it did."""
    path = path or restore_file()
    try:
        saved = json.loads(path.read_text())
        out = Output(int(saved["volume"]), bool(saved["muted"]))
    except FileNotFoundError:
        return False
    except (OSError, ValueError, KeyError, TypeError) as e:
        log(f"Unreadable {path.name} ({e}); removed, volume left as it is", "WARN")
        path.unlink(missing_ok=True)
        return False
    if set_output(out, run):
        log(f"Put the output volume back to {out.volume} (muted={out.muted}), left raised by a summon", "INFO")
        path.unlink(missing_ok=True)
        return True
    log(f"Could not put the output volume back to {out.volume}; {path.name} kept for the next start", "WARN")
    return False


@contextmanager
def raised(
    level: object,
    log: Callable[[str, str], None],
    *,
    run: Runner = _run,
    platform: str | None = None,
    path: Path | None = None,
) -> Generator[None, None, None]:
    """While the body runs, the output volume is at least `level` and unmuted; then as it was.

    No level (an ordinary message) does nothing. A volume already at or above the level and not
    muted is left alone, so a summon never turns anyone down.
    """
    target = clamp(level)
    if target is None:
        yield
        return
    if (platform or sys.platform) != "darwin":
        log(f"Summon asked for output volume {target}; not on macOS, played at the current volume", "WARN")
        yield
        return
    path = path or restore_file()
    before = get_output(run)
    if before is None:
        log(f"Summon asked for output volume {target}; the output volume cannot be read, left as it is", "WARN")
        yield
        return
    if before.volume >= target and not before.muted:
        yield
        return
    try:
        _write_restore(before, path)
    except OSError as e:
        # Without the record a crash could leave the Mac loud; play at the current volume.
        log(f"Could not write {path.name} ({e}); summon played at the current volume", "WARN")
        yield
        return
    if not set_output(Output(max(before.volume, target), False), run):
        path.unlink(missing_ok=True)
        log(f"Could not raise the output volume to {target}; played at the current volume", "WARN")
        yield
        return
    log(f"Output volume {before.volume} -> {max(before.volume, target)} (muted={before.muted}) for a summon", "INFO")
    try:
        yield
    finally:
        if set_output(before, run):
            path.unlink(missing_ok=True)
            log(f"Output volume back to {before.volume} (muted={before.muted})", "INFO")
        else:
            log(f"Could not put the output volume back to {before.volume}; the next start retries", "WARN")
