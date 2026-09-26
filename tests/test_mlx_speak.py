"""The pure parts of mlx_speak.py, the worker that runs inside the mlx venv.

mlx-audio is not importable outside that venv, so these tests cover what
does not need it: matching a request to a model's generate() signature,
concatenating chunks, and writing a valid 16-bit WAV.
"""

from __future__ import annotations

import wave
from pathlib import Path

import pytest

from claude_code_tts import mlx_speak


class TestGenerateKwargs:
    def test_kokoro_shape_takes_voice_speed_and_lang(self):
        def generate(text, voice=None, speed=1.0, lang_code="a", split_pattern=None):
            pass
        assert mlx_speak.generate_kwargs(generate, voice="af_heart", speed=1.3, lang_code="b") == {
            "voice": "af_heart", "speed": 1.3, "lang_code": "b",
        }

    def test_empty_voice_and_lang_leave_the_model_defaults(self):
        def generate(text, voice=None, speed=1.0, lang_code="a"):
            pass
        assert mlx_speak.generate_kwargs(generate, voice="", speed=1.0, lang_code="") == {"speed": 1.0}

    def test_model_without_speed_or_lang_gets_only_voice(self):
        def generate(text, voice="Vivian", instruct=None, temperature=0.7):
            pass
        assert mlx_speak.generate_kwargs(generate, voice="Ryan", speed=2.0, lang_code="a") == {"voice": "Ryan"}

    def test_var_kwargs_takes_everything_non_empty(self):
        def generate(text, **kwargs):
            pass
        assert mlx_speak.generate_kwargs(generate, voice="v", speed=1.5, lang_code="") == {"voice": "v", "speed": 1.5}

    def test_unsignaturable_callable_gets_nothing(self):
        assert mlx_speak.generate_kwargs(print, voice="v", speed=1.0, lang_code="a") in ({}, {"speed": 1.0})


class TestWriteWav:
    def test_writes_mono_16bit_and_clips(self, tmp_path):
        out = tmp_path / "nested" / "out.wav"
        n = mlx_speak.write_wav(out, [0.0, 0.5, -0.5, 2.0, -2.0], 24000)
        assert n == 5
        with wave.open(str(out)) as w:
            assert (w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()) == (1, 2, 24000, 5)
            frames = w.readframes(5)
        import struct
        values = struct.unpack("<5h", frames)
        assert values[0] == 0 and values[1] == 16383 and values[2] == -16383
        assert values[3] == 32767 and values[4] == -32767


class _Chunk:
    def __init__(self, audio, sample_rate=None):
        self.audio = audio
        self.sample_rate = sample_rate


class _Model:
    sample_rate = 24000

    def __init__(self, chunks):
        self.chunks = chunks
        self.calls: list[dict] = []

    def generate(self, text, voice=None, speed=1.0, lang_code="a"):
        self.calls.append({"text": text, "voice": voice, "speed": speed, "lang_code": lang_code})
        yield from self.chunks


class TestSynthesize:
    def test_concatenates_chunks_and_reports_seconds(self, tmp_path):
        model = _Model([_Chunk([[0.1, 0.2]], 24000), _Chunk([0.3] * 24000)])
        out = tmp_path / "s.wav"
        seconds = mlx_speak.synthesize(model, "hello", voice="af_heart", speed=1.0, lang_code="a", output=out)
        assert seconds == pytest.approx((2 + 24000) / 24000)
        with wave.open(str(out)) as w:
            assert w.getnframes() == 24002
        assert model.calls == [{"text": "hello", "voice": "af_heart", "speed": 1.0, "lang_code": "a"}]

    def test_no_samples_is_an_error(self, tmp_path):
        with pytest.raises(RuntimeError, match="no samples"):
            mlx_speak.synthesize(_Model([]), "hi", voice="", speed=1.0, lang_code="", output=tmp_path / "x.wav")

    def test_chunk_sample_rate_wins_over_model_default(self, tmp_path):
        model = _Model([_Chunk([0.0, 0.0], 16000)])
        mlx_speak.synthesize(model, "hi", voice="", speed=1.0, lang_code="", output=tmp_path / "r.wav")
        with wave.open(str(tmp_path / "r.wav")) as w:
            assert w.getframerate() == 16000


class TestMain:
    def test_single_shot_requires_output(self, capsys):
        assert mlx_speak.main(["--model", "m"]) == 1
        assert "--output is required" in capsys.readouterr().err

    def test_single_shot_rejects_empty_text(self, tmp_path, capsys):
        assert mlx_speak.main(["--model", "m", "--output", str(tmp_path / "o.wav"), "--text", "  "]) == 3

    def test_single_shot_reports_load_failure(self, tmp_path, monkeypatch, capsys):
        def boom(model_id):
            raise ValueError("no such repo")
        monkeypatch.setattr(mlx_speak, "_load", boom)
        assert mlx_speak.main(["--model", "m", "--output", str(tmp_path / "o.wav"), "--text", "hi"]) == 4
        assert "could not load m: ValueError: no such repo" in capsys.readouterr().err

    def test_single_shot_writes_wav(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(mlx_speak, "_load", lambda model_id: _Model([_Chunk([0.1] * 2400)]))
        out = tmp_path / "o.wav"
        assert mlx_speak.main(["--model", "m", "--output", str(out), "--text", "hi", "--voice", "af_heart"]) == 0
        assert out.exists()
        assert "wrote 0.1s" in capsys.readouterr().err

    def test_serve_mode_answers_requests(self, tmp_path, monkeypatch, capsys):
        import io
        model = _Model([_Chunk([0.1, 0.2])])
        monkeypatch.setattr(mlx_speak, "_load", lambda model_id: model)
        out = tmp_path / "s.wav"
        requests = [
            {"text": "hello", "output": str(out), "voice": "bm_george", "speed": 1.2, "lang_code": "b"},
            {"text": "", "output": str(out)},
            {"text": "x", "output": ""},
            "not json",
        ]
        stdin = "\n".join(r if isinstance(r, str) else __import__("json").dumps(r) for r in requests) + "\n"
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
        assert mlx_speak.main(["--model", "m", "--serve"]) == 0
        lines = [__import__("json").loads(line) for line in capsys.readouterr().out.strip().splitlines()]
        assert lines[0] == {"ready": True, "model": "m", "sample_rate": 24000}
        assert lines[1]["ok"] is True
        assert lines[2] == {"ok": False, "error": "empty text"}
        assert lines[3] == {"ok": False, "error": "no output path"}
        assert lines[4]["ok"] is False and "bad json" in lines[4]["error"]
        assert model.calls[0]["voice"] == "bm_george" and model.calls[0]["lang_code"] == "b"
        assert Path(out).exists()

    def test_serve_mode_reports_load_failure(self, monkeypatch, capsys):
        def boom(model_id):
            raise OSError("offline")
        monkeypatch.setattr(mlx_speak, "_load", boom)
        assert mlx_speak.main(["--model", "m", "--serve"]) == 4
        assert capsys.readouterr().out.strip() == '{"ready": false, "error": "OSError: offline"}'
