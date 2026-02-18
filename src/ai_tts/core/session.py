"""
Session management for ai_tts.

Sessions track per-terminal/per-project state:
- Mute status
- Active persona override
- Speed override

Session IDs can come from:
- Environment variable (AI_TTS_SESSION or CLAUDE_TTS_SESSION)
- CLI tool adapter (each tool has its own session detection)
- Fallback to current working directory hash
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ai_tts.core.config import get_config


@dataclass
class Session:
    """A TTS session with its own settings."""

    id: str
    muted: bool = False
    persona: str | None = None  # None = use project or global default
    speed: float | None = None  # None = use persona default

    @classmethod
    def from_config(cls, session_id: str) -> Session:
        """Load session from config, creating if needed."""
        config = get_config()
        session_data = config.sessions.get(session_id, {})

        return cls(
            id=session_id,
            muted=session_data.get("muted", config.default_muted),
            persona=session_data.get("persona"),
            speed=session_data.get("speed"),
        )

    def save(self) -> None:
        """Save session state to config."""
        config = get_config()

        if self.id not in config.sessions:
            config.sessions[self.id] = {}

        session_data = config.sessions[self.id]
        session_data["muted"] = self.muted

        if self.persona is not None:
            session_data["persona"] = self.persona
        elif "persona" in session_data:
            del session_data["persona"]

        if self.speed is not None:
            session_data["speed"] = self.speed
        elif "speed" in session_data:
            del session_data["speed"]

        config.save()

    def get_effective_persona(self) -> str:
        """Get the persona to use, respecting hierarchy: session > project > global."""
        config = get_config()

        # Session override takes priority
        if self.persona:
            return self.persona

        # Then project default
        project_persona = config.project_personas.get(self.id)
        if project_persona:
            return project_persona

        # Finally global default
        return config.default_persona


def get_session_id(cli_tool: str | None = None) -> str:
    """
    Get the current session ID.

    Priority:
    1. AI_TTS_SESSION env var (explicit override)
    2. CLAUDE_TTS_SESSION env var (legacy)
    3. CLI tool adapter detection (if cli_tool specified)
    4. PWD-based fallback

    Args:
        cli_tool: Optional CLI tool name for adapter-specific detection.
                  Supported: "claude", "gemini", "codex", "devstral"
    """
    # Check env vars first
    for var in ("AI_TTS_SESSION", "CLAUDE_TTS_SESSION"):
        if session := os.environ.get(var):
            return session

    # Try CLI-specific adapter
    if cli_tool:
        from ai_tts.adapters import get_adapter

        adapter = get_adapter(cli_tool)
        if adapter and (session := adapter.detect_session()):
            return session

    # Fallback: transform PWD like Claude Code does
    # /Users/foo/_bar -> -Users-foo--bar
    pwd = os.getcwd()
    return pwd.replace("/", "-").replace("_", "-")


def get_current_session(cli_tool: str | None = None) -> Session:
    """Get the current session, loading from config."""
    session_id = get_session_id(cli_tool)
    return Session.from_config(session_id)
