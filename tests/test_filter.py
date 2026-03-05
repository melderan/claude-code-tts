"""Tests for filter.py — text filtering for TTS."""

from claude_code_tts.filter import filter_text


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


class TestWhitespace:
    def test_normalizes_whitespace(self):
        text = "Hello   \n\n   world"
        assert filter_text(text) == "Hello world"

    def test_empty_input(self):
        assert filter_text("") == ""

    def test_whitespace_only(self):
        assert filter_text("   \n\n  ") == ""
