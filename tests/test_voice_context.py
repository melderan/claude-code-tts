"""Tests for the voice context mutating webhook."""

import pytest

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


def _analysis_db(tmp_path, rows):
    """An analysis DB in the daemon's schema holding (file_name, age_s, transcript, tone) rows."""
    import time

    from claude_code_tts.handy import _init_db
    db = tmp_path / "handy_analysis.db"
    conn = _init_db(db)
    now = time.time()
    for name, age, text, tone in rows:
        conn.execute(
            "INSERT INTO voice_analysis (file_name, timestamp, transcript, tone_summary, analyzed_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (name, int(now - age), text, tone, now - age),
        )
    conn.commit()
    conn.close()
    return db


SPOKEN = "what the hell is that thing doing in the queue again"


class TestEnrichMessage:
    def test_a_dictated_segment_carries_its_recordings_tone(self, tmp_path):
        """The transcript and tone come from the analysis DB alone, the file a sandbox can see."""
        from claude_code_tts.voice_context import enrich_message
        db = _analysis_db(tmp_path, [("a.wav", 30, SPOKEN, "animated and expressive")])
        out = enrich_message(f"{SPOKEN}\n\nplease look", max_age_seconds=600, db_path=db)
        assert out == "[Voice context]\n[Segment 1: animated and expressive]"

    def test_typed_text_and_old_recordings_stay_unmarked(self, tmp_path):
        from claude_code_tts.voice_context import enrich_message
        db = _analysis_db(tmp_path, [("old.wav", 900, SPOKEN, "animated and expressive")])
        assert enrich_message(SPOKEN, max_age_seconds=600, db_path=db) is None
        db2 = _analysis_db(tmp_path / "b", [("a.wav", 30, SPOKEN, "animated and expressive")])
        assert enrich_message("a line typed by hand, nothing spoken", max_age_seconds=600, db_path=db2) is None


def test_the_hook_reads_the_prompt_field_claude_code_sends(monkeypatch, capsys):
    """UserPromptSubmit puts the text in top-level "prompt"; reading anything else matches nothing."""
    import io
    import json

    import claude_code_tts.handy as handy_mod
    import claude_code_tts.voice_context as vc
    from claude_code_tts import cli as cli_mod
    monkeypatch.setattr(handy_mod, "get_aggregated_tone", lambda **kw: None)
    monkeypatch.setattr(vc, "_get_recent_transcripts", lambda **kw: [
        {"file_name": "a.wav", "timestamp": 0, "text": SPOKEN, "tone": "speaking quietly"}])
    stdin = io.StringIO(json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": SPOKEN}))
    stdin.isatty = lambda: False  # type: ignore[method-assign]
    monkeypatch.setattr("sys.stdin", stdin)
    monkeypatch.setattr("sys.argv", ["claude-tts", "handy", "tone-context"])
    cli_mod.main()
    assert capsys.readouterr().out == "[Voice context]\n[Segment 1: speaking quietly]\n"


@pytest.mark.parametrize(("cli_body", "out"), [
    ('echo "[Voice context: JMO is calm]"; exit 0', "[Voice context: JMO is calm]\n"),  # the context joins the prompt
    ('echo "usage: invalid choice" >&2; exit 2', ""),                                 # a CLI older than the subcommand
])
def test_the_shim_passes_the_context_and_never_blocks_the_prompt(tmp_path, cli_body, out):
    """A UserPromptSubmit hook's stdout is added to the prompt and exit 2 blocks it."""
    import os
    import subprocess
    from pathlib import Path
    shim = Path(__file__).resolve().parent.parent / "hooks" / "voice-context.sh"
    fake = tmp_path / "claude-tts"
    fake.write_text(f"#!/bin/bash\n{cli_body}\n")
    fake.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ.get('PATH', '')}"}
    r = subprocess.run(["bash", str(shim)], input="{}", capture_output=True, text=True, env=env, timeout=10)
    assert (r.returncode, r.stdout, r.stderr) == (0, out, "")
