"""Tests for `claude-tts sherpa list` and `claude-tts speak --voice-sherpa`.

The two operator-facing surfaces added in v9.3.0:
  - `sherpa list` enumerates installed models with their detected layout
    family, so the operator can verify a hand-dropped model is recognized.
  - `speak --voice-sherpa <id> --speaker-sherpa <n>` is the one-shot path
    for "did this model just work?" before wiring it into a persona.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from claude_code_tts import cli


# --- Layout detection ----------------------------------------------------


class TestLayoutDetection:
    def test_vits_minimum(self, tmp_path):
        (tmp_path / "model.onnx").write_bytes(b"")
        (tmp_path / "tokens.txt").write_text("")
        assert cli._detect_sherpa_layout(tmp_path) == "vits"

    def test_kokoro_when_voices_bin_present(self, tmp_path):
        (tmp_path / "model.onnx").write_bytes(b"")
        (tmp_path / "tokens.txt").write_text("")
        (tmp_path / "voices.bin").write_bytes(b"")
        assert cli._detect_sherpa_layout(tmp_path) == "kokoro"

    def test_matcha_when_am_and_vocoder(self, tmp_path):
        (tmp_path / "am.onnx").write_bytes(b"")
        (tmp_path / "vocoder.onnx").write_bytes(b"")
        (tmp_path / "tokens.txt").write_text("")
        assert cli._detect_sherpa_layout(tmp_path) == "matcha"

    def test_incomplete_when_tokens_missing(self, tmp_path):
        (tmp_path / "model.onnx").write_bytes(b"")
        # no tokens.txt
        assert cli._detect_sherpa_layout(tmp_path) == "incomplete"

    def test_incomplete_when_model_missing(self, tmp_path):
        (tmp_path / "tokens.txt").write_text("")
        # no model.onnx
        assert cli._detect_sherpa_layout(tmp_path) == "incomplete"


# --- `sherpa list` -------------------------------------------------------


class TestSherpaList:
    def test_empty_dir_prints_helpful_next_steps(self, tmp_path, monkeypatch, capsys):
        empty = tmp_path / "sherpa-models"
        empty.mkdir()
        monkeypatch.setattr("claude_code_tts.config.SHERPA_MODELS_DIR", empty)
        monkeypatch.setattr(
            "claude_code_tts.config.SHERPA_VENV_DIR", tmp_path / "no-venv"
        )

        args = argparse.Namespace(sherpa_command="list")
        cli.cmd_sherpa(args)
        out = capsys.readouterr().out
        assert "No models installed" in out
        # Points user at the curated picklist + manual-install fallback
        assert "claude-tts sherpa list-available" in out
        assert "claude-tts sherpa install" in out

    def test_missing_dir_prints_helpful_next_steps(self, tmp_path, monkeypatch, capsys):
        missing = tmp_path / "nope"
        monkeypatch.setattr("claude_code_tts.config.SHERPA_MODELS_DIR", missing)
        monkeypatch.setattr(
            "claude_code_tts.config.SHERPA_VENV_DIR", tmp_path / "no-venv"
        )

        args = argparse.Namespace(sherpa_command="list")
        cli.cmd_sherpa(args)
        out = capsys.readouterr().out
        assert "No models installed" in out
        assert "claude-tts sherpa list-available" in out

    def test_lists_installed_models_with_layouts(self, tmp_path, monkeypatch, capsys):
        models_dir = tmp_path / "sherpa-models"
        models_dir.mkdir()

        # A complete VITS layout
        vits = models_dir / "libritts-vits"
        vits.mkdir()
        (vits / "model.onnx").write_bytes(b"x" * 1024)
        (vits / "tokens.txt").write_text("a b c")

        # A complete Kokoro layout
        kokoro = models_dir / "kokoro-en"
        kokoro.mkdir()
        (kokoro / "model.onnx").write_bytes(b"y" * 2048)
        (kokoro / "tokens.txt").write_text("a")
        (kokoro / "voices.bin").write_bytes(b"z" * 512)

        # An incomplete one (only model.onnx, no tokens)
        broken = models_dir / "half-installed"
        broken.mkdir()
        (broken / "model.onnx").write_bytes(b"")

        venv = tmp_path / "venvs" / "sherpa" / "bin"
        venv.mkdir(parents=True)
        (venv / "python").write_text("#!/bin/sh\n")

        monkeypatch.setattr("claude_code_tts.config.SHERPA_MODELS_DIR", models_dir)
        monkeypatch.setattr(
            "claude_code_tts.config.SHERPA_VENV_DIR", tmp_path / "venvs" / "sherpa"
        )

        args = argparse.Namespace(sherpa_command="list")
        cli.cmd_sherpa(args)
        out = capsys.readouterr().out

        assert "libritts-vits" in out
        assert "vits" in out
        assert "kokoro-en" in out
        assert "kokoro" in out
        assert "half-installed" in out
        assert "incomplete" in out
        # Venv ready hint
        assert "ready" in out
        # The footer test-a-voice hint should appear
        assert "claude-tts speak --voice-sherpa" in out

    def test_reports_venv_not_bootstrapped(self, tmp_path, monkeypatch, capsys):
        models_dir = tmp_path / "sherpa-models"
        models_dir.mkdir()
        monkeypatch.setattr("claude_code_tts.config.SHERPA_MODELS_DIR", models_dir)
        monkeypatch.setattr(
            "claude_code_tts.config.SHERPA_VENV_DIR", tmp_path / "missing-venv"
        )

        args = argparse.Namespace(sherpa_command="list")
        cli.cmd_sherpa(args)
        out = capsys.readouterr().out
        assert "NOT bootstrapped" in out
        assert "claude-tts-install --enable-sherpa" in out


# --- `speak --voice-sherpa` ---------------------------------------------


class TestSpeakSherpaFlag:
    """The flag should route through generate_speech with voice_sherpa set
    and disable Piper/Kokoro paths for that one-shot call."""

    def test_voice_sherpa_flag_routes_to_sherpa(self, tmp_path, monkeypatch):
        captured = {}

        def fake_generate_speech(text, **kwargs):
            captured.update(kwargs)
            captured["text"] = text
            return None  # don't try to play anything

        # Mock load_config to return a clean-defaults config
        from claude_code_tts.config import TTSConfig
        monkeypatch.setattr(
            "claude_code_tts.cli.load_config",
            lambda *_: TTSConfig(speed=1.0, speed_method="playback"),
        )
        monkeypatch.setattr(
            "claude_code_tts.audio.generate_speech", fake_generate_speech
        )

        args = argparse.Namespace(
            text="hello there",
            voice=None,
            speed=None,
            speaker=None,
            random=False,
            voice_sherpa="my-model",
            speaker_sherpa=42,
            from_file=None,
            preview=False,
            reader=False,
            from_hook=False,
            hook_type=None,
        )

        with pytest.raises(SystemExit):
            cli.cmd_speak(args)

        assert captured.get("voice_sherpa") == "my-model"
        assert captured.get("speaker_sherpa") == 42
        # Piper/Kokoro paths disabled for this call
        assert captured.get("voice_path") is None
        assert captured.get("voice_kokoro") == ""
        assert captured.get("voice_kokoro_blend") == ""
        assert captured.get("text") == "hello there"

    def test_voice_flag_disables_voice_sherpa(self, tmp_path, monkeypatch):
        """If both --voice (Piper) and persona-config voice_sherpa are set,
        --voice wins and clears voice_sherpa."""
        captured = {}

        def fake_generate_speech(text, **kwargs):
            captured.update(kwargs)
            return None

        from claude_code_tts.config import TTSConfig
        monkeypatch.setattr(
            "claude_code_tts.cli.load_config",
            lambda *_: TTSConfig(
                voice_sherpa="config-set-model",
                speaker_sherpa=7,
                speed=1.0,
                speed_method="playback",
            ),
        )
        monkeypatch.setattr(
            "claude_code_tts.audio.generate_speech", fake_generate_speech
        )

        args = argparse.Namespace(
            text="hello",
            voice="some-piper-voice",
            speed=None,
            speaker=None,
            random=False,
            voice_sherpa=None,  # no CLI override
            speaker_sherpa=-1,
            from_file=None,
            preview=False,
            reader=False,
            from_hook=False,
            hook_type=None,
        )

        with pytest.raises(SystemExit):
            cli.cmd_speak(args)

        # --voice clears sherpa even if persona config set it
        assert captured.get("voice_sherpa") == ""
