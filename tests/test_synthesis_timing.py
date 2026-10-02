"""The daemon log says how long each synthesis took next to how much speech it made (9.41.4)."""

from __future__ import annotations

import struct
import wave
from pathlib import Path

import claude_code_tts.daemon as d


def _write_wav(path: Path, seconds: float, rate: int = 22050) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(struct.pack("<h", 0) * int(seconds * rate))


def _capture(monkeypatch) -> list[str]:
    lines: list[str] = []
    monkeypatch.setattr(d, "log", lambda msg, *a, **k: lines.append(msg))
    return lines


def test_a_synthesized_message_logs_speech_seconds_wall_seconds_and_the_ratio(tmp_path, monkeypatch):
    lines = _capture(monkeypatch)

    def gen_factory(persona, **_):
        def gen(text, out):
            _write_wav(out, 3.0)
            return True

        return gen

    monkeypatch.setattr(d, "sentence_generator", gen_factory)
    ok, marks = d.synthesize_message(
        "three seconds", "claude-prime", tmp_path / "m.wav", want_marks=False, speed=1.0, speed_method="playback"
    )
    assert ok and marks is None
    timing = [ln for ln in lines if ln.startswith("Synthesized ")]
    assert len(timing) == 1
    assert timing[0].startswith("Synthesized 3.0s of speech in ")
    assert "for claude-prime (" in timing[0] and "x real time)" in timing[0]


def test_a_failed_synthesis_logs_no_timing_line(tmp_path, monkeypatch):
    lines = _capture(monkeypatch)
    monkeypatch.setattr(d, "sentence_generator", lambda persona, **_: (lambda text, out: False))
    ok, marks = d.synthesize_message(
        "nothing", "claude-prime", tmp_path / "m.wav", want_marks=False, speed=1.0, speed_method="playback"
    )
    assert (ok, marks) == (False, None)
    assert not [ln for ln in lines if ln.startswith("Synthesized ")]


def test_an_empty_file_logs_no_timing_line(tmp_path, monkeypatch):
    lines = _capture(monkeypatch)
    d.log_synthesis_time("claude-prime", tmp_path / "missing.wav", 0.5)
    (tmp_path / "empty.wav").write_bytes(b"")
    d.log_synthesis_time("claude-prime", tmp_path / "empty.wav", 0.5)
    assert lines == []
