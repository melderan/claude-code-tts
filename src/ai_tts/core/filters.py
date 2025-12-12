"""
Text filtering for speech.

Cleans up text before sending to TTS:
- Removes code blocks
- Strips markdown formatting
- Handles URLs and links
- Removes emojis and special characters that sound weird
"""

from __future__ import annotations

import re


def filter_text_for_speech(text: str) -> str:
    """
    Filter text for TTS consumption.

    Removes code blocks, markdown formatting, and other elements
    that don't translate well to speech.
    """
    # Remove code blocks (```...```)
    text = re.sub(r"```[\s\S]*?```", "", text)

    # Remove inline code (`...`)
    text = re.sub(r"`[^`]+`", "", text)

    # Remove markdown links but keep the text: [text](url) -> text
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)

    # Remove bare URLs
    text = re.sub(r"https?://\S+", "", text)

    # Remove bullet points that are now empty (were just URLs)
    text = re.sub(r"^\s*[-*]\s*$", "", text, flags=re.MULTILINE)

    # Remove markdown headers (# ## ### etc)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)

    # Remove bold/italic markers
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)  # **bold**
    text = re.sub(r"\*([^*]+)\*", r"\1", text)  # *italic*
    text = re.sub(r"__([^_]+)__", r"\1", text)  # __bold__
    text = re.sub(r"_([^_]+)_", r"\1", text)  # _italic_

    # Remove horizontal rules
    text = re.sub(r"^[-*_]{3,}\s*$", "", text, flags=re.MULTILINE)

    # Remove HTML tags
    text = re.sub(r"<[^>]+>", "", text)

    # Remove common emojis that TTS handles poorly
    # (Keep this minimal - some TTS engines handle emojis fine)
    emoji_pattern = re.compile(
        "["
        "\U0001f600-\U0001f64f"  # emoticons
        "\U0001f300-\U0001f5ff"  # symbols & pictographs
        "\U0001f680-\U0001f6ff"  # transport & map
        "\U0001f1e0-\U0001f1ff"  # flags
        "\U00002702-\U000027b0"  # dingbats
        "\U0001f900-\U0001f9ff"  # supplemental symbols
        "]+",
        flags=re.UNICODE,
    )
    text = emoji_pattern.sub("", text)

    # Collapse multiple newlines
    text = re.sub(r"\n{3,}", "\n\n", text)

    # Collapse multiple spaces
    text = re.sub(r" {2,}", " ", text)

    # Strip leading/trailing whitespace
    text = text.strip()

    return text


def estimate_speech_duration(text: str, speed: float = 2.0) -> float:
    """
    Estimate how long speech will take in seconds.

    Based on average speaking rate of ~150 words per minute at 1x speed.
    """
    words = len(text.split())
    base_duration = (words / 150) * 60  # seconds at 1x
    return base_duration / speed
