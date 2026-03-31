"""Session ID resolution for Claude Code TTS.

Maps PROJECT_ROOT (filesystem path) to the Claude Code project folder name
in ~/.claude/projects/. Claude Code's encoding isn't a simple tr '/' '-' —
it also strips underscores, dots, and other non-alphanumeric chars. We match
by comparing alphanumeric content only.
"""

import os
import re
from pathlib import Path


def _path_to_session_id(path: str) -> str:
    """Convert a filesystem path to a Claude Code-style session ID.

    Claude Code encodes paths by replacing each non-alphanumeric character
    with a dash: /Users/foo/_bar/baz → -Users-foo--bar-baz
    """
    return re.sub(r"[^a-zA-Z0-9]", "-", path)


def get_session_id() -> str:
    """Resolve the current Claude Code session ID.

    Priority:
        1. CLAUDE_TTS_SESSION env override
        2. PROJECT_ROOT → scan ~/.claude/projects/ for matching folder
        3. PWD fallback (path-to-session-id encoding)
    """
    # Explicit override
    if override := os.environ.get("CLAUDE_TTS_SESSION"):
        return override

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
        # Fallback if ~/.claude/projects/ lookup fails
        return _path_to_session_id(project_root)

    # Fallback for non-Claude-Code contexts
    return _path_to_session_id(os.getcwd())
