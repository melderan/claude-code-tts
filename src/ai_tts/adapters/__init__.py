"""
CLI tool adapters for ai_tts.

Each adapter knows how to:
- Detect if its CLI tool is running
- Find the session ID for that tool
- Extract text from the tool's response format
- Hook into the tool's event system

Supported adapters:
- claude: Claude Code (anthropic/claude-code)
- gemini: Gemini CLI (google-gemini/gemini-cli) [planned]
- codex: OpenAI Codex CLI [planned]
- devstral: Mistral Devstral CLI [planned]
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ai_tts.core.session import Session

# Registry of adapters
_adapters: dict[str, type["BaseAdapter"]] = {}


class BaseAdapter(ABC):
    """
    Base class for CLI tool adapters.

    Subclass this to add support for a new CLI tool.
    """

    # Override in subclass
    name: str = "base"
    display_name: str = "Base Adapter"

    @abstractmethod
    def detect_session(self) -> str | None:
        """
        Detect the current session ID for this CLI tool.

        Returns None if unable to detect (tool not running, etc).
        """
        ...

    @abstractmethod
    def extract_text(self, event_data: dict) -> str | None:
        """
        Extract speakable text from an event payload.

        Args:
            event_data: The event data from the CLI tool's hook system.

        Returns:
            The text to speak, or None if this event shouldn't be spoken.
        """
        ...

    def is_available(self) -> bool:
        """Check if this CLI tool is installed/available."""
        return True

    @classmethod
    def register(cls) -> None:
        """Register this adapter in the global registry."""
        _adapters[cls.name] = cls


def get_adapter(name: str) -> BaseAdapter | None:
    """Get an adapter instance by name."""
    if name in _adapters:
        return _adapters[name]()
    return None


def list_adapters() -> list[str]:
    """List all registered adapter names."""
    return list(_adapters.keys())


# Import adapters to trigger registration
from ai_tts.adapters.claude import ClaudeCodeAdapter

# Future imports:
# from ai_tts.adapters.gemini import GeminiCLIAdapter
# from ai_tts.adapters.codex import CodexAdapter
# from ai_tts.adapters.devstral import DevstralAdapter
