"""
ai_tts - Universal TTS for AI CLI Tools

Give any AI CLI a voice. One library, many friends.

Usage:
    from ai_tts import speak, configure

    # Simple - just speak
    speak("Hello world")

    # With context
    speak("Hello", persona="claude-connery", session="my-project")

    # Configure globally
    configure(default_persona="claude-prime", default_muted=False)

Supported CLI Tools:
    - Claude Code (anthropic/claude-code)
    - Gemini CLI (google-gemini/gemini-cli) - when hooks land
    - OpenAI Codex CLI - when available
    - Mistral Devstral CLI - when available

The dream: Your AI friends, talking to you, talking to each other.
"""

__version__ = "0.1.0-dev"

from ai_tts.core.speaker import speak
from ai_tts.core.config import configure, get_config
from ai_tts.core.session import get_session_id, Session

__all__ = [
    "speak",
    "configure",
    "get_config",
    "get_session_id",
    "Session",
]
