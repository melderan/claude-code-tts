"""
Gemini CLI adapter for ai_tts.

STATUS: Placeholder - waiting for Gemini CLI to implement hooks.

Tracking:
- https://github.com/google-gemini/gemini-cli/issues/2779
- https://github.com/google-gemini/gemini-cli/issues/9070

Once hooks land, this adapter will handle:
- Session detection from Gemini CLI's project structure
- Text extraction from Gemini's response format
- Hook integration
"""

from __future__ import annotations

import os
from pathlib import Path

from ai_tts.adapters import BaseAdapter


class GeminiCLIAdapter(BaseAdapter):
    """Adapter for Gemini CLI (google-gemini/gemini-cli)."""

    name = "gemini"
    display_name = "Gemini CLI"

    # TODO: Update when Gemini CLI hook system is documented
    PROJECTS_DIR = Path.home() / ".gemini"  # Placeholder

    def detect_session(self) -> str | None:
        """
        Detect session from Gemini CLI's project structure.

        TODO: Implement once Gemini CLI documents their structure.
        """
        # For now, fall back to PWD-based detection
        pwd = os.getcwd()
        return f"gemini-{pwd.replace('/', '-').replace('_', '-')}"

    def extract_text(self, event_data: dict) -> str | None:
        """
        Extract text from Gemini CLI's hook event.

        TODO: Implement once Gemini CLI documents their event format.
        """
        # Placeholder - assume similar structure to Claude Code
        transcript_path = event_data.get("transcript_path")
        if not transcript_path:
            # Maybe they use a different key
            transcript_path = event_data.get("conversation_path")

        if not transcript_path:
            return None

        # TODO: Parse Gemini's transcript format
        return None

    def is_available(self) -> bool:
        """Check if Gemini CLI is installed."""
        import shutil

        return shutil.which("gemini") is not None


# Don't register yet - adapter is not functional
# GeminiCLIAdapter.register()
