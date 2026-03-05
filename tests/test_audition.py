"""Tests for audition subcommand — non-interactive logic."""

import json
from unittest.mock import patch
import argparse

import pytest

import claude_code_tts.config as config_mod
import claude_code_tts.audio as audio_mod


# ---------------------------------------------------------------------------
# Helpers — extracted from cmd_audition's inner functions for testability
# ---------------------------------------------------------------------------


def kokoro_display_name(voice: str) -> str:
    """Convert 'am_adam' to 'Adam'. Mirror of _kokoro_display_name in cli.py."""
    raw = voice.split("_", 1)[1] if "_" in voice else voice
    return " ".join(w.capitalize() for w in raw.replace("_", " ").split())


def filter_voices(voices: list[str], filter_arg: str) -> list[str]:
    """Apply prefix filter. Mirror of filter logic in cmd_audition."""
    prefixes = [p.strip() for p in filter_arg.split(",")]
    return [v for v in voices if any(v.startswith(p) for p in prefixes)]


def parse_range(range_arg: str, num_speakers: int) -> list[int]:
    """Parse --range N-M into speaker list. Mirror of range logic in cmd_audition."""
    if "-" in range_arg:
        lo, hi = range_arg.split("-", 1)
        lo_i = max(0, int(lo))
        hi_i = min(num_speakers - 1, int(hi))
        return list(range(lo_i, hi_i + 1))
    return []


# ---------------------------------------------------------------------------
# TestKokoroDisplayName
# ---------------------------------------------------------------------------


class TestKokoroDisplayName:
    def test_standard_voice(self):
        assert kokoro_display_name("am_adam") == "Adam"

    def test_female_voice(self):
        assert kokoro_display_name("af_heart") == "Heart"

    def test_multi_word(self):
        assert kokoro_display_name("bf_emma_jones") == "Emma Jones"

    def test_no_prefix(self):
        assert kokoro_display_name("standalone") == "Standalone"

    def test_british_male(self):
        assert kokoro_display_name("bm_george") == "George"


# ---------------------------------------------------------------------------
# TestFilterVoices
# ---------------------------------------------------------------------------


class TestFilterVoices:
    VOICE_LIST = [
        "af_heart", "af_bella", "af_nicole",
        "am_adam", "am_michael",
        "bf_emma", "bf_isabella",
        "bm_george", "bm_lewis",
    ]

    def test_single_prefix(self):
        result = filter_voices(self.VOICE_LIST, "af_")
        assert result == ["af_heart", "af_bella", "af_nicole"]

    def test_multiple_prefixes(self):
        result = filter_voices(self.VOICE_LIST, "am_,bm_")
        assert result == ["am_adam", "am_michael", "bm_george", "bm_lewis"]

    def test_no_match(self):
        result = filter_voices(self.VOICE_LIST, "xx_")
        assert result == []

    def test_whitespace_in_filter(self):
        result = filter_voices(self.VOICE_LIST, "af_ , bm_")
        assert result == ["af_heart", "af_bella", "af_nicole", "bm_george", "bm_lewis"]

    def test_all_match(self):
        result = filter_voices(self.VOICE_LIST, "af_,am_,bf_,bm_")
        assert result == self.VOICE_LIST

    def test_single_voice_exact(self):
        result = filter_voices(self.VOICE_LIST, "af_heart")
        assert result == ["af_heart"]


# ---------------------------------------------------------------------------
# TestRangeParsing
# ---------------------------------------------------------------------------


class TestRangeParsing:
    def test_basic_range(self):
        assert parse_range("0-5", 100) == [0, 1, 2, 3, 4, 5]

    def test_range_clamped_to_speakers(self):
        assert parse_range("0-200", 10) == list(range(10))

    def test_range_starts_mid(self):
        assert parse_range("5-8", 100) == [5, 6, 7, 8]

    def test_range_zero_start(self):
        """Range starting at 0 works."""
        assert parse_range("0-3", 100) == [0, 1, 2, 3]

    def test_single_speaker_range(self):
        assert parse_range("42-42", 100) == [42]

    def test_no_dash(self):
        assert parse_range("42", 100) == []


# ---------------------------------------------------------------------------
# TestQueueSpeak — queue message construction
# ---------------------------------------------------------------------------


class TestQueueSpeak:
    @pytest.fixture
    def queue_dir(self, tmp_path):
        qdir = tmp_path / "queue"
        qdir.mkdir()
        return qdir

    def test_queue_message_kokoro(self, queue_dir):
        """Queue message carries kokoro voice override."""
        with patch.object(audio_mod, "TTS_QUEUE_DIR", queue_dir), \
             patch.object(audio_mod, "daemon_healthy", return_value=True):
            cfg = config_mod.TTSConfig(
                mode="queue",
                speed=1.5,
                speed_method="playback",
                voice_kokoro="am_adam",
                session_id="audition",
                project_name="audition",
            )
            path = audio_mod.write_queue_message("Hello world", cfg)

            assert path.exists()
            msg = json.loads(path.read_text())
            assert msg["text"] == "Hello world"
            assert msg["voice_kokoro"] == "am_adam"
            assert msg["speed"] == 1.5
            assert msg["session_id"] == "audition"

    def test_queue_message_blend(self, queue_dir):
        """Queue message carries blend spec."""
        with patch.object(audio_mod, "TTS_QUEUE_DIR", queue_dir):
            cfg = config_mod.TTSConfig(
                mode="queue",
                speed=2.0,
                speed_method="playback",
                voice_kokoro_blend="am_adam:60,af_heart:40",
                session_id="audition",
                project_name="audition",
            )
            path = audio_mod.write_queue_message("Test blend", cfg)

            msg = json.loads(path.read_text())
            assert msg["voice_kokoro_blend"] == "am_adam:60,af_heart:40"
            assert msg["voice_kokoro"] == ""

    def test_queue_message_has_required_fields(self, queue_dir):
        """Queue messages contain all fields the daemon expects."""
        with patch.object(audio_mod, "TTS_QUEUE_DIR", queue_dir):
            cfg = config_mod.TTSConfig(
                mode="queue",
                speed=2.0,
                speed_method="playback",
                session_id="audition",
                project_name="audition",
            )
            path = audio_mod.write_queue_message("check fields", cfg)
            msg = json.loads(path.read_text())

            required = {"id", "timestamp", "session_id", "project", "text",
                        "persona", "speed", "speed_method", "voice_kokoro",
                        "voice_kokoro_blend"}
            assert required.issubset(set(msg.keys()))


# ---------------------------------------------------------------------------
# TestQueueFallback — --queue with non-Kokoro falls back to direct
# ---------------------------------------------------------------------------


class TestQueueFallback:
    def test_piper_queue_warns_and_falls_back(self, capsys):
        """--queue with Piper voice prints warning and falls back."""
        from claude_code_tts.cli import cmd_audition

        args = argparse.Namespace(
            voice="en_US-hfc_male-medium",
            speakers=None,
            kokoro=False,
            blend=None,
            filter=None,
            text="test",
            range=None,
            queue=True,
            speed=1.5,
        )

        # Mock audio imports that happen inside cmd_audition
        with patch("claude_code_tts.audio.generate_speech", return_value=None), \
             patch("claude_code_tts.audio.detect_player", return_value=None), \
             patch("claude_code_tts.audio.daemon_healthy", return_value=False):
            # Will fail at terminal interaction after printing fallback warning
            try:
                cmd_audition(args)
            except (SystemExit, EOFError, OSError, ValueError):
                pass

        captured = capsys.readouterr()
        assert "--queue only supported with Kokoro" in captured.out

    def test_kokoro_queue_no_fallback_warning(self, capsys):
        """--queue with --kokoro does NOT print fallback warning."""
        from claude_code_tts.cli import cmd_audition

        args = argparse.Namespace(
            voice=None,
            speakers=None,
            kokoro=True,
            blend=None,
            filter=None,
            text="test",
            range=None,
            queue=True,
            speed=1.5,
        )

        # swift-kokoro not found -> exits early before any interactive bits
        with patch("shutil.which", return_value=None):
            with pytest.raises(SystemExit):
                cmd_audition(args)

        captured = capsys.readouterr()
        assert "--queue only supported" not in captured.out

    def test_blend_queue_no_fallback_warning(self, capsys):
        """--queue with --blend does NOT print fallback warning."""
        from claude_code_tts.cli import cmd_audition

        args = argparse.Namespace(
            voice=None,
            speakers=None,
            kokoro=False,
            blend="am_adam,af_heart",
            filter=None,
            text="test",
            range=None,
            queue=True,
            speed=1.5,
        )

        # swift-kokoro not found -> exits early
        with patch("shutil.which", return_value=None):
            with pytest.raises(SystemExit):
                cmd_audition(args)

        captured = capsys.readouterr()
        assert "--queue only supported" not in captured.out


# ---------------------------------------------------------------------------
# TestBlendArgParsing
# ---------------------------------------------------------------------------


class TestBlendArgParsing:
    def test_two_voices(self):
        voices = [v.strip() for v in "am_adam,af_heart".split(",")]
        assert voices == ["am_adam", "af_heart"]

    def test_whitespace(self):
        voices = [v.strip() for v in "am_adam , af_heart".split(",")]
        assert voices == ["am_adam", "af_heart"]

    def test_single_voice_insufficient(self):
        voices = [v.strip() for v in "am_adam".split(",")]
        assert len(voices) < 2

    def test_three_voices_uses_first_two(self):
        voices = [v.strip() for v in "am_adam,af_heart,bf_emma".split(",")]
        v1, v2 = voices[0], voices[1]
        assert v1 == "am_adam"
        assert v2 == "af_heart"

    def test_blend_ratio_spec(self):
        """Blend spec format: voice:weight,voice:weight."""
        v1, v2 = "am_adam", "af_heart"
        for w1, w2 in [(80, 20), (60, 40), (50, 50), (40, 60), (20, 80)]:
            spec = f"{v1}:{w1},{v2}:{w2}"
            assert ":" in spec
            parts = spec.split(",")
            assert len(parts) == 2
            assert parts[0] == f"am_adam:{w1}"
            assert parts[1] == f"af_heart:{w2}"
