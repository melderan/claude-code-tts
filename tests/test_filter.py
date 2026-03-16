"""Tests for filter.py — text filtering for TTS."""

from claude_code_tts.filter import (
    _is_high_entropy,
    _redact_secrets,
    filter_document,
    filter_text,
    read_and_filter,
)


class TestThinkingBlocks:
    def test_removes_thinking(self):
        text = "Hello <thinking>internal reasoning</thinking> world"
        assert filter_text(text) == "Hello world"

    def test_removes_multiline_thinking(self):
        text = "Before <thinking>\nline 1\nline 2\n</thinking> After"
        assert filter_text(text) == "Before After"


class TestCodeBlocks:
    def test_removes_fenced_code(self):
        text = "Here is code:\n```python\nprint('hello')\n```\nDone."
        assert "print" not in filter_text(text)
        assert "Done." in filter_text(text)

    def test_removes_indented_code(self):
        text = "Normal text\n    indented_code()\nMore text"
        result = filter_text(text)
        assert "indented_code" not in result
        assert "Normal text" in result

    def test_preserves_inline_code_words(self):
        text = "The `foo_bar` function is important"
        result = filter_text(text)
        assert "foo_bar" in result
        assert "`" not in result


class TestMarkdown:
    def test_removes_headers(self):
        text = "## Header\nContent"
        result = filter_text(text)
        assert "##" not in result
        assert "Header" in result

    def test_removes_bold(self):
        text = "This is **bold** text"
        assert filter_text(text) == "This is bold text"

    def test_removes_italic(self):
        text = "This is *italic* text"
        assert filter_text(text) == "This is italic text"


class TestURLs:
    def test_removes_bare_urls(self):
        text = "Visit https://example.com for more"
        result = filter_text(text)
        assert "https://" not in result

    def test_extracts_link_text(self):
        text = "See [the docs](https://example.com) here"
        result = filter_text(text)
        assert "the docs" in result
        assert "https://" not in result

    def test_removes_url_bullet_lines(self):
        text = "Items:\n- https://example.com\n- Normal item"
        result = filter_text(text)
        assert "example.com" not in result
        assert "Normal item" in result


class TestBoilerplate:
    def test_removes_agent_launch(self):
        text = "Let me launch a subagent to handle this."
        assert filter_text(text) == ""

    def test_removes_task_tool(self):
        text = "I'm going to use the Task tool to track this."
        assert filter_text(text) == ""

    def test_removes_file_reading(self):
        text = "Let me read the file to understand the code."
        assert filter_text(text) == ""

    def test_removes_request_ids(self):
        text = "Error req_abc123_def456 occurred"
        result = filter_text(text)
        assert "req_" not in result


class TestHtmlTags:
    def test_removes_html_tags(self):
        text = "Click <b>here</b> for info"
        assert filter_text(text) == "Click here for info"

    def test_removes_system_reminder_tags(self):
        text = "Text <system-reminder>hidden</system-reminder> more"
        result = filter_text(text)
        assert "system-reminder" not in result


class TestHorizontalRules:
    def test_removes_dashes(self):
        text = "Section one\n---\nSection two"
        assert filter_text(text) == "Section one Section two"

    def test_removes_asterisks(self):
        text = "Above\n***\nBelow"
        assert filter_text(text) == "Above Below"


class TestImages:
    def test_removes_image_syntax_keeps_alt(self):
        text = "See ![diagram](image.png) for details"
        result = filter_text(text)
        assert "diagram" in result
        assert "image.png" not in result


class TestWhitespace:
    def test_normalizes_whitespace(self):
        text = "Hello   \n\n   world"
        assert filter_text(text) == "Hello world"

    def test_empty_input(self):
        assert filter_text("") == ""

    def test_whitespace_only(self):
        assert filter_text("   \n\n  ") == ""


# ---------------------------------------------------------------------------
# Document filter tests (filter_document / read_and_filter)
# ---------------------------------------------------------------------------


class TestDocumentFrontmatter:
    def test_strips_yaml_frontmatter(self):
        text = "---\ntitle: My Doc\ndate: 2026-01-01\n---\nActual content here."
        result = filter_document(text)
        assert "title" not in result
        assert "Actual content here" in result

    def test_frontmatter_must_be_at_start(self):
        text = "Some text\n---\ntitle: Not frontmatter\n---\nMore text"
        result = filter_document(text)
        assert "Some text" in result
        assert "More text" in result

    def test_no_frontmatter(self):
        text = "Just plain content.\nNothing special."
        result = filter_document(text)
        assert "Just plain content" in result


class TestDocumentTables:
    def test_removes_pipe_tables(self):
        text = (
            "Results:\n"
            "| Name | Value |\n"
            "|------|-------|\n"
            "| foo  | 42    |\n"
            "| bar  | 99    |\n"
            "After the table."
        )
        result = filter_document(text)
        assert "|" not in result
        assert "After the table" in result

    def test_preserves_single_pipe_in_prose(self):
        text = "Use the OR operator | to combine"
        result = filter_document(text)
        assert "OR operator" in result


class TestDocumentFilePaths:
    def test_verbalizes_full_absolute_path(self):
        text = "The config lives at ~/vault/tmp/config.json and works well"
        result = filter_document(text)
        assert "tilde vault tmp config dot json" in result
        assert "works well" in result

    def test_verbalizes_relative_paths(self):
        text = "See src/claude_code_tts/cli.py for details"
        result = filter_document(text)
        assert "src claude underscore code underscore tts cli dot py" in result

    def test_verbalizes_paths_with_special_chars(self):
        text = "All session data stays in ~/.claude/context-mode/sessions/{hash}.db"
        result = filter_document(text)
        assert "{hash}" not in result
        assert "hash variable dot db" in result

    def test_verbalizes_hyphens_in_paths(self):
        text = "See ~/vault/tmp/context-management-brief.md for details"
        result = filter_document(text)
        assert "context hyphen management hyphen brief dot md" in result

    def test_verbalizes_leading_slash(self):
        text = "Check /tmp/claude_tts_debug.log for errors"
        result = filter_document(text)
        assert "slash tmp claude underscore tts underscore debug dot log" in result

    def test_verbalizes_dot_prefixed_paths(self):
        text = "Security policies from `.claude/settings.json` are enforced"
        result = filter_document(text)
        assert "dot claude settings dot json" in result

    def test_verbalizes_hidden_dir_with_tilde(self):
        text = "Edit ~/.claude/settings.json to configure"
        result = filter_document(text)
        assert "tilde dot claude settings dot json" in result

    def test_preserves_non_path_text(self):
        text = "The function returns a string"
        assert "returns a string" in filter_document(text)


class TestDocumentLists:
    def test_strips_bullet_markers(self):
        text = "Items:\n- First thing\n- Second thing\n* Third thing"
        result = filter_document(text)
        assert "First thing" in result
        assert "Second thing" in result
        assert "Third thing" in result
        assert result.startswith("Items:")

    def test_strips_numbered_list_markers(self):
        text = "Steps:\n1. Do this\n2. Do that\n3. Done"
        result = filter_document(text)
        assert "Do this" in result
        assert "Do that" in result
        assert "Done" in result


class TestDocumentCodeBlocks:
    def test_removes_fenced_code_with_language(self):
        text = "Example:\n```bash\nexport FOO=bar\n```\nMoving on."
        result = filter_document(text)
        assert "export" not in result
        assert "Moving on" in result


class TestDocumentParagraphs:
    def test_preserves_paragraph_breaks(self):
        text = "First paragraph about topic A.\n\nSecond paragraph about topic B."
        result = filter_document(text)
        assert "\n\n" in result
        assert "First paragraph" in result
        assert "Second paragraph" in result

    def test_sections_become_paragraphs(self):
        text = "## Section One\n\nContent A.\n\n## Section Two\n\nContent B."
        result = filter_document(text)
        assert "\n\n" in result
        paragraphs = result.split("\n\n")
        assert len(paragraphs) >= 2

    def test_single_paragraph_no_trailing_newline(self):
        text = "Just one paragraph of text."
        result = filter_document(text)
        assert "\n" not in result


class TestDocumentIntegration:
    """Test filter_document against realistic document content."""

    def test_research_brief_style(self):
        doc = (
            "---\ndate: 2026-03-15\nauthor: Claude\n---\n"
            "# Context Management Research Brief\n\n"
            "## The Problem\n\n"
            "Claude Code scopes project memory by filesystem path.\n\n"
            "---\n\n"
            "## Key Discovery\n\n"
            "### `CLAUDE_CODE_AUTO_MEMORY_PATH` (env var)\n\n"
            "Overrides where auto-memory is stored.\n\n"
            "```bash\nexport CLAUDE_CODE_AUTO_MEMORY_PATH=foo\n```\n\n"
            "| File | Contents |\n"
            "|------|----------|\n"
            "| ~/vault/tmp/brief.md | This document |\n"
            "\n"
            "The env var approach is better because it can be computed dynamically."
        )
        result = filter_document(doc)
        # Frontmatter gone
        assert "author: Claude" not in result
        # Headers readable (no ## markers)
        assert "Context Management Research Brief" in result
        assert "##" not in result
        # Code blocks gone
        assert "export CLAUDE" not in result
        # Tables gone
        assert "|" not in result
        # Prose survives
        assert "scopes project memory by filesystem path" in result
        assert "dynamically" in result

    def test_empty_after_filtering(self):
        doc = "---\ntitle: empty\n---\n```\nonly code\n```"
        result = filter_document(doc)
        assert result == ""


class TestReadAndFilter:
    def test_reads_real_file(self, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("---\ntitle: Test\n---\n# Hello World\n\nThis is content.")
        result = read_and_filter(f)
        assert "Hello World" in result
        assert "This is content" in result
        assert "title: Test" not in result

    def test_file_not_found(self):
        import pytest
        with pytest.raises(FileNotFoundError):
            read_and_filter("/nonexistent/file.md")

    def test_accepts_string_path(self, tmp_path):
        f = tmp_path / "doc.md"
        f.write_text("Simple content.")
        result = read_and_filter(str(f))
        assert "Simple content" in result


class TestHighEntropy:
    """Test detection of secrets, tokens, and high-entropy strings."""

    def test_random_password(self):
        assert _is_high_entropy("NIij4wghBD4s7LuhQpu5y2hCrYUU5oLvZJYOWyfGoet7V8LrVGzhfj1TYsXjF9PZ")

    def test_hex_hash(self):
        assert _is_high_entropy("a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6")

    def test_base64_token(self):
        assert _is_high_entropy("dGhpcyBpcyBhIHRlc3QgdG9rZW4=")

    def test_short_string_not_entropy(self):
        assert not _is_high_entropy("hello")
        assert not _is_high_entropy("short123")

    def test_normal_words_not_entropy(self):
        assert not _is_high_entropy("authentication")
        assert not _is_high_entropy("configurationManager")

    def test_camel_case_not_entropy(self):
        # camelCase identifiers have recognizable word patterns
        assert not _is_high_entropy("getUserByIdFromDatabase")

    def test_snake_case_not_entropy(self):
        # snake_case with underscores — these are identifiers, not secrets
        assert not _is_high_entropy("get_user_by_id_from_db")

    def test_uuid_like(self):
        assert _is_high_entropy("550e8400e29b41d4a716446655440000")


class TestRedactSecrets:
    """Test secret redaction in speech output."""

    def test_standalone_credential(self):
        text = "The value is NIij4wghBD4s7LuhQpu5y2hCrYUU5oLvZJYOWyfGoet7V8LrVGzhfj1TYsXjF9PZ right there"
        result = _redact_secrets(text)
        assert "NIij4w" not in result
        assert "redacted" in result

    def test_jwt_token(self):
        text = "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        result = _redact_secrets(text)
        assert "eyJ" not in result
        assert "redacted token" in result

    def test_labeled_api_key(self):
        text = "api_key=xK9mP2nQ7rS4tU6vW8yZ0aB3cD5eF7gH"
        result = _redact_secrets(text)
        assert "xK9mP2" not in result
        assert "redacted" in result

    def test_labeled_password(self):
        text = "password: Xk9mP2nQ7rS4tU6vW8yZ0aB3cD5eF7gH"
        result = _redact_secrets(text)
        assert "Xk9mP2" not in result
        assert "redacted" in result

    def test_normal_text_preserved(self):
        text = "The authentication module handles user sessions correctly."
        result = _redact_secrets(text)
        assert result == text

    def test_hex_hash_redacted(self):
        text = "commit a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
        result = _redact_secrets(text)
        assert "a1b2c3" not in result

    def test_short_hex_preserved(self):
        text = "commit abc123f"
        result = _redact_secrets(text)
        assert "abc123f" in result

    def test_file_paths_not_redacted(self):
        text = "Check /usr/local/bin/some_long_path_name"
        result = _redact_secrets(text)
        # Paths have slashes — the lookbehind should protect them
        assert "some_long_path_name" in result


class TestRedactSecretsInFilters:
    """Test that redaction works through the main filter entry points."""

    def test_filter_text_redacts(self):
        text = "Set the token to NIij4wghBD4s7LuhQpu5y2hCrYUU5oLvZJYOWyfGoet7V8LrVGzhfj1TYsXjF9PZ and restart"
        result = filter_text(text)
        assert "NIij4w" not in result
        assert "redacted" in result
        assert "restart" in result

    def test_filter_document_redacts(self):
        text = "API_KEY=xK9mP2nQ7rS4tU6vW8yZ0aB3cD5eF7gH\n\nNext section."
        result = filter_document(text)
        assert "xK9mP2" not in result
        assert "Next section" in result

    def test_code_block_secrets_removed(self):
        # Secrets in code blocks get removed with the whole block
        text = "Config:\n```\npassword=SuperSecret123abc456def\n```\nDone."
        result = filter_text(text)
        assert "SuperSecret" not in result
        assert "Done." in result
