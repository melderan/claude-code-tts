"""Tests for audition subcommand — non-interactive logic."""

import argparse
import json
from unittest.mock import patch

import pytest

import claude_code_tts.audio as audio_mod
import claude_code_tts.config as config_mod

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
