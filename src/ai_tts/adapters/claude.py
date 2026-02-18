"""
Claude Code adapter for ai_tts.

Handles:
- Session detection from Claude Code's project folders
- Text extraction from Claude Code's transcript format
- Hook integration via Stop event
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from ai_tts.adapters import BaseAdapter


class ClaudeCodeAdapter(BaseAdapter):
    """Adapter for Claude Code (anthropic/claude-code)."""

    name = "claude"
    display_name = "Claude Code"

    # Claude Code stores project data here
    PROJECTS_DIR = Path.home() / ".claude" / "projects"

    def detect_session(self) -> str | None:
        """
        Detect session from Claude Code's project folder structure.

        Claude Code creates folders like: ~/.claude/projects/-Users-foo-bar-project/
        The folder name is the path with / and _ replaced by -

        We find the LONGEST matching project folder for the current PWD.
        """
        # Transform PWD to Claude Code format
        pwd = os.getcwd()
        pwd_transformed = pwd.replace("/", "-").replace("_", "-")

        if not self.PROJECTS_DIR.exists():
            return None

        # Find longest matching project
        best_match = None
        best_length = 0

        for project_dir in self.PROJECTS_DIR.iterdir():
            if not project_dir.is_dir():
                continue

            project_name = project_dir.name
            if pwd_transformed.startswith(project_name):
                if len(project_name) > best_length:
                    best_match = project_name
                    best_length = len(project_name)

        return best_match

    def extract_text(self, event_data: dict) -> str | None:
        """
        Extract text from Claude Code's Stop hook event.

        The hook receives: {"transcript_path": "/path/to/transcript.jsonl"}

        We read the transcript and find the last assistant message with text content,
        skipping tool_use-only messages.
        """
        transcript_path = event_data.get("transcript_path")
        if not transcript_path:
            return None

        transcript_file = Path(transcript_path)
        if not transcript_file.exists():
            return None

        # Read last few lines of transcript (JSONL format)
        try:
            lines = transcript_file.read_text().strip().split("\n")
        except Exception:
            return None

        # Walk backwards to find the last assistant message with text
        for line in reversed(lines):
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            # Skip non-message entries
            if entry.get("type") != "message":
                continue

            message = entry.get("message", {})

            # Must be from assistant
            if message.get("role") != "assistant":
                continue

            # Look for text content
            content = message.get("content", [])
            if isinstance(content, str):
                return content

            # Content is array of blocks
            text_parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(block.get("text", ""))

            if text_parts:
                return "\n".join(text_parts)

        return None

    def is_available(self) -> bool:
        """Check if Claude Code is installed."""
        import shutil

        return shutil.which("claude") is not None

    def get_transcript_path_from_stdin(self) -> str | None:
        """
        Read transcript path from stdin (for hook integration).

        Claude Code hooks receive JSON on stdin.
        """
        import sys

        try:
            data = json.load(sys.stdin)
            return data.get("transcript_path")
        except Exception:
            return None


# Register the adapter
ClaudeCodeAdapter.register()
