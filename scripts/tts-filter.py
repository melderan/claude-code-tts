#!/usr/bin/env python3
"""
tts-filter.py - Text filter for TTS speech synthesis.

Reads text from stdin, strips markdown formatting, code blocks, URLs,
and other content that sounds bad when spoken aloud. Writes clean prose
to stdout.

Replaces 12 sequential subshell pipelines in tts-lib.sh with a single
Python process.
"""

import re
import sys


def filter_text(text: str) -> str:
    """Filter text for speech synthesis."""

    # Remove <thinking> blocks
    text = re.sub(r"<thinking>[\s\S]*?</thinking>", "", text)

    # Remove fenced code blocks
    text = re.sub(r"```[\s\S]*?```", "", text)

    # Remove indented code blocks (lines starting with 4+ spaces)
    text = re.sub(r"^    .*$", "", text, flags=re.MULTILINE)

    # Remove inline code
    text = re.sub(r"`[^`]*`", "", text)

    # Remove markdown headers
    text = re.sub(r"^##* *", "", text, flags=re.MULTILINE)

    # Remove bold and italic markers
    text = re.sub(r"\*\*", "", text)
    text = re.sub(r"\*", "", text)

    # Remove URL-only bullet lines
    text = re.sub(r"^\s*[-*]\s*https?:.*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*[-*]\s*\[.*?\]\(http.*$", "", text, flags=re.MULTILINE)

    # Replace markdown links with link text
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r" \1 ", text)

    # Remove bare URLs
    text = re.sub(r"https?://\S+", "", text)

    # Normalize whitespace
    text = " ".join(text.split()).strip()

    return text


if __name__ == "__main__":
    text = sys.stdin.read()
    result = filter_text(text)
    if result:
        print(result)
