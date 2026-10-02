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


# A removed span leaves this marker until the sentence around it is mended.
_GAP = ""

# The rule is length, not kind: inline code or a bare path longer than this many
# characters is never spoken (a command line, a long path, an expression read aloud is
# noise); anything this short or shorter (a flag, a filename, `src/x.py`) is speech and
# stays. Removal leaves nothing, not a placeholder; see _mend_gaps.
MAX_SPOKEN_CODE_SPAN = 20

# Words that only introduce a thing: when the thing is removed they go with it.
_LEAD_INS = r"(?:with|via|using|from|in|at|on|by|of|for|into|to|see|as|than|e\.g\.|i\.e\.|like)"
# A stop after these is not a sentence end.
_NOT_ABBREV = r"(?<!e\.g\.)(?<!i\.e\.)(?<!etc\.)"
_END = r"(?=[ \t]*(?:[.,;:!?)\]\"']|$))"


def _mend_gaps(text: str) -> str:
    """Close the holes _GAP marks so the sentence around a removed span still reads."""
    g = _GAP
    ml = re.MULTILINE
    # A bullet or numbered item that held nothing else.
    text = re.sub(rf"^[ \t]*(?:[-*+]|\d+\.)?[ \t]*{g}[ \t]*$", "", text, flags=ml)
    # Neighbouring holes ("`a`, `b` and `c`", "x or <hole> or <hole>") become one hole.
    text = re.sub(rf"{g}(?:[ \t]*[,;]?[ \t]*(?:(?:and|or)[ \t]+)?{g})+", g, text)
    text = re.sub(rf"\b(and|or)[ \t]+{g}[ \t]*,?[ \t]*(?:and|or)\b", r"\1", text)
    # "x and <hole>." -> "x."
    text = re.sub(rf"[ \t]*,?[ \t]*\b(?:and|or)[ \t]+{g}{_END}", g, text)
    # "starts with <hole>." and "(see <hole>)": the lead-in word goes too.
    text = re.sub(rf"\b{_LEAD_INS}[ \t]+{g}{_END}", g, text, flags=re.IGNORECASE)
    # Brackets and quotes around nothing: (`long`) -> nothing
    text = re.sub(rf"[(\[{{][ \t]*{g}[ \t]*[)\]}}]", g, text)
    text = re.sub(rf"([\"']){g}\1", g, text)
    # A sentence that is only a lead word once the hole is gone said nothing ("Run.",
    # "visit"). A hole at the start leaves the rest of the sentence standing.
    def _thin(line: str) -> str:
        if g not in line:
            return line
        parts = re.split(rf"(?<=[.!?]){_NOT_ABBREV}[ \t]+", line)

        def _says_something(part: str) -> bool:
            if g not in part:
                return True
            words = len(re.findall(rf"[^\s{g}]*\w[^\s{g}]*", part))
            return words > 1 or (words == 1 and re.match(rf"[ \t]*(?:[-*+][ \t]+)?{g}", part) is not None)

        return " ".join(p for p in parts if _says_something(p))

    text = "\n".join(_thin(line) for line in text.split("\n"))
    # Hole opening a list: "Files: <hole>, and x.py" -> "Files: x.py"
    text = re.sub(rf"([:;])[ \t]*{g}[ \t]*,?[ \t]*(?:and|or)[ \t]+", r"\1 ", text)
    # "a, <hole>, and b" -> "a and b"
    text = re.sub(rf",[ \t]*{g}[ \t]*,?[ \t]*(?=(?:and|or)\b)", " ", text)
    # "visit <hole> or x" -> "visit x"
    text = re.sub(rf"{g}[ \t]*,?[ \t]*(?:and|or)[ \t]+(?!then\b)", g + " ", text)
    # A hole at the start of a sentence or line takes its own comma or stop with it,
    # and a plain lowercase word after it is capitalised: "`x` returns None." ->
    # "Returns None." Words that are not plain prose (iPhone, src/x.py:42, npm) stay.
    def _start(m: re.Match) -> str:
        word = m.group(2) or ""
        if word and re.search(r"[aeiouy]", word):
            word = word[0].upper() + word[1:]
        return m.group(1) + word

    text = re.sub(
        rf"(^[ \t]*(?:(?:[-*+]|\d+\.)[ \t]+)?|(?<!e\.g)(?<!i\.e)(?<!etc)[.!?][ \t]+){g}[ \t]*[,;:.!?]?[ \t]*"
        rf"([a-z]+(?![\w./:@~{{}}-]))?",
        _start, text, flags=ml,
    )
    # ", <hole>," -> ","   and   ": <hole>," -> ":"
    text = re.sub(rf"([,;:])[ \t]*{g}[ \t]*[,;]", r"\1", text)
    # "word <hole>." -> "word."  and  "(see x <hole>)" -> "(see x)"
    text = re.sub(rf"[ \t]*{g}[ \t]*([,.;:!?])", r"\1", text)
    text = re.sub(rf"[ \t]*{g}[ \t]*(?=[)\]])", "", text)
    # Anything left: a hole inside a sentence is one space.
    text = re.sub(rf"[ \t]*{g}[ \t]*", " ", text)
    return text


def _drop_long_code_spans(text: str) -> str:
    """Remove single-line inline code spans longer than MAX_SPOKEN_CODE_SPAN characters."""
    text = text.replace(_GAP, "")

    def _span(m: re.Match) -> str:
        return _GAP if len(m.group(1)) > MAX_SPOKEN_CODE_SPAN else m.group(0)

    # One line only: a stray backtick must never pair with one in a later paragraph and
    # swallow the prose between them.
    return _mend_gaps(re.sub(r"`([^`\n]*)`", _span, text))


_PATH_EXT = re.compile(
    r"\.(?:md|py|txt|json|yaml|yml|toml|sh|ts|js|go|rs|rb|db|csv|xml|html|log|cfg|ini|lock|conf|env)$"
)
_PATH_CHARS = re.compile(r"[\w.{}@+~/-]+")
_LINE_SUFFIX = re.compile(r"(?::\d+){1,2}$")  # file.py:42 and file.py:42:7
_PATH_TOKEN_RE = re.compile(
    r"(?<!\S)([(\[{\"']*)([^\s()\[\]\"',;:!?]*?/[^\s]*?)([)\]\"',;:!?.]*)(?=\s|$)"
)


def _looks_like_path(tok: str) -> bool:
    """A path the speaker would otherwise spell out: ~/x, ./x, /a/b, src/x.py, a/b_c/d, dir/sub/.

    One word with one slash (and/or, TCP/IP), dates and slash commands (/tts-mute) are not
    paths. A relative path with two slashes needs a dot, underscore or hyphen, or a
    trailing slash, so "Linux/macOS/Windows" stays a list of words. A :line or :line:col
    suffix belongs to the path.
    """
    tok = _LINE_SUFFIX.sub("", tok)
    if not _PATH_CHARS.fullmatch(tok):
        return False
    if tok.startswith(("~/", "./", "../")):
        return len(tok) > 2
    comps = [c for c in tok.split("/") if c]
    if tok.startswith("/"):
        return len(comps) >= 2 or bool(comps and _PATH_EXT.search(comps[0]))
    if not re.match(r"[A-Za-z_.@]", tok):
        return False
    slashes = tok.count("/")
    if _PATH_EXT.search(tok):
        return True
    return slashes >= 2 and bool(re.search(r"[._{@~-]", tok) or tok.endswith("/"))


def _drop_bare_paths(text: str) -> str:
    """Remove long file paths from running prose (the length rule of MAX_SPOKEN_CODE_SPAN)."""
    text = text.replace(_GAP, "")

    def _path(m: re.Match) -> str:
        pre, tok, post = m.groups()
        if len(tok) > MAX_SPOKEN_CODE_SPAN and _looks_like_path(tok):
            return f"{pre}{_GAP}{post}"
        return m.group(0)

    return _mend_gaps(_PATH_TOKEN_RE.sub(_path, text))


def _remove_fences(text: str) -> str:
    """Drop fenced code blocks line by line, ``` or ~~~, an unclosed one to the end.

    A fence opens on a line that starts with three or more backticks or tildes and
    closes on a line of the same character at least as long. An unclosed fence is code
    to the end of the reply (a reply cut short mid-block must not speak its tail).
    """
    out: list[str] = []
    fence: tuple[str, int] | None = None
    for line in text.split("\n"):
        m = re.match(r"^[ \t]*(`{3,}|~{3,})", line)
        if fence is None:
            if m and not (m.group(1)[0] == "`" and "`" in line[m.end():]):
                fence = (m.group(1)[0], len(m.group(1)))
                continue
            out.append(line)
        elif m and m.group(1)[0] == fence[0] and len(m.group(1)) >= fence[1] and not line[m.end():].strip():
            fence = None
    return "\n".join(out)


_TABLE_SEP_RE = re.compile(r"^[ \t]*\|?[ \t]*:?-+:?[ \t]*(?:\|[ \t]*:?-+:?[ \t]*)*\|?[ \t]*$")


def _table_cells(line: str) -> int:
    """Cell count of a pipe row: leading and trailing pipes are not columns.

    Inline code is blanked first, so a pipe inside backticks (`x|y`) is not a column.
    """
    line = re.sub(r"`[^`]*`", "x", line.strip())
    line = line[1:] if line.startswith("|") else line
    line = line[:-1] if line.endswith("|") and not line.endswith("\\|") else line
    return len(re.split(r"(?<!\\)\|", line))


def _is_table_header(head: str, sep: str) -> bool:
    """A header line and a separator line (cells of dashes) with the same number of cells.

    This is the CommonMark rule, outer pipes optional on both, so "Total: 5 | 6" over
    "---|---" is a table (of 0 rows) and is spoken as one; a prose line is not guessed at.
    A one-cell table needs outer pipes, which the pipe on both lines already shows.
    """
    if "|" not in head or "|" not in sep or not _TABLE_SEP_RE.match(sep):
        return False
    return _table_cells(head) == _table_cells(sep)


def _is_table_row(line: str, head: str) -> bool:
    """A pipe-led row has the header's width, one cell more at most (extra cells are
    ignored, fewer are allowed); a line that does not start with a pipe is a row only
    under a header that does not, and only at the header's width."""
    if "|" not in line:
        return False
    width = _table_cells(head)
    cells = _table_cells(line)
    if line.strip().startswith("|"):
        return cells <= width + 1
    return not head.strip().startswith("|") and cells == width


def _summarize_tables(text: str) -> str:
    """Replace each Markdown table with "table of N rows." (N = body rows).

    A table is a header line, a separator line of dashes and pipes with the same number of
    cells, then the rows up to the first line that is not one. A pipe in a sentence, or a
    rule of dashes without pipes, starts nothing; a sentence under a table that happens to
    hold a pipe is not a row. The line above a table (a caption or a heading) is left
    alone, so it is kept.
    """
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        head = lines[i]
        if i + 1 < len(lines) and _is_table_header(head, lines[i + 1]):
            j = i + 2
            while j < len(lines) and _is_table_row(lines[j], head):
                j += 1
            rows = j - (i + 2)
            out.append(f"table of {rows} {'row' if rows == 1 else 'rows'}.")
            i = j
        else:
            out.append(head)
            i += 1
    return "\n".join(out)


def _filter_markdown(text: str, *, drop_long_code: bool = False) -> str:
    """Shared markdown cleanup used by both filter modes.

    drop_long_code removes inline code spans longer than MAX_SPOKEN_CODE_SPAN characters; the live
    filter turns it on, the document filter keeps them (it spells paths out instead).
    """

    # Code and tables first, whole, before anything can cut across their lines: fenced
    # blocks (also when unclosed), then tables (as one phrase), then long inline spans.
    # A fence used to be removed after the HTML-tag pass, which could eat a fence line
    # and let the code under it be spoken.
    text = _remove_fences(text)
    text = _summarize_tables(text)
    # Links and images before long spans: a link whose text is code would otherwise lose
    # its brackets to the gap mending and leave "(" behind.
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)  # images: keep alt text
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r" \1 ", text)  # links: keep link text
    if drop_long_code:
        text = _drop_long_code_spans(text)

    # Link text was taken above; URLs go first, whole. Secret redaction inserts spaces inside a
    # long query string; after that the URL regex sees no URL and the tail
    # (redirect_uri=..., resource=..., response_type=code) is spoken. Two OAuth
    # authorize links reached the speaker that way on 2026-09-23.
    text = re.sub(r"^\s*[-*]\s*https?:.*$", "", text, flags=re.MULTILINE)  # URL-only bullets
    text = re.sub(r"^\s*[-*]\s*\[.*?\]\(http.*$", "", text, flags=re.MULTILINE)
    # A URL alone in parentheses takes the parentheses (and a lead-in word) with it.
    text = re.sub(
        rf"[ \t]*[(\[][ \t]*(?:{_LEAD_INS}[ \t:]+)?(?:https?://|www\.)[^\s)\]]*[ \t]*[)\]]",
        "", text, flags=re.IGNORECASE,
    )
    text = re.sub(r"(?:https?://|www\.)\S+", "", text, flags=re.IGNORECASE)
    # A percent-encoded URL only occurs as a query value; take its key with it.
    text = re.sub(r"\S*https?%3A%2F%2F\S+", "", text, flags=re.IGNORECASE)

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

    # Remove markdown headers (keep the text)
    text = re.sub(r"^##* *", "", text, flags=re.MULTILINE)

    # Remove bold and italic markers
    text = re.sub(r"\*\*", "", text)
    text = re.sub(r"\*", "", text)

    # Remove horizontal rules
    text = re.sub(r"^[-*_]{3,}\s*$", "", text, flags=re.MULTILINE)

    # Remove API error request IDs (e.g., "req_abc123...")
    text = re.sub(r"\breq_[a-zA-Z0-9_-]+\b", "", text)

    # Strip emoji and pictographic symbols — TTS engines read their Unicode names
    # aloud (e.g., "magnifying glass", "angular ruler") which is always noise.
    text = re.sub(
        r"[\U0001F300-\U0001FAFF\U00002600-\U000027BF︀-️‍⃣]+",
        " ", text,
    )

    # Strip box-drawing and block-element runs (Algorithm phase headers: ━━━ OBSERVE ━━━)
    text = re.sub(r"[─-▟]+", "", text)

    # Clean up punctuation clusters that cause Piper to produce noise
    # artifacts. E.g., ".)" or "?)" or "!]" -- Piper generates end-of-
    # sentence prosody on the first mark, then chokes on the second.
    text = re.sub(r"([.!?])[)\]}>\"']+", r"\1", text)

    # Query-string debris that survived everything above: a token holding
    # key=value pairs joined by & or percent-encoding is never speech.
    text = re.sub(r"\S*(?:&[\w-]+=|%[0-9A-Fa-f]{2})\S*", "", text)

    return text


def filter_text(text: str) -> str:
    """Filter Claude's live response text for speech synthesis."""

    text = _filter_markdown(text, drop_long_code=True)
    text = _drop_bare_paths(text)

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
    Handles YAML frontmatter and other document-specific
    formatting that filter_text() doesn't need to worry about.

    Preserves paragraph boundaries as double-newlines so the spoken output
    has natural pauses between sections.
    """

    # Strip YAML frontmatter (--- block at start of file)
    text = re.sub(r"\A---\n[\s\S]*?\n---\n?", "", text)

    # Apply shared markdown cleanup (tables become one phrase there)
    text = _filter_markdown(text)

    # Table rows the shared pass did not recognise as a table (no separator line)
    text = re.sub(r"^\|[\s:-]+\|\s*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\|.*\|.*\|.*$", "", text, flags=re.MULTILINE)

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
