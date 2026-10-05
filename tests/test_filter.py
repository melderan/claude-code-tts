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


class TestFencedBlocksAreSkipped:
    """A fenced block is never read, line by line or in part."""

    def test_multi_line_block_is_skipped_entirely(self):
        text = "Before.\n```bash\nrm -rf build\ncd src && make\necho done\n```\nAfter."
        assert filter_text(text) == "Before. After."

    def test_tilde_fence_is_skipped(self):
        assert filter_text("Before.\n~~~\nsecret_call()\n~~~\nAfter.") == "Before. After."

    def test_unclosed_fence_hides_the_rest(self):
        # A reply cut short mid-block must not speak the tail of the code.
        assert filter_text("Look.\n```py\nprint(1)\nprint(2)") == "Look."

    def test_angle_brackets_in_prose_cannot_eat_a_fence_line(self):
        # The tag pass used to run before the fence pass: "< 3" to the ">" inside the code
        # spanned the opening fence, and the code after it was spoken.
        text = "When x < 3:\n```\nif a > b_marker:\n    pass\n```\nDone."
        result = filter_text(text)
        assert "b_marker" not in result and "```" not in result
        assert result.endswith("Done.")

    def test_longer_fence_closes_only_on_an_equal_or_longer_fence(self):
        text = "A.\n````\n```\nnested()\n```\n````\nB."
        assert filter_text(text) == "A. B."


class TestLongInlineCode:
    """Inline spans over 20 characters are not spoken; short ones are."""

    SPAN20 = "a" * 20
    SPAN21 = "a" * 21

    def test_span_of_exactly_20_chars_is_spoken(self):
        assert filter_text(f"Use `{self.SPAN20}` here.") == f"Use {self.SPAN20} here."

    def test_span_of_21_chars_is_removed(self):
        assert filter_text(f"Use `{self.SPAN21}` here.") == "Use here."

    def test_long_span_at_the_start_of_a_sentence(self):
        text = "`uv run pytest tests/test_filter.py -q` passes cleanly."
        assert filter_text(text) == "Passes cleanly."

    def test_long_span_in_the_middle_of_a_sentence(self):
        text = "The `uv run pytest tests/test_filter.py -q` command passes."
        assert filter_text(text) == "The command passes."

    def test_long_span_at_the_end_of_a_sentence_takes_its_lead_in_word(self):
        text = "The config is stored in `~/.config/claude-tts/settings.json`. Then it waits."
        assert filter_text(text) == "The config is stored. Then it waits."

    def test_removal_leaves_nothing_not_a_placeholder(self):
        result = filter_text("The `docker compose up -d --build` step is slow.")
        assert result == "The step is slow."

    def test_two_long_spans_in_a_list_leave_no_stray_commas(self):
        text = "Files: `src/claude_code_tts/filter.py`, `src/claude_code_tts/cli.py`, and `x.py`."
        assert filter_text(text) == "Files: x.py."

    def test_long_span_in_parentheses_takes_the_parentheses(self):
        text = "The helper (see `src/claude_code_tts/filter.py`) is fine."
        assert filter_text(text) == "The helper is fine."

    def test_bullet_holding_only_a_long_span_vanishes(self):
        text = "Changed:\n- `src/claude_code_tts/filter.py`\n- the tests"
        assert filter_text(text) == "Changed: - the tests"

    def test_sentence_left_with_one_lead_word_is_dropped(self):
        assert filter_text("Run `docker compose up -d --build --force`. Then wait.") == "Then wait."
        assert filter_text("visit example.com/docs/page_that_is_long or github.com/foo/bar/baz_long_enough") == ""

    def test_first_word_after_a_sentence_start_removal_is_capitalised(self):
        text = "`uv run pytest tests/test_filter.py -q` returns None. `uv run pytest tests/test_filter.py -q` then stop."
        assert filter_text(text) == "Returns None. Then stop."

    def test_capitalisation_only_touches_plain_lowercase_words(self):
        span = "`uv run pytest tests/test_filter.py -q`"
        assert filter_text(f"{span} iPhone works. {span} npm works. {span} src/x.py:42 works.") == (
            "iPhone works. npm works. src/x.py:42 works."
        )

    def test_abbreviation_stops_are_not_sentence_ends(self):
        span = "`uv run pytest tests/test_filter.py -q`"
        assert filter_text(f"Use tools, e.g. {span} returns None.") == "Use tools, e.g. returns None."
        assert filter_text(f"Use tools, i.e. {span} returns None.") == "Use tools, i.e. returns None."
        assert filter_text(f"Some tools, etc. {span} returns None.") == "Some tools, etc. returns None."

    # Reviewer findings, inputs verbatim.

    def test_link_whose_text_is_long_code_keeps_its_sentence(self):
        text = "See [`_drop_long_code_spans`](https://github.com/x/y) for details."
        assert filter_text(text) == "See for details."

    def test_empty_parentheses_after_a_lead_in_word_are_removed_whole(self):
        assert filter_text("The helper (in `docker compose up -d --build --force`) is fine.") == "The helper is fine."
        assert filter_text("It works (see docs/some/very/long/path_to_a/file.md) today.") == "It works today."

    def test_repeated_joiners_collapse_after_adjacent_holes(self):
        text = "Use /tts-mute or /usr/local/share/foo/bar or /usr/local/share/foo/baz or ./run.sh."
        result = filter_text(text)
        assert "or or" not in result
        assert result == "Use /tts-mute or ./run.sh."
        assert filter_text("Use /tts-mute or /etc/hosts or /tmp or ./run.sh or ../x or ~/.") == (
            "Use /tts-mute or /etc/hosts or /tmp or ./run.sh or ../x or ~/."
        )

    def test_short_spans_still_speak_their_word(self):
        result = filter_text("Pass `--force` to `uv` for `foo_bar`.")
        assert result == "Pass --force to uv for foo_bar."

    def test_stray_backtick_never_swallows_prose_in_a_later_paragraph(self):
        text = "A stray ` here.\n\nSecond paragraph with real words that must stay ` there."
        result = filter_text(text)
        assert "Second paragraph with real words that must stay" in result

    def test_document_filter_keeps_long_spans_for_path_speech(self):
        text = "Policies from `.claude/settings.json` apply."
        assert "dot claude settings dot json" in filter_document(text)


class TestBarePaths:
    """The rule is length, not kind: a path of 20 characters or fewer is spoken, longer is not."""

    def test_short_paths_are_spoken(self):
        text = "Edit `src/x.py` and `tests/test_x.py` now"
        assert filter_text(text) == "Edit src/x.py and tests/test_x.py now"

    def test_short_bare_path_is_spoken(self):
        assert filter_text("It lives in /etc/foo/bar.conf.") == "It lives in /etc/foo/bar.conf."

    def test_path_of_exactly_20_chars_is_spoken_and_21_is_not(self):
        p20 = "src/abcdefghijkl.txt"
        assert len(p20) == 20
        assert filter_text(f"Open {p20} now.") == f"Open {p20} now."
        assert filter_text("Open src/abcdefghijklm.txt now.") == "Open now."

    def test_long_paths_of_every_shape_are_removed(self):
        text = (
            "Edit src/claude_code_tts/cli.py and /usr/local/share/foo/bar then ~/.config/claude/x.json, "
            "./scripts/some_long_name.sh."
        )
        result = filter_text(text)
        for leaked in ("/", "cli.py", "foo", "x.json", "some_long"):
            assert leaked not in result, leaked
        assert result == "Edit then."

    def test_long_path_ending_a_sentence_keeps_the_stop(self):
        assert filter_text("It lives in /etc/some/long/dir/bar.conf.") == "It lives."

    def test_path_with_a_line_number_obeys_the_length_rule(self):
        assert filter_text("Look at src/claude_code_tts/filter.py:42 now.") == "Look at now."
        assert filter_text("Look at src/claude_code_tts/filter.py:42:7 now.") == "Look at now."
        assert filter_text("Look at src/x.py:42 now.") == "Look at src/x.py:42 now."

    def test_words_with_slashes_are_not_paths(self):
        text = "Use and/or TCP/IP on 10/01/2026 for Linux/macOS/Windows via /tts-mute."
        assert filter_text(text) == text


class TestTables:
    TABLE = "| Name | Value |\n|------|-------|\n| foo | 42 |\n| bar | 99 |"

    def test_table_becomes_one_phrase_counting_body_rows(self):
        result = filter_text(f"Here.\n\n{self.TABLE}\n\nDone.")
        assert result == "Here. table of 2 rows. Done."

    def test_header_and_separator_are_not_counted(self):
        assert filter_text("| a | b |\n|---|---|\n| 1 | x |\n| 2 | y |\n| 3 | z |") == "table of 3 rows."

    def test_zero_body_rows(self):
        assert filter_text("| a | b |\n|---|---|\nNext.") == "table of 0 rows. Next."

    def test_one_row_is_singular(self):
        assert filter_text("| a | b |\n|---|---|\n| 1 | 2 |") == "table of 1 row."

    def test_caption_line_above_is_kept(self):
        assert filter_text(f"Results by host:\n{self.TABLE}") == "Results by host: table of 2 rows."

    def test_heading_line_above_is_kept(self):
        assert filter_text(f"## Results\n{self.TABLE}") == "Results table of 2 rows."

    def test_two_tables_in_one_reply(self):
        text = "One\n| a | b |\n|---|---|\n| 1 | 2 |\n\nTwo\n| a | b |\n| --- | --- |\n| 1 | 2 |\n| 3 | 4 |"
        assert filter_text(text) == "One table of 1 row. Two table of 2 rows."

    def test_table_inside_a_fenced_block_says_nothing(self):
        text = f"Look:\n```\n{self.TABLE}\n```\nOk."
        assert filter_text(text) == "Look: Ok."

    def test_pipes_in_prose_are_not_a_table(self):
        text = "Use a | b to pipe.\nAnd c | d too."
        assert filter_text(text) == "Use a | b to pipe. And c | d too."

    def test_pipe_line_followed_by_a_dash_rule_is_not_a_table(self):
        assert filter_text("a | b\n---\nafter") == "a | b after"

    def test_separator_must_match_the_header_width(self):
        text = "| a | b | c |\n|---|---|\n| 1 | 2 |"
        assert "table of" not in filter_text(text)

    def test_alignment_colons_in_the_separator(self):
        assert filter_text("| a | b |\n|:--|--:|\n| 1 | 2 |") == "table of 1 row."

    def test_table_without_outer_pipes(self):
        assert filter_text("a | b | c\n--|--|--\n1 | 2 | 3\n4 | 5 | 6") == "table of 2 rows."

    def test_header_and_separator_of_the_same_width_is_a_table_whatever_the_header_says(self):
        # CommonMark: a header line over a separator of the same cell count IS a table, so
        # "Total: 5 | 6" over "---|---" is a table of 0 rows. Prose is not guessed at.
        # (Reverses the 9.39.8 test that called it prose.)
        assert filter_text("Total: 5 | 6\n---|---\nok") == "table of 0 rows. ok"

    def test_three_cells_over_three_dashes_is_a_table_of_zero_rows_then_prose(self):
        assert filter_text("Choose A | B | C\n---|---|---\nnext.") == "table of 0 rows. next."

    def test_two_column_table_without_outer_pipes(self):
        assert filter_text("a | b\n--|--\n1 | 2\n3 | 4") == "table of 2 rows."

    def test_single_column_table_with_outer_pipes(self):
        assert filter_text("| a |\n|---|\n| 1 |") == "table of 1 row."
        assert filter_text("| a |\n|---|\n| 1 |\n| 2 |\n| 3 |") == "table of 3 rows."

    def test_separator_without_a_pipe_never_makes_a_single_column_table(self):
        assert "table of" not in filter_text("a |\n---\nafter")

    def test_pipe_led_row_wider_than_the_header_plus_one_is_prose(self):
        text = "| a | b |\n|---|---|\n| 1 | 2 |\n| also | prose | with | pipes |\nafter"
        assert filter_text(text) == "table of 1 row. | also | prose | with | pipes | after"

    def test_pipe_led_row_with_fewer_cells_or_one_extra_is_a_row(self):
        text = "| a | b |\n|---|---|\n| 1 |\n| 2 | 3 | 4 |"
        assert filter_text(text) == "table of 2 rows."

    def test_sentence_with_a_pipe_under_a_table_is_not_a_row(self):
        text = "| a | b |\n|---|---|\n| 1 | 2 |\nNote: x | y\nafter"
        assert filter_text(text) == "table of 1 row. Note: x | y after"

    def test_row_without_outer_pipes_needs_the_header_width(self):
        text = "a | b | c\n--|--|--\n1 | 2 | 3\nsee x | y\nafter"
        assert filter_text(text) == "table of 1 row. see x | y after"

    def test_pipe_inside_inline_code_is_not_a_column(self):
        text = "a | b | c\n--|--|--\n`x|y` | 2 | 3\n4 | 5 | 6"
        assert filter_text(text) == "table of 2 rows."

    def test_urls_in_cells_do_not_break_the_count(self):
        text = "| a | b |\n|---|---|\n|https://x.example/a|y|\n| [z](https://q.example) | w |"
        assert filter_text(text) == "table of 2 rows."

    def test_document_filter_says_the_same_phrase(self):
        assert filter_document(f"Results:\n{self.TABLE}\nAfter.") == "Results: table of 2 rows. After."


class TestUrlsInParentheses:
    def test_url_alone_in_parentheses_takes_the_parentheses(self):
        assert filter_text("The docs (https://example.com/a/b) explain it") == "The docs explain it"

    def test_lead_in_word_goes_with_it(self):
        assert filter_text("The docs (see https://example.com/a/b) explain it.") == "The docs explain it."

    def test_bracketed_url(self):
        assert filter_text("The docs [https://example.com/a/b] explain it.") == "The docs explain it."


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

    OAUTH = (
        "Two servers need a browser login:\n\n"
        "  - Jira: port 51362, https://mcp.example.com/v1/authorize?client_id=fakeclientid00001"
        "&code_challenge=fakechallenge0000000000000000000000000001&code_challenge_method=S256"
        "&redirect_uri=http%3A%2F%2F127.0.0.1%3A51362%2Fcallback&resource=https%3A%2F%2Fmcp.example.com"
        "%2Fv1%2Fmcp&response_type=code&state=fakestate000000000000000000000000000000001\n"
        "  - Notes: port 51372, https://mcp.example.org/authorize?client_id=fakeclientid00002"
        "&code_challenge=fakechallenge0000000000000000000000000002&scope=default"
        "&state=fakestate000000000000000000000000000000002\n\n"
        "Open each in a browser."
    )

    def test_oauth_links_vanish_whole_before_redaction_can_split_them(self):
        # Regression: redaction used to run first, break the URL with spaces, and the
        # unrecognised tail (redirect_uri, resource, percent-encoding) was spoken.
        result = filter_text(self.OAUTH)
        for leaked in ("redirect_uri", "resource=", "response_type", "%2F", "%3A",
                       "code_challenge", "client_id", "authorize", "redacted"):
            assert leaked not in result, leaked
        assert "Jira: port 51362," in result
        assert "Notes: port 51372," in result
        assert result.endswith("Open each in a browser.")

    def test_percent_encoded_url_and_query_debris_removed(self):
        text = "Go to https%3A%2F%2Fexample.com%2Fcb and redirect_uri=http%3A%2F%2Fhost&x=1 now"
        result = filter_text(text)
        assert "%" not in result and "redirect" not in result and "example" not in result
        assert result.startswith("Go to") and result.endswith("now")

    def test_www_url_removed(self):
        assert "www." not in filter_text("See www.example.com/path?a=b for details")

    def test_ordinary_percent_and_ampersand_survive(self):
        result = filter_text("Coverage rose 12% and R&D shipped 3% more.")
        assert "12%" in result and "R&D" in result


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
