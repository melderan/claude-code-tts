"""Text filter for TTS speech synthesis.

Strips markdown formatting, code blocks, URLs, and other content that
sounds bad when spoken aloud. Replaces 12 sequential subshell pipelines
in the old tts-lib.sh with a single Python function.

Two entry points:
  filter_text()     - for Claude's live responses (strips agent boilerplate)
  filter_document() - for reading files aloud (strips frontmatter, tables, etc.)

Both share a common base via _filter_markdown().
"""

import re
from pathlib import Path


def _is_high_entropy(s: str) -> bool:
    """Check if a string looks like a secret/credential (high entropy random chars).

    Catches: API keys, passwords, tokens, base64 blobs, hex strings, JWTs.
    """
    # Too short to be a secret
    if len(s) < 16:
        return False

    # Count character classes
    has_upper = bool(re.search(r"[A-Z]", s))
    has_lower = bool(re.search(r"[a-z]", s))
    has_digit = bool(re.search(r"[0-9]", s))

    # Pure hex (32+ chars) — likely a hash or key
    if re.fullmatch(r"[0-9a-fA-F]{32,}", s):
        return True

    # Base64-like (24+ chars, alphanumeric + /+=)
    if len(s) >= 24 and re.fullmatch(r"[A-Za-z0-9+/=_-]{24,}", s):
        # Must have mixed case + digits to avoid matching normal words
        if has_upper and has_lower and has_digit:
            return True

    # Long alphanumeric with no spaces or real word patterns (20+ chars)
    if len(s) >= 20 and re.fullmatch(r"[A-Za-z0-9_-]{20,}", s):
        if has_upper and has_lower and has_digit:
            # Check it's not a camelCase identifier — those have word boundaries
            # Secrets don't have recognizable word patterns
            words = re.findall(r"[A-Z]?[a-z]+", s)
            if not words or max(len(w) for w in words) <= 3:
                return True

    return False


def _redact_secrets(text: str) -> str:
    """Replace high-entropy strings (secrets, tokens, keys) with a spoken marker."""

    # JWT tokens (three base64 segments separated by dots)
    text = re.sub(
        r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b",
        "redacted token",
        text,
    )

    # Labeled secrets: key=VALUE, key: VALUE, key "VALUE" patterns
    def _redact_labeled(m: re.Match) -> str:
        label = m.group(1)
        sep = m.group(2)
        value = m.group(3)
        if _is_high_entropy(value):
            return f"{label}{sep}redacted"
        return m.group(0)

    text = re.sub(
        r"(\b\w*(?:key|token|secret|password|passwd|credential|auth)[_\w]*)"
        r"(\s*[=:]\s*[\"']?)"
        r"([A-Za-z0-9+/=_-]{16,})",
        _redact_labeled, text, flags=re.IGNORECASE,
    )

    # Standalone high-entropy strings (not part of a path or URL)
    def _redact_standalone(m: re.Match) -> str:
        s = m.group(0)
        if _is_high_entropy(s):
            return "redacted credential"
        return s

    text = re.sub(r"(?<![/\\.])(?<!\w)[A-Za-z0-9+/=_-]{24,}(?!\w)(?![/\\.])", _redact_standalone, text)

    return text


def _filter_markdown(text: str) -> str:
    """Shared markdown cleanup used by both filter modes."""

    # Redact secrets before any other processing
    text = _redact_secrets(text)

    # Remove <thinking> blocks
    text = re.sub(r"<thinking>[\s\S]*?</thinking>", "", text)

    # Remove HTML tags (but keep their text content)
    text = re.sub(r"<[^>]+>", "", text)

    # Remove fenced code blocks
    text = re.sub(r"```[\s\S]*?```", "", text)

    # Remove indented code blocks (lines starting with 4+ spaces)
    text = re.sub(r"^    .*$", "", text, flags=re.MULTILINE)

    # Strip inline code backticks but keep the word (it's often part of speech)
    text = re.sub(r"`([^`]*)`", r"\1", text)

    # Remove markdown image syntax ![alt](url)
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)

    # Replace markdown links with link text
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r" \1 ", text)

    # Remove bare URLs
    text = re.sub(r"https?://\S+", "", text)

    # Remove URL-only bullet lines
    text = re.sub(r"^\s*[-*]\s*https?:.*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*[-*]\s*\[.*?\]\(http.*$", "", text, flags=re.MULTILINE)

    # Remove markdown headers (keep the text)
    text = re.sub(r"^##* *", "", text, flags=re.MULTILINE)

    # Remove bold and italic markers
    text = re.sub(r"\*\*", "", text)
    text = re.sub(r"\*", "", text)

    # Remove horizontal rules
    text = re.sub(r"^[-*_]{3,}\s*$", "", text, flags=re.MULTILINE)

    # Remove API error request IDs (e.g., "req_abc123...")
    text = re.sub(r"\breq_[a-zA-Z0-9_-]+\b", "", text)

    return text


def filter_text(text: str) -> str:
    """Filter Claude's live response text for speech synthesis."""

    text = _filter_markdown(text)

    # Strip agent launch boilerplate
    text = re.sub(
        r"(?i)^.*(?:let me (?:launch|use|start) (?:a |an |the )?(?:sub)?agent|"
        r"I'll (?:launch|use|start) (?:a |an |the )?(?:sub)?agent).*$",
        "", text, flags=re.MULTILINE,
    )
    text = re.sub(
        r"(?i)^.*(?:I'm going to use the Task tool|Using the .* agent).*$",
        "", text, flags=re.MULTILINE,
    )
    text = re.sub(
        r"(?i)^.*(?:Let me explore the codebase|I'll explore the codebase).*$",
        "", text, flags=re.MULTILINE,
    )

    # Strip tool invocation narration
    text = re.sub(
        r"(?i)^.*(?:Let me read|I'll read|Let me check|I'll check) "
        r"(?:the |that |this )?(?:file|code|output).*$",
        "", text, flags=re.MULTILINE,
    )

    # Normalize whitespace
    text = " ".join(text.split()).strip()

    return text


def filter_document(text: str) -> str:
    """Filter a document file for speech synthesis.

    Designed for reading files aloud without loading them into context.
    Handles YAML frontmatter, markdown tables, and other document-specific
    formatting that filter_text() doesn't need to worry about.

    Preserves paragraph boundaries as double-newlines so the spoken output
    has natural pauses between sections.
    """

    # Strip YAML frontmatter (--- block at start of file)
    text = re.sub(r"\A---\n[\s\S]*?\n---\n?", "", text)

    # Remove markdown tables (lines with pipes as column separators)
    # First remove separator rows: | --- | --- |
    text = re.sub(r"^\|[\s:-]+\|\s*$", "", text, flags=re.MULTILINE)
    # Then remove data rows that are clearly tabular (2+ pipe separators)
    text = re.sub(r"^\|.*\|.*\|.*$", "", text, flags=re.MULTILINE)

    # Apply shared markdown cleanup
    text = _filter_markdown(text)

    # Convert file paths to fully spoken words
    # ~/vault/tmp/config.json -> "tilde vault tmp config dot json"
    # src/claude_code_tts/cli.py -> "src claude underscore code underscore tts cli dot py"
    # ~/.claude/settings.json -> "tilde dot claude settings dot json"
    def _path_to_speech(m: re.Match) -> str:
        path = m.group(0)

        # Handle leading special chars
        spoken_parts: list[str] = []
        if path.startswith("~/"):
            spoken_parts.append("tilde")
            path = path[2:]
        elif path.startswith("~"):
            spoken_parts.append("tilde")
            path = path[1:]
        elif path.startswith("/"):
            spoken_parts.append("slash")
            path = path[1:]

        # Split on / and convert each component
        components = [c for c in path.split("/") if c]
        for i, comp in enumerate(components):
            is_last = i == len(components) - 1

            # Handle hidden dir/file prefix (leading dot)
            if comp.startswith("."):
                spoken_parts.append("dot")
                comp = comp[1:]

            if is_last and "." in comp:
                # Last component with extension: "cli.py" -> "cli dot py"
                name, ext = comp.rsplit(".", 1)
                if name:
                    spoken_parts.append(_verbalize_name(name))
                spoken_parts.append("dot")
                spoken_parts.append(ext)
            elif comp:
                spoken_parts.append(_verbalize_name(comp))

        return " ".join(spoken_parts)

    def _verbalize_name(name: str) -> str:
        """Convert a path component name to spoken words.

        Underscores become 'underscore', hyphens become 'hyphen',
        curly braces become 'variable'.
        """
        # {hash} -> "hash variable"
        name = re.sub(r"\{(\w+)\}", r"\1 variable", name)
        # foo_bar -> "foo underscore bar"
        name = re.sub(r"_", " underscore ", name)
        # foo-bar -> "foo hyphen bar"
        name = re.sub(r"-", " hyphen ", name)
        # Collapse multiple spaces
        return " ".join(name.split())

    # Match file paths: ~/foo/bar.py, /tmp/foo.log, ./foo.py, .claude/foo.json, src/foo.py
    text = re.sub(
        r"(?:~/|/|\./)[\w{}./-]*\."
        r"(?:md|py|txt|json|yaml|yml|toml|sh|ts|js|go|rs|rb|db|csv|xml|html|log)\b"
        r"|"
        r"(?:\.\w+|\b\w+)/[\w{}./-]*\."
        r"(?:md|py|txt|json|yaml|yml|toml|sh|ts|js|go|rs|rb|db|csv|xml|html|log)\b",
        _path_to_speech, text,
    )

    # Remove bullet markers but keep the text
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.MULTILINE)

    # Convert numbered list markers to natural flow
    text = re.sub(r"^\s*\d+\.\s+", "", text, flags=re.MULTILINE)

    # Collapse runs of 3+ blank lines into paragraph breaks (double newline)
    text = re.sub(r"\n{3,}", "\n\n", text)

    # Normalize whitespace WITHIN paragraphs but preserve paragraph breaks
    paragraphs = re.split(r"\n\n+", text)
    cleaned = []
    for para in paragraphs:
        normalized = " ".join(para.split()).strip()
        if normalized:
            cleaned.append(normalized)

    return "\n\n".join(cleaned)


def read_and_filter(path: str | Path) -> str:
    """Read a file from disk and filter it for speech.

    This is the read-side entry point for --from-file. Zero tokens,
    zero context window -- just disk to voice.
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"File not found: {path}")

    content = path.read_text(encoding="utf-8", errors="replace")
    return filter_document(content)
