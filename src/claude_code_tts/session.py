"""Session ID resolution for Claude Code TTS.

The canonical session ID is the folder name Claude Code uses under
~/.claude/projects/ for the active session. Hooks know this exactly because
Claude Code passes them the transcript path; the CLI side has no such luxury.

To bridge that gap, the hook handler "pins" the canonical session ID to a
file keyed by the parent `claude` process PID (see pin_session). The CLI
walks its process tree to find the same `claude` ancestor and reads the
pinned ID, guaranteeing both sides agree.

PROJECT_ROOT and CWD encoding remain as fallbacks for contexts where no
hook has fired yet (or where claude-tts is invoked outside of Claude Code).
"""

import os
import re
import subprocess
from pathlib import Path

ACTIVE_DIR = Path.home() / ".claude-tts" / "active"


def _path_to_session_id(path: str) -> str:
    """Convert a filesystem path to a Claude Code-style session ID.

    Claude Code encodes paths by replacing each non-alphanumeric character
    with a dash: /Users/foo/_bar/baz → -Users-foo--bar-baz
    """
    return re.sub(r"[^a-zA-Z0-9]", "-", path)


def _ps_query(pid: int) -> tuple[int, str] | None:
    """Return (ppid, comm) for a PID, or None if the process is gone."""
    try:
        out = subprocess.run(
            ["ps", "-o", "ppid=,comm=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return None
    if not out:
        return None
    parts = out.split(None, 1)
    if len(parts) < 2:
        return None
    try:
        return int(parts[0]), parts[1].strip()
    except ValueError:
        return None


def find_claude_ancestor_pid(start_pid: int | None = None) -> int | None:
    """Walk up the process tree to find the nearest `claude` ancestor PID.

    Returns None if no claude ancestor is found within 20 hops.
    """
    pid = start_pid if start_pid is not None else os.getppid()
    for _ in range(20):
        if pid is None or pid <= 1:
            return None
        info = _ps_query(pid)
        if info is None:
            return None
        ppid, comm = info
        # comm is the basename of the executable
        if Path(comm).name == "claude":
            return pid
        pid = ppid
    return None


def _pin_path(claude_pid: int) -> Path:
    return ACTIVE_DIR / f"{claude_pid}.session"


def pin_session(session_id: str) -> None:
    """Write the canonical session ID to a file keyed by the claude ancestor PID.

    Called by the hook handler once it has resolved the session ID from the
    transcript path. Subsequent CLI invocations under the same `claude`
    parent will read this file via read_pinned_session().

    No-op if no claude ancestor can be found or the write fails.
    """
    claude_pid = find_claude_ancestor_pid()
    if claude_pid is None:
        return
    try:
        ACTIVE_DIR.mkdir(parents=True, exist_ok=True)
        path = _pin_path(claude_pid)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(session_id)
        tmp.replace(path)
    except OSError:
        pass


def read_pinned_session() -> str | None:
    """Read the pinned session ID for the current claude ancestor, if any."""
    claude_pid = find_claude_ancestor_pid()
    if claude_pid is None:
        return None
    path = _pin_path(claude_pid)
    if not path.is_file():
        return None
    try:
        content = path.read_text().strip()
    except OSError:
        return None
    return content or None


def cleanup_stale_pins() -> int:
    """Remove pin files for claude PIDs that no longer exist.

    Returns the number of files removed.
    """
    if not ACTIVE_DIR.is_dir():
        return 0
    removed = 0
    for entry in ACTIVE_DIR.iterdir():
        if not entry.is_file() or not entry.name.endswith(".session"):
            continue
        try:
            pid = int(entry.stem)
        except ValueError:
            continue
        if _ps_query(pid) is None:
            try:
                entry.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def get_session_id() -> str:
    """Resolve the current Claude Code session ID.

    Priority:
        1. CLAUDE_TTS_SESSION env override
        2. Pinned session (canonical, set by hooks from transcript_path)
        3. PROJECT_ROOT → scan ~/.claude/projects/ for matching folder (legacy)
        4. CWD fallback (path-to-session-id encoding)
    """
    # 1. Explicit override
    if override := os.environ.get("CLAUDE_TTS_SESSION"):
        return override

    # 2. Pinned by hook — canonical, matches what hooks see from transcript_path
    if pinned := read_pinned_session():
        return pinned

    # 3. PROJECT_ROOT scan (legacy — kept for backward compatibility)
    project_root = os.environ.get("PROJECT_ROOT")
    if project_root:
        projects_dir = Path.home() / ".claude" / "projects"
        if projects_dir.is_dir():
            target = re.sub(r"[^a-zA-Z0-9]", "", project_root)
            for entry in projects_dir.iterdir():
                if not entry.is_dir():
                    continue
                candidate = re.sub(r"[^a-zA-Z0-9]", "", entry.name)
                if candidate == target:
                    return entry.name
        return _path_to_session_id(project_root)

    # 4. Fallback for non-Claude-Code contexts
    return _path_to_session_id(os.getcwd())
