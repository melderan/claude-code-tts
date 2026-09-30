"""Loudness of synthesized speech, measured and evened out across backends.

Each engine writes WAVs at its own level: on one machine Piper peaked at full scale
with speech around -13 dBFS while Kokoro on mlx peaked near -8 dBFS with speech
around -21, a 7 dB gap the ear hears as one voice at half the loudness of the other.
The daemon plays WAVs as synthesized, so nothing evened that out.

`normalize` rewrites a 16-bit PCM WAV in place so that its speech level sits at a
target, with the gain capped so the peak stays under a ceiling. Speech level is the
mean RMS of the loudest fifth of 50 ms windows: it ignores the silence between
sentences that drags a whole-file RMS down, and it does not chase one click the way
a peak measure would. Standard library only: `wave` and `array`; the gain goes
through a lookup table so a minute of audio takes well under a second.
"""

from __future__ import annotations

import array
import math
import wave
from dataclasses import dataclass
from pathlib import Path

WINDOW_S = 0.05
LOUD_FRACTION = 0.2
FULL_SCALE = 32768.0
MIN_GAIN_DB = 0.1  # below this the rewrite is not worth the disk write


@dataclass(frozen=True)
class Level:
    peak_dbfs: float
    speech_dbfs: float
    seconds: float
    sample_rate: int


def dbfs(linear: float) -> float:
    return 20.0 * math.log10(linear) if linear > 0 else -120.0


def measure_samples(samples: array.array, rate: int, channels: int = 1) -> Level:
    """Peak and speech level of interleaved 16-bit samples."""
    n = len(samples)
    if n == 0:
        return Level(-120.0, -120.0, 0.0, rate)
    peak = max(abs(s) for s in samples) / FULL_SCALE
    win = max(1, int(rate * WINDOW_S) * channels)
    rms_windows: list[float] = []
    for i in range(0, n - win + 1, win):
        chunk = samples[i : i + win]
        rms_windows.append(math.sqrt(sum(s * s for s in chunk) / win) / FULL_SCALE)
    if len(rms_windows) < 5:
        speech = math.sqrt(sum(s * s for s in samples) / n) / FULL_SCALE
    else:
        rms_windows.sort()
        loud = rms_windows[int(len(rms_windows) * (1 - LOUD_FRACTION)) :]
        speech = sum(loud) / len(loud)
    return Level(dbfs(peak), dbfs(speech), n / channels / rate, rate)


def _read(path: Path) -> tuple[array.array, wave._wave_params] | None:
    with wave.open(str(path), "rb") as w:
        params = w.getparams()
        if params.sampwidth != 2 or params.nframes == 0:
            return None
        samples = array.array("h")
        samples.frombytes(w.readframes(params.nframes))
    return samples, params


def measure(path: Path) -> Level | None:
    """Level of a 16-bit PCM WAV; None for other formats or an empty file."""
    read = _read(path)
    if read is None:
        return None
    samples, params = read
    return measure_samples(samples, params.framerate, params.nchannels)


def apply_gain(samples: array.array, gain_db: float) -> array.array:
    """Scale 16-bit samples by gain_db with clipping, through a 65536-entry table."""
    g = 10 ** (gain_db / 20.0)
    table = [0] * 65536
    for i in range(65536):
        v = i - 65536 if i >= 32768 else i
        table[i] = max(-32768, min(32767, round(v * g)))
    return array.array("h", map(table.__getitem__, map((0xFFFF).__and__, samples)))


def normalize(
    path: Path, target_dbfs: float, *, peak_dbfs: float = -1.0, extra_db: float = 0.0
) -> tuple[Level, float] | None:
    """Bring the WAV's speech level to target_dbfs (+ extra_db), peak capped at peak_dbfs.

    Rewrites the file in place. Returns (level before, gain applied in dB), or None
    when the file is not 16-bit PCM or is empty. A gain under MIN_GAIN_DB is reported
    as 0.0 and the file is left alone.
    """
    read = _read(path)
    if read is None:
        return None
    samples, params = read
    before = measure_samples(samples, params.framerate, params.nchannels)
    gain = target_dbfs + extra_db - before.speech_dbfs
    gain = min(gain, peak_dbfs - before.peak_dbfs)  # never push the peak over the ceiling
    if abs(gain) < MIN_GAIN_DB:
        return before, 0.0
    out = apply_gain(samples, gain)
    tmp = path.with_suffix(path.suffix + ".lvl")
    with wave.open(str(tmp), "wb") as w:
        w.setparams(params)
        w.writeframes(out.tobytes())
    tmp.replace(path)
    return before, gain
