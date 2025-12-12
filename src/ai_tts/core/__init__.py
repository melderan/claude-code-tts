"""Core library components."""

from ai_tts.core.speaker import speak
from ai_tts.core.config import configure, get_config, Config
from ai_tts.core.session import get_session_id, Session
from ai_tts.core.persona import Persona, resolve_persona

__all__ = [
    "speak",
    "configure",
    "get_config",
    "Config",
    "get_session_id",
    "Session",
    "Persona",
    "resolve_persona",
]
