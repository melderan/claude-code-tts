"""level: speech level measured on the loud windows, gain capped by the peak, file rewritten in place."""

from __future__ import annotations

import array
import math
import wave
from pathlib import Path
from unittest.mock import patch

import claude_code_tts.daemon as d
from claude_code_tts.level import Level, apply_gain, measure, normalize


def tone(
    path: Path, amplitude: float, seconds: float = 1.0, rate: int = 22050, gaps: bool = True
) -> Path:
    """A 440 Hz tone at `amplitude` of full scale, with silent gaps like speech pauses."""
    n = int(rate * seconds)
    s = array.array("h")
    for i in range(n):
        silent = gaps and (i // (rate // 4)) % 2 == 1  # quarter-second on, quarter-second off
        v = 0.0 if silent else amplitude * math.sin(2 * math.pi * 440 * i / rate)
        s.append(int(round(v * 32767)))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(s.tobytes())
    return path


def test_measure_reads_speech_from_the_loud_windows_not_the_silence(tmp_path: Path) -> None:
    lvl = measure(tone(tmp_path / "a.wav", 0.5))
    assert lvl is not None
    assert abs(lvl.peak_dbfs - (-6.0)) < 0.2
    # A full-scale sine has RMS -3 dBFS; at 0.5 that is -9; the gaps must not pull it down.
    assert abs(lvl.speech_dbfs - (-9.0)) < 0.3
    assert abs(lvl.seconds - 1.0) < 0.01


def test_normalize_raises_quiet_and_lowers_loud_to_the_same_level(tmp_path: Path) -> None:
    quiet = tone(tmp_path / "q.wav", 0.1)
    loud = tone(tmp_path / "l.wav", 0.9)
    rq = normalize(quiet, -16.0)
    rl = normalize(loud, -16.0)
    assert rq and rl and rq[1] > 0 > rl[1]
    for p in (quiet, loud):
        after = measure(p)
        assert after is not None and abs(after.speech_dbfs - (-16.0)) < 0.3, p


def test_gain_is_capped_by_the_peak_ceiling(tmp_path: Path) -> None:
    # Peaky file: speech is quiet but one sample is near full scale.
    p = tone(tmp_path / "p.wav", 0.05)
    with wave.open(str(p), "rb") as w:
        params, frames = w.getparams(), w.readframes(w.getnframes())
    s = array.array("h")
    s.frombytes(frames)
    s[100] = 30000
    with wave.open(str(p), "wb") as w:
        w.setparams(params)
        w.writeframes(s.tobytes())
    result = normalize(p, -16.0, peak_dbfs=-1.0)
    assert result is not None
    after = measure(p)
    assert after is not None and after.peak_dbfs <= -0.9
    assert after.speech_dbfs < -16.0, "target not reached because the ceiling won"


def test_small_gain_leaves_the_file_alone_and_extra_db_shifts_the_target(tmp_path: Path) -> None:
    p = tone(tmp_path / "s.wav", 0.5)  # speech -9 dBFS
    before = p.read_bytes()
    result = normalize(p, -9.0)
    assert result is not None and result[1] == 0.0 and p.read_bytes() == before
    result = normalize(p, -16.0, extra_db=4.0)  # persona wants to be 4 dB above the house target
    after = measure(p)
    assert result and after and abs(after.speech_dbfs - (-12.0)) < 0.3


def test_non_16_bit_and_empty_files_are_skipped(tmp_path: Path) -> None:
    p = tmp_path / "w8.wav"
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(1)
        w.setframerate(8000)
        w.writeframes(b"\x80" * 800)
    assert normalize(p, -16.0) is None
    e = tmp_path / "e.wav"
    with wave.open(str(e), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
    assert measure(e) is None


def test_apply_gain_clips_instead_of_wrapping() -> None:
    out = apply_gain(array.array("h", [30000, -30000, 100]), 6.0)
    assert list(out) == [32767, -32768, 200]


def test_daemon_target_and_persona_gain_come_from_config() -> None:
    with patch.object(
        d,
        "load_raw_config",
        lambda: {"queue": {"normalize_dbfs": -18}, "personas": {"p": {"gain_db": 2}}},
    ):
        assert d.normalize_target() == -18.0
        assert d.persona_gain_db("p") == 2.0 and d.persona_gain_db("other") == 0.0
    with patch.object(d, "load_raw_config", lambda: {"queue": {"normalize_dbfs": None}}):
        assert d.normalize_target() is None
    with patch.object(d, "load_raw_config", lambda: {}):
        assert d.normalize_target() == d.DEFAULT_NORMALIZE_DBFS


def test_level_after_synthesis_logs_one_line_and_never_raises(tmp_path: Path) -> None:
    lines: list[str] = []
    p = tone(tmp_path / "m.wav", 0.1)
    with (
        patch.object(d, "load_raw_config", lambda: {}),
        patch.object(d, "log", lambda msg, *a, **k: lines.append(msg)),
    ):
        d.level_after_synthesis(p, "claude-prime")
        d.level_after_synthesis(tmp_path / "missing.wav", "claude-prime")  # engine wrote nothing: not ours
        (tmp_path / "bad.wav").write_bytes(b"RIFFnot a wav at all")
        d.level_after_synthesis(tmp_path / "bad.wav", "claude-prime")
    assert len(lines) == 2
    assert lines[0].startswith("Level [claude-prime]: speech") and "gain +" in lines[0]
    assert lines[1].startswith("Level [claude-prime]: skipped")
    assert isinstance(measure(p), Level)
