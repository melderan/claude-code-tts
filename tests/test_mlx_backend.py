"""Tests for the mlx-audio backend (additive fourth TTS engine, Apple silicon).

Same two principles as the sherpa backend:
  1. mlx is OPT-IN. Personas without voice_mlx never reach it.
  2. With voice_mlx set, generate_speech asks the persistent worker for the
     model, voice and language the persona named, and applies speed the way
     Piper does: in the audio only for speed_method length_scale.

mlx-audio itself is never imported here (it exists only in the venv on a
Mac). The JSON-line worker protocol is exercised for real against a tiny
stand-in script, so the parent side is proven without MLX.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from claude_code_tts import audio
from claude_code_tts.audio import generate_speech


@pytest.fixture
def fake_mlx_venv(tmp_path, monkeypatch):
    venv = tmp_path / "venvs" / "mlx"
    (venv / "bin").mkdir(parents=True)
    py = venv / "bin" / "python"
    py.write_text("#!/bin/sh\nexit 0\n")
    py.chmod(0o755)
    monkeypatch.setattr(audio, "MLX_VENV_DIR", venv)
    monkeypatch.setattr(audio, "_mlx_workers", {})
    return venv


class TestMlxIsOptIn:
    def test_empty_voice_mlx_skips_the_branch(self, tmp_path, fake_mlx_venv):
        with patch("claude_code_tts.audio._generate_mlx") as mock_mlx, \
             patch("claude_code_tts.audio.shutil.which", return_value=None):
            generate_speech("hello world", voice_mlx="", output_path=tmp_path / "out.wav")
        mock_mlx.assert_not_called()

    def test_sherpa_persona_does_not_reach_mlx(self, tmp_path, fake_mlx_venv):
        with patch("claude_code_tts.audio._generate_mlx") as mock_mlx, \
             patch("claude_code_tts.audio._generate_sherpa", return_value=None), \
             patch("claude_code_tts.audio.shutil.which", return_value=None):
            generate_speech("hello", voice_sherpa="vctk-vits", output_path=tmp_path / "out.wav")
        mock_mlx.assert_not_called()


class TestMlxRouting:
    def _fake_worker(self, calls):
        class FakeWorker:
            def generate(self, text, *, voice, speed, lang_code, output_path):
                calls.append({"text": text, "voice": voice, "speed": speed, "lang_code": lang_code})
                Path(output_path).write_bytes(b"RIFF")
                return True
        return FakeWorker()

    def test_worker_gets_model_voice_and_lang(self, tmp_path, fake_mlx_venv):
        calls: list[dict] = []
        with patch("claude_code_tts.audio._get_mlx_worker", return_value=self._fake_worker(calls)) as get:
            out = generate_speech(
                "hello there", voice_mlx="mlx-community/Kokoro-82M-bf16", speaker_mlx="af_heart",
                lang_mlx="b", speed=2.0, speed_method="playback", output_path=tmp_path / "out.wav",
            )
        assert out == tmp_path / "out.wav"
        get.assert_called_once_with("mlx-community/Kokoro-82M-bf16")
        assert calls == [{"text": "hello there", "voice": "af_heart", "speed": 1.0, "lang_code": "b"}]

    def test_speed_is_synthesised_only_for_length_scale(self, tmp_path, fake_mlx_venv):
        calls: list[dict] = []
        with patch("claude_code_tts.audio._get_mlx_worker", return_value=self._fake_worker(calls)):
            generate_speech("hi", voice_mlx="m", speed=1.7, speed_method="length_scale", output_path=tmp_path / "o.wav")
        assert calls[0]["speed"] == 1.7

    def test_missing_venv_falls_through_to_piper(self, tmp_path, monkeypatch):
        monkeypatch.setattr(audio, "MLX_VENV_DIR", tmp_path / "nowhere")
        monkeypatch.setattr(audio, "_mlx_workers", {})
        voice = tmp_path / "en_US-hfc_male-medium.onnx"
        voice.write_bytes(b"model")
        ran: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            ran.append(cmd)
            Path(cmd[cmd.index("--output_file") + 1]).write_bytes(b"RIFF")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("claude_code_tts.audio.shutil.which", side_effect=lambda n: "/usr/bin/piper" if n == "piper" else None), \
             patch("claude_code_tts.audio.subprocess.run", side_effect=fake_run):
            out = generate_speech("hi", voice_mlx="m", voice_path=voice, output_path=tmp_path / "o.wav")
        assert out == tmp_path / "o.wav"
        assert ran and ran[0][0] == "piper"

    def test_worker_command_names_the_module_and_model(self, fake_mlx_venv):
        cmd = audio._MlxWorker("mlx-community/Kokoro-82M-bf16")._command()
        assert cmd is not None
        assert cmd[0] == str(fake_mlx_venv / "bin" / "python")
        assert cmd[1:] == ["-m", "claude_code_tts.mlx_speak", "--serve", "--model", "mlx-community/Kokoro-82M-bf16"]

    def test_worker_command_is_none_without_venv(self, tmp_path, monkeypatch):
        monkeypatch.setattr(audio, "MLX_VENV_DIR", tmp_path / "nowhere")
        assert audio._MlxWorker("m")._command() is None

    def test_warm_starts_one_worker_per_model(self, fake_mlx_venv):
        started: list[str] = []

        class FakeWorker:
            def __init__(self, model_id):
                self.model_id = model_id

            def ensure_started(self):
                started.append(self.model_id)
                return self.model_id != "broken"

        with patch("claude_code_tts.audio._get_mlx_worker", side_effect=FakeWorker):
            ready = audio.warm_mlx_workers({
                "a": {"voice_mlx": "kokoro"}, "b": {"voice_mlx": "kokoro"},
                "c": {"voice_mlx": "broken"}, "d": {"voice": "piper-only"},
            })
        assert started == ["kokoro", "broken"]
        assert ready == ["kokoro"]


STAND_IN = '''
import json, sys
args = sys.argv[1:]
if "--fail-load" in args:
    print(json.dumps({"ready": False, "error": "no such model"}), flush=True); sys.exit(4)
print(json.dumps({"ready": True, "model": args[args.index("--model") + 1]}), flush=True)
for line in sys.stdin:
    req = json.loads(line)
    if req.get("text") == "explode":
        print(json.dumps({"ok": False, "error": "boom"}), flush=True)
        continue
    open(req["output"], "wb").write(b"RIFF")
    print(json.dumps({"ok": True, "echo": req}), flush=True)
'''


class TestJsonLineWorkerProtocol:
    """The parent side of the worker protocol, against a stand-in child."""

    def _worker(self, tmp_path, *extra):
        script = tmp_path / "stand_in.py"
        script.write_text(STAND_IN)

        class Worker(audio._JsonLineWorker):
            label = "stand-in worker"
            ready_timeout = 10.0

            def _command(self):
                return [sys.executable, str(script), "--model", "demo", *extra]

        return Worker()

    def test_request_round_trip_and_restart_after_death(self, tmp_path):
        w = self._worker(tmp_path)
        out = tmp_path / "a.wav"
        resp = w.request({"text": "hello", "output": str(out)})
        assert resp and resp["ok"] and resp["echo"]["text"] == "hello"
        assert out.read_bytes() == b"RIFF"
        assert w.request({"text": "explode", "output": str(out)}) == {"ok": False, "error": "boom"}
        w._proc.kill()
        w._proc.wait()
        resp = w.request({"text": "again", "output": str(out)})
        assert resp and resp["ok"]
        w._proc.terminate()

    def test_failed_load_is_reported_once_and_returns_none(self, tmp_path):
        w = self._worker(tmp_path, "--fail-load")
        with patch("claude_code_tts.audio.debug") as dbg:
            assert w.request({"text": "x", "output": str(tmp_path / "x.wav")}) is None
        assert any("failed to start: no such model" in str(c) for c in dbg.call_args_list)

    def test_mlx_worker_generate_uses_the_protocol(self, tmp_path, fake_mlx_venv):
        script = tmp_path / "stand_in.py"
        script.write_text(STAND_IN)
        w = audio._MlxWorker("demo")
        w._command = lambda: [sys.executable, str(script), "--model", "demo"]  # type: ignore[method-assign]
        out = tmp_path / "m.wav"
        assert w.generate("hi", voice="af_heart", speed=1.0, lang_code="a", output_path=out) is True
        assert out.exists()
        w._proc.terminate()


class TestMlxConfigPlumbing:
    def test_voice_mlx_loaded_from_persona(self, tmp_path, monkeypatch):
        from claude_code_tts.config import load_config
        monkeypatch.setattr("claude_code_tts.config.HOME", tmp_path)
        monkeypatch.setattr("claude_code_tts.config.TTS_CONFIG_DIR", tmp_path / ".claude-tts")
        monkeypatch.setattr("claude_code_tts.config.TTS_CONFIG_FILE", tmp_path / ".claude-tts" / "config.json")
        monkeypatch.setattr("claude_code_tts.config.TTS_SESSIONS_DIR", tmp_path / ".claude-tts" / "sessions.d")
        cfg_dir = tmp_path / ".claude-tts"
        cfg_dir.mkdir()
        (cfg_dir / "config.json").write_text(json.dumps({
            "active_persona": "k",
            "personas": {"k": {"voice_mlx": "mlx-community/Kokoro-82M-bf16", "speaker_mlx": "bm_george", "lang_mlx": "b"}},
        }))
        cfg = load_config("test-session")
        assert (cfg.voice_mlx, cfg.speaker_mlx, cfg.lang_mlx) == ("mlx-community/Kokoro-82M-bf16", "bm_george", "b")

    def test_defaults_are_empty(self):
        from claude_code_tts.config import TTSConfig
        c = TTSConfig()
        assert (c.voice_mlx, c.speaker_mlx, c.lang_mlx) == ("", "", "")

    def test_queue_message_carries_mlx_fields(self, tmp_path, monkeypatch):
        from claude_code_tts.config import TTSConfig
        monkeypatch.setattr(audio, "TTS_QUEUE_DIR", tmp_path / "queue")
        cfg = TTSConfig(voice_mlx="m", speaker_mlx="v", lang_mlx="a", session_id="s", project_name="p")
        msg = json.loads(audio.write_queue_message("hello", cfg).read_text())
        assert (msg["voice_mlx"], msg["speaker_mlx"], msg["lang_mlx"]) == ("m", "v", "a")
