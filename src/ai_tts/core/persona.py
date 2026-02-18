"""
Persona management for ai_tts.

A persona is a named voice configuration:
- Voice model (Piper .onnx file)
- Speed settings
- Speed method (playback vs length_scale)
- Optional speaker ID for multi-speaker models
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ai_tts.core.config import get_config


@dataclass
class Persona:
    """A voice personality configuration."""

    name: str
    voice: str  # e.g., "en_US-joe-medium"
    speed: float = 2.0
    speed_method: str = "playback"  # "playback" or "length_scale" or "hybrid"
    speaker: int | None = None  # For multi-speaker models like libritts
    description: str = ""

    # For hybrid mode
    length_scale: float | None = None
    playback_boost: float | None = None

    def get_voice_path(self, voices_dir: Path | None = None) -> Path:
        """Get the full path to the voice model."""
        if voices_dir is None:
            voices_dir = get_config().voices_dir

        return voices_dir / f"{self.voice}.onnx"

    def exists(self, voices_dir: Path | None = None) -> bool:
        """Check if the voice model file exists."""
        return self.get_voice_path(voices_dir).exists()

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> Persona:
        """Create a Persona from config dict."""
        return cls(
            name=name,
            voice=data.get("voice", "en_US-lessac-medium"),
            speed=data.get("speed", 2.0),
            speed_method=data.get("speed_method", "playback"),
            speaker=data.get("speaker"),
            description=data.get("description", ""),
            length_scale=data.get("length_scale"),
            playback_boost=data.get("playback_boost"),
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to config dict."""
        data: dict[str, Any] = {
            "voice": self.voice,
            "speed": self.speed,
            "speed_method": self.speed_method,
        }

        if self.speaker is not None:
            data["speaker"] = self.speaker
        if self.description:
            data["description"] = self.description
        if self.length_scale is not None:
            data["length_scale"] = self.length_scale
        if self.playback_boost is not None:
            data["playback_boost"] = self.playback_boost

        return data


def resolve_persona(name: str | None = None) -> Persona:
    """
    Resolve a persona by name.

    If name is None, returns the global default persona.
    If the named persona doesn't exist, returns a default persona with that name.
    """
    config = get_config()
    persona_name = name or config.default_persona

    if persona_name in config.personas:
        return Persona.from_dict(persona_name, config.personas[persona_name])

    # Return a default persona
    return Persona(
        name=persona_name,
        voice="en_US-lessac-medium",
        speed=config.default_speed,
    )


def list_personas() -> list[Persona]:
    """Get all registered personas."""
    config = get_config()
    return [
        Persona.from_dict(name, data) for name, data in config.personas.items()
    ]


def register_persona(persona: Persona) -> None:
    """Register or update a persona in the config."""
    config = get_config()
    config.personas[persona.name] = persona.to_dict()
    config.save()
