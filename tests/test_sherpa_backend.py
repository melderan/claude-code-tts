"""Tests for the sherpa-onnx backend (additive third TTS engine).

Two principles under test:
  1. Sherpa is OPT-IN. Personas that don't set voice_sherpa never invoke it.
     This protects existing personas (claude-prime, claude-connery, etc.) from
     accidental migration onto a new engine they didn't choose.
  2. When voice_sherpa IS set, generate_speech builds a subprocess call to
     the isolated venv's Python, invoking claude_code_tts.sherpa_speak with
     the right model dir, speaker, speed, and output path.

The helper script (sherpa_speak.py) is exercised separately for its model-
layout auto-detection logic — it imports sherpa_onnx, which only the
isolated venv has, so we test the parts that don't need sherpa_onnx.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from claude_code_tts import audio
from claude_code_tts.audio import generate_speech


@pytest.fixture
def fake_sherpa_layout(tmp_path, monkeypatch):
    """Build a fake sherpa venv + models dir under tmp_path."""
    venv = tmp_path / "venvs" / "sherpa"
    (venv / "bin").mkdir(parents=True)
    py = venv / "bin" / "python"
    py.write_text("#!/bin/sh\nexit 0\n")
    py.chmod(0o755)

    models = tmp_path / "sherpa-models"
    models.mkdir()

    monkeypatch.setattr(audio, "SHERPA_VENV_DIR", venv)
    monkeypatch.setattr(audio, "SHERPA_MODELS_DIR", models)

    # Provide a single model directory so generate_sherpa finds something.
    (models / "vctk-vits").mkdir()
    (models / "vctk-vits" / "model.onnx").write_bytes(b"")
    (models / "vctk-vits" / "tokens.txt").write_text("")

    return {"venv": venv, "models": models, "py": py}


class TestSherpaIsOptIn:
    """Existing personas (voice_sherpa='') never invoke the sherpa branch."""

    def test_empty_voice_sherpa_skips_sherpa_branch(self, tmp_path, fake_sherpa_layout):
        """When voice_sherpa is empty, _generate_sherpa is never called."""
        with patch("claude_code_tts.audio._generate_sherpa") as mock_sherpa, \
             patch("claude_code_tts.audio.shutil.which", return_value=None):
            generate_speech(
                "hello world",
                voice_sherpa="",  # opted out
                output_path=tmp_path / "out.wav",
            )
        mock_sherpa.assert_not_called()

    def test_kokoro_persona_unaffected(self, tmp_path, fake_sherpa_layout):
        """A persona with voice_kokoro set takes the Kokoro branch, not sherpa."""
        called = {"kokoro": False, "sherpa": False}

        def kokoro_which(name):
            return "/usr/bin/swift-kokoro" if name == "swift-kokoro" else None

        def fake_run(cmd, **kwargs):
            if cmd[0] == "swift-kokoro":
                called["kokoro"] = True
                Path(kwargs.get("input") and "" or "")  # keep linter happy
                # Touch the output path so generate_speech returns it.
                out = Path(cmd[cmd.index("--output") + 1])
                out.write_bytes(b"\x00")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("claude_code_tts.audio.shutil.which", side_effect=kokoro_which), \
             patch("claude_code_tts.audio.subprocess.run", side_effect=fake_run), \
             patch("claude_code_tts.audio._generate_sherpa") as mock_sherpa:
            generate_speech(
                "hello world",
                voice_kokoro="af_sky",
                voice_sherpa="",  # not set, even though sherpa is bootstrapped
                output_path=tmp_path / "out.wav",
            )
        assert called["kokoro"] is True
        mock_sherpa.assert_not_called()


class TestSherpaRouting:
    """When voice_sherpa is set, sherpa is invoked correctly."""

    def test_sherpa_invoked_when_voice_sherpa_set(self, tmp_path, fake_sherpa_layout):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["text"] = kwargs.get("input")
            # Touch the output to mimic successful generation.
            out_idx = cmd.index("--output") + 1
            Path(cmd[out_idx]).write_bytes(b"RIFFfake")
            return MagicMock(returncode=0, stdout="", stderr="")

        out = tmp_path / "out.wav"
        with patch("claude_code_tts.audio.subprocess.run", side_effect=fake_run):
            result = generate_speech(
                "hello there",
                voice_sherpa="vctk-vits",
                speaker_sherpa=42,
                speed=1.5,
                output_path=out,
            )

        assert result == out
        assert captured["text"] == "hello there"
        cmd = captured["cmd"]
        assert cmd[0] == str(fake_sherpa_layout["py"])
        assert "-m" in cmd and "claude_code_tts.sherpa_speak" in cmd
        assert "--model-dir" in cmd
        assert str(fake_sherpa_layout["models"] / "vctk-vits") in cmd
        assert "--speaker" in cmd and "42" in cmd
        assert "--speed" in cmd and "1.500" in cmd

    def test_sherpa_returns_none_if_venv_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(audio, "SHERPA_VENV_DIR", tmp_path / "no-venv")
        monkeypatch.setattr(audio, "SHERPA_MODELS_DIR", tmp_path / "models-noexist")

        with patch("claude_code_tts.audio.subprocess.run") as mock_run:
            result = generate_speech(
                "hi",
                voice_sherpa="vctk-vits",
                output_path=tmp_path / "out.wav",
            )
        assert result is None
        mock_run.assert_not_called()

    def test_sherpa_falls_through_to_piper_when_model_missing(
        self, tmp_path, fake_sherpa_layout
    ):
        """If voice_sherpa names a model that isn't installed, sherpa returns
        None and the rest of the routing chain still runs (Piper, etc.)."""
        piper_called = {"yes": False}

        def piper_which(name):
            return "/usr/bin/piper" if name == "piper" else None

        def fake_run(cmd, **kwargs):
            # generate_speech invokes piper as bare "piper" (PATH lookup at exec).
            if cmd[0] == "piper":
                piper_called["yes"] = True
                out_idx = cmd.index("--output_file") + 1
                Path(cmd[out_idx]).write_bytes(b"RIFF")
            return MagicMock(returncode=0, stdout="", stderr="")

        # Real piper voice file
        voice_path = tmp_path / "voice.onnx"
        voice_path.write_bytes(b"")

        with patch("claude_code_tts.audio.shutil.which", side_effect=piper_which), \
             patch("claude_code_tts.audio.subprocess.run", side_effect=fake_run):
            result = generate_speech(
                "hi",
                voice_sherpa="not-installed-model",
                voice_path=voice_path,
                output_path=tmp_path / "out.wav",
            )
        assert piper_called["yes"] is True
        assert result == tmp_path / "out.wav"


class TestSherpaConfigPlumbing:
    """voice_sherpa flows from persona dict → TTSConfig → generate_speech."""

    def test_voice_sherpa_loaded_from_persona(self, tmp_path, monkeypatch):
        from claude_code_tts.config import TTSConfig, load_config

        # Redirect HOME so we don't touch the real config.
        monkeypatch.setattr("claude_code_tts.config.HOME", tmp_path)
        monkeypatch.setattr("claude_code_tts.config.TTS_CONFIG_DIR", tmp_path / ".claude-tts")
        monkeypatch.setattr(
            "claude_code_tts.config.TTS_CONFIG_FILE",
            tmp_path / ".claude-tts" / "config.json",
        )
        monkeypatch.setattr(
            "claude_code_tts.config.TTS_SESSIONS_DIR",
            tmp_path / ".claude-tts" / "sessions.d",
        )

        import json
        cfg_dir = tmp_path / ".claude-tts"
        cfg_dir.mkdir()
        (cfg_dir / "config.json").write_text(json.dumps({
            "active_persona": "claude-newvoice",
            "personas": {
                "claude-newvoice": {
                    "voice_sherpa": "libritts-vits",
                    "speaker_sherpa": 137,
                    "speed": 1.0,
                },
            },
        }))

        cfg = load_config("test-session")
        assert cfg.active_persona == "claude-newvoice"
        assert cfg.voice_sherpa == "libritts-vits"
        assert cfg.speaker_sherpa == 137

    def test_default_voice_sherpa_is_empty(self):
        from claude_code_tts.config import TTSConfig
        c = TTSConfig()
        assert c.voice_sherpa == ""
        assert c.speaker_sherpa == -1


class TestSherpaQueueMessage:
    """Queue mode (write_queue_message) carries voice_sherpa fields."""

    def test_queue_message_includes_sherpa_fields(self, tmp_path, monkeypatch):
        from claude_code_tts.audio import write_queue_message
        from claude_code_tts.config import TTSConfig
        import json

        monkeypatch.setattr(audio, "TTS_QUEUE_DIR", tmp_path / "queue")

        cfg = TTSConfig(
            voice_sherpa="kokoro-multi",
            speaker_sherpa=3,
            session_id="test",
            project_name="test-project",
        )
        path = write_queue_message("hello", cfg)
        msg = json.loads(path.read_text())
        assert msg["voice_sherpa"] == "kokoro-multi"
        assert msg["speaker_sherpa"] == 3
