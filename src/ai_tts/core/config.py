"""
Configuration management for ai_tts.

Handles loading, saving, and merging config from multiple sources:
- Global config file (~/.ai-tts/config.json)
- Environment variables (AI_TTS_*)
- Runtime overrides

The config is a singleton loaded once and cached.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Config file locations
DEFAULT_CONFIG_DIR = Path.home() / ".ai-tts"
LEGACY_CONFIG_DIR = Path.home() / ".claude-tts"  # Backward compat


@dataclass
class Config:
    """Global TTS configuration."""

    # Defaults
    default_persona: str = "default"
    default_muted: bool = True  # New sessions start muted
    default_speed: float = 2.0

    # Playback
    mode: str = "direct"  # "direct" or "queue"
    max_chars: int = 10000

    # Paths
    config_dir: Path = field(default_factory=lambda: DEFAULT_CONFIG_DIR)
    voices_dir: Path = field(
        default_factory=lambda: Path.home() / ".local/share/piper-voices"
    )

    # Registered personas (name -> Persona config)
    personas: dict[str, dict[str, Any]] = field(default_factory=dict)

    # Session overrides (session_id -> settings)
    sessions: dict[str, dict[str, Any]] = field(default_factory=dict)

    # Project personas (project_id -> persona_name)
    project_personas: dict[str, str] = field(default_factory=dict)

    def save(self) -> None:
        """Save config to disk."""
        self.config_dir.mkdir(parents=True, exist_ok=True)
        config_file = self.config_dir / "config.json"

        data = {
            "default_persona": self.default_persona,
            "default_muted": self.default_muted,
            "default_speed": self.default_speed,
            "mode": self.mode,
            "max_chars": self.max_chars,
            "personas": self.personas,
            "sessions": self.sessions,
            "project_personas": self.project_personas,
        }

        with open(config_file, "w") as f:
            json.dump(data, f, indent=2)

    @classmethod
    def load(cls, config_dir: Path | None = None) -> Config:
        """Load config from disk, with fallback to legacy location."""
        # Try new location first, then legacy
        dirs_to_try = [
            config_dir or DEFAULT_CONFIG_DIR,
            LEGACY_CONFIG_DIR,
        ]

        for dir_path in dirs_to_try:
            config_file = dir_path / "config.json"
            if config_file.exists():
                with open(config_file) as f:
                    data = json.load(f)

                return cls(
                    default_persona=data.get("default_persona", data.get("active_persona", "default")),
                    default_muted=data.get("default_muted", True),
                    default_speed=data.get("default_speed", data.get("speed", 2.0)),
                    mode=data.get("mode", "direct"),
                    max_chars=data.get("max_chars", 10000),
                    config_dir=dir_path,
                    personas=data.get("personas", {}),
                    sessions=data.get("sessions", {}),
                    project_personas=data.get("project_personas", {}),
                )

        # No config found, return defaults
        return cls()


# Singleton instance
_config: Config | None = None


def get_config() -> Config:
    """Get the global config instance (loads from disk on first call)."""
    global _config
    if _config is None:
        _config = Config.load()
    return _config


def configure(**kwargs: Any) -> Config:
    """Update global config with new values."""
    global _config
    if _config is None:
        _config = Config.load()

    for key, value in kwargs.items():
        if hasattr(_config, key):
            setattr(_config, key, value)

    return _config


def reload_config() -> Config:
    """Force reload config from disk."""
    global _config
    _config = Config.load()
    return _config
