"""Tests for the voice context mutating webhook."""

from claude_code_tts.voice_context import (
    _find_transcript_in_message,
    _normalize_for_matching,
)


class TestNormalize:
    def test_collapses_whitespace(self):
        assert _normalize_for_matching("hello   world") == "hello world"

    def test_lowercases(self):
        assert _normalize_for_matching("Hello World") == "hello world"

    def test_strips_newlines(self):
        assert _normalize_for_matching("hello\nworld") == "hello world"


class TestFindTranscriptInMessage:
    def test_exact_match(self):
        transcript = "hello world this is a test"
        message = "some prefix hello world this is a test some suffix"
        msg_norm = _normalize_for_matching(message)
        result = _find_transcript_in_message(transcript, message, msg_norm)
        assert result is not None
        start, end = result
        assert msg_norm[start:end] == _normalize_for_matching(transcript)

    def test_case_insensitive_match(self):
        transcript = "Hello World This Is A Test"
        message = "prefix hello world this is a test suffix"
        msg_norm = _normalize_for_matching(message)
        result = _find_transcript_in_message(transcript, message, msg_norm)
        assert result is not None

    def test_no_match(self):
        transcript = "completely different text"
        message = "this message has nothing in common"
        msg_norm = _normalize_for_matching(message)
        result = _find_transcript_in_message(transcript, message, msg_norm)
        assert result is None

    def test_prefix_match_long_transcript(self):
        """When full match fails but first 50 chars match."""
        transcript = "this is a long transcript that starts the same way but diverges in the middle and then comes back at the end"
        # Message has the same start but different middle
        message = "this is a long transcript that starts the same way but has edits here and then comes back at the end"
        msg_norm = _normalize_for_matching(message)
        result = _find_transcript_in_message(transcript, message, msg_norm)
        assert result is not None

    def test_head_tail_match(self):
        """When first 40 + last 40 chars match but middle differs."""
        head = "this is the beginning of a long speech that"
        tail = "and this is definitely the end of it all"
        transcript = head + " MIDDLE PART ONE " + tail
        message = "prefix " + head + " different middle " + tail + " suffix"
        msg_norm = _normalize_for_matching(message)
        result = _find_transcript_in_message(transcript, message, msg_norm)
        assert result is not None

    def test_short_transcript_substring_match(self):
        """Short transcripts match via full substring if present."""
        transcript = "a longer message"
        message = "this is a longer message with more"
        msg_norm = _normalize_for_matching(message)
        result = _find_transcript_in_message(transcript, message, msg_norm)
        assert result is not None

    def test_short_transcript_no_match(self):
        """Short transcripts don't match when not present."""
        transcript = "something completely different"
        message = "this is a longer message"
        msg_norm = _normalize_for_matching(message)
        result = _find_transcript_in_message(transcript, message, msg_norm)
        assert result is None

    def test_multiple_transcripts_in_order(self):
        """Two transcripts in the same message should match at different positions."""
        t1 = "first block of speech that is long enough to match properly"
        t2 = "second block of speech that is also long enough to match"
        message = f"intro {t1} middle {t2} outro"
        msg_norm = _normalize_for_matching(message)

        r1 = _find_transcript_in_message(t1, message, msg_norm)
        r2 = _find_transcript_in_message(t2, message, msg_norm)
        assert r1 is not None
        assert r2 is not None
        assert r1[0] < r2[0]  # First block appears before second

    def test_whitespace_differences(self):
        """Transcript with different whitespace should still match."""
        transcript = "hello    world   with   extra   spaces   and   more   words  to   make  it  long  enough"
        message = "hello world with extra spaces and more words to make it long enough"
        msg_norm = _normalize_for_matching(message)
        result = _find_transcript_in_message(transcript, message, msg_norm)
        assert result is not None
