"""Voice signatures: a small, tolerant description of what a WAV sounds like.

A refactor of the speech path can pass every unit test and still sound wrong: quieter,
faster, clipped at the start, a pause dropped between sentences. A signature captures the
shape of a synthesized WAV in a few numbers so two of them can be compared with tolerances
instead of by ear: total length, speech and peak level, the energy envelope over time, the
zero-crossing profile (a cheap stand-in for spectral brightness), leading and trailing
silence, and the number of interior pauses.

Engines are not bit-exact from run to run (Piper samples noise for every utterance), so a
signature never compares samples. Tolerances default to what one engine's run-to-run spread
looks like, and `spread` measures that spread from several signatures of the same utterance
so a caller can widen them with evidence rather than guesses. Standard library only; a
12 second WAV signs in well under a second.
"""

from __future__ import annotations

import hashlib
import json
import math
import wave
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from .level import FULL_SCALE, _read, dbfs, measure_samples

BINS = 32
SILENCE_BELOW_SPEECH_DB = 25.0  # a window this far under the speech level is silence
PAUSE_MIN_S = 0.12
WINDOW_S = 0.02


@dataclass(frozen=True)
class Signature:
    """What a WAV sounds like, in numbers. `text_sha` and `voice` say what it was meant to be."""

    seconds: float
    sample_rate: int
    speech_dbfs: float
    peak_dbfs: float
    lead_silence_s: float
    trail_silence_s: float
    pauses: int
    envelope: list[float]  # BINS values, dBFS of RMS per equal slice of the file
    zcr: list[float]  # BINS values, zero crossings per second per slice, in kHz
    text_sha: str = ""
    voice: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=1)

    @classmethod
    def from_json(cls, text: str) -> Signature:
        data = json.loads(text)
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in names})


@dataclass(frozen=True)
class Tolerance:
    """How far two signatures may drift and still be the same voice saying the same thing."""

    seconds_rel: float = 0.05
    seconds_abs: float = 0.15
    speech_db: float = 1.0
    peak_db: float = 2.0
    silence_s: float = 0.12
    pauses: int = 1
    envelope_r: float = 0.85  # Pearson correlation floor
    zcr_r: float = 0.80
    zcr_rel: float = 0.15  # mean zero-crossing rate, relative: a pitch or brightness change


def text_sha(text: str) -> str:
    return hashlib.sha256(text.strip().encode()).hexdigest()[:16]


def _bins(values: list[float], n: int) -> list[float]:
    """Average `values` into n equal slices; pads by repeating when there are fewer values."""
    if not values:
        return [0.0] * n
    out = []
    for i in range(n):
        lo = int(i * len(values) / n)
        hi = max(lo + 1, int((i + 1) * len(values) / n))
        chunk = values[lo:hi]
        out.append(sum(chunk) / len(chunk))
    return out


def sign_samples(samples, rate: int, channels: int = 1, *, text: str = "", voice: str = "") -> Signature:
    """Signature of interleaved 16-bit samples."""
    level = measure_samples(samples, rate, channels)
    win = max(1, int(rate * WINDOW_S) * channels)
    n = len(samples)
    rms_db: list[float] = []
    zcr_khz: list[float] = []
    for i in range(0, n - win + 1, win):
        chunk = samples[i : i + win]
        rms_db.append(dbfs(math.sqrt(sum(s * s for s in chunk) / win) / FULL_SCALE))
        crossings = 0
        prev = chunk[0]
        for s in chunk:
            if (s >= 0) != (prev >= 0):
                crossings += 1
            prev = s
        zcr_khz.append(crossings / WINDOW_S / 1000.0)
    floor = level.speech_dbfs - SILENCE_BELOW_SPEECH_DB
    loud = [v > floor for v in rms_db]
    first = next((i for i, v in enumerate(loud) if v), len(loud))
    last = next((i for i in range(len(loud) - 1, -1, -1) if loud[i]), -1)
    pauses = 0
    run = 0
    min_run = int(PAUSE_MIN_S / WINDOW_S)
    for v in loud[first : last + 1]:
        if v:
            if run >= min_run:
                pauses += 1
            run = 0
        else:
            run += 1
    return Signature(
        seconds=round(level.seconds, 3),
        sample_rate=rate,
        speech_dbfs=round(level.speech_dbfs, 2),
        peak_dbfs=round(level.peak_dbfs, 2),
        lead_silence_s=round(first * WINDOW_S, 2),
        trail_silence_s=round(max(0, len(loud) - 1 - last) * WINDOW_S, 2),
        pauses=pauses,
        envelope=[round(v, 1) for v in _bins(rms_db, BINS)],
        zcr=[round(v, 3) for v in _bins(zcr_khz, BINS)],
        text_sha=text_sha(text) if text else "",
        voice=voice,
    )


def sign(path: Path, *, text: str = "", voice: str = "") -> Signature | None:
    """Signature of a 16-bit PCM WAV; None for other formats or an empty file."""
    try:
        read = _read(path)
    except (wave.Error, EOFError, OSError):
        return None
    if read is None:
        return None
    samples, params = read
    return sign_samples(samples, params.framerate, params.nchannels, text=text, voice=voice)


def _pearson(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or len(a) < 2:
        return 0.0
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b, strict=True))
    va = math.sqrt(sum((x - ma) ** 2 for x in a))
    vb = math.sqrt(sum((y - mb) ** 2 for y in b))
    if va == 0 or vb == 0:
        return 1.0 if va == vb else 0.0
    return cov / (va * vb)


def compare(expected: Signature, actual: Signature, tol: Tolerance | None = None) -> list[str]:
    """Every way `actual` drifts from `expected` beyond `tol`; empty means the same sound."""
    t = tol or Tolerance()
    out: list[str] = []
    if expected.text_sha and actual.text_sha and expected.text_sha != actual.text_sha:
        out.append(f"text differs: {expected.text_sha} vs {actual.text_sha}")
    if expected.voice and actual.voice and expected.voice != actual.voice:
        out.append(f"voice differs: {expected.voice} vs {actual.voice}")
    if expected.sample_rate != actual.sample_rate:
        out.append(f"sample rate {expected.sample_rate} vs {actual.sample_rate}")
    d = abs(expected.seconds - actual.seconds)
    if d > max(t.seconds_abs, expected.seconds * t.seconds_rel):
        out.append(f"length {expected.seconds}s vs {actual.seconds}s")
    if abs(expected.speech_dbfs - actual.speech_dbfs) > t.speech_db:
        out.append(f"speech level {expected.speech_dbfs} vs {actual.speech_dbfs} dBFS")
    if abs(expected.peak_dbfs - actual.peak_dbfs) > t.peak_db:
        out.append(f"peak {expected.peak_dbfs} vs {actual.peak_dbfs} dBFS")
    if abs(expected.lead_silence_s - actual.lead_silence_s) > t.silence_s:
        out.append(f"leading silence {expected.lead_silence_s}s vs {actual.lead_silence_s}s")
    if abs(expected.trail_silence_s - actual.trail_silence_s) > t.silence_s:
        out.append(f"trailing silence {expected.trail_silence_s}s vs {actual.trail_silence_s}s")
    if abs(expected.pauses - actual.pauses) > t.pauses:
        out.append(f"pauses {expected.pauses} vs {actual.pauses}")
    r = _pearson(expected.envelope, actual.envelope)
    if r < t.envelope_r:
        out.append(f"envelope correlation {r:.2f} < {t.envelope_r}")
    r = _pearson(expected.zcr, actual.zcr)
    if r < t.zcr_r:
        out.append(f"zero-crossing correlation {r:.2f} < {t.zcr_r}")
    ea, aa = _mean(expected.zcr), _mean(actual.zcr)
    if ea and abs(ea - aa) / ea > t.zcr_rel:
        out.append(f"brightness (mean zero-crossing rate) {ea:.2f} vs {aa:.2f} kHz")
    return out


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


@dataclass
class Spread:
    """Observed run-to-run spread of several signatures of one utterance."""

    seconds_rel: float = 0.0
    speech_db: float = 0.0
    peak_db: float = 0.0
    silence_s: float = 0.0
    pauses: int = 0
    envelope_r: float = 1.0  # the lowest pairwise correlation seen
    zcr_r: float = 1.0
    zcr_rel: float = 0.0
    runs: int = 0
    notes: list[str] = field(default_factory=list)

    def tolerance(self, margin: float = 2.0, base: Tolerance | None = None) -> Tolerance:
        """A Tolerance no tighter than `base` and at least `margin` times the spread seen."""
        b = base or Tolerance()
        return Tolerance(
            seconds_rel=max(b.seconds_rel, self.seconds_rel * margin),
            seconds_abs=b.seconds_abs,
            speech_db=max(b.speech_db, self.speech_db * margin),
            peak_db=max(b.peak_db, self.peak_db * margin),
            silence_s=max(b.silence_s, self.silence_s * margin),
            pauses=max(b.pauses, self.pauses),
            envelope_r=min(b.envelope_r, 1.0 - (1.0 - self.envelope_r) * margin),
            zcr_r=min(b.zcr_r, 1.0 - (1.0 - self.zcr_r) * margin),
            zcr_rel=max(b.zcr_rel, self.zcr_rel * margin),
        )


def spread(signatures: list[Signature]) -> Spread:
    """How much several signatures of the same utterance differ from one another."""
    s = Spread(runs=len(signatures))
    if len(signatures) < 2:
        return s
    secs = [x.seconds for x in signatures]
    s.seconds_rel = (max(secs) - min(secs)) / max(secs) if max(secs) else 0.0
    s.speech_db = max(x.speech_dbfs for x in signatures) - min(x.speech_dbfs for x in signatures)
    s.peak_db = max(x.peak_dbfs for x in signatures) - min(x.peak_dbfs for x in signatures)
    lead = [x.lead_silence_s for x in signatures]
    trail = [x.trail_silence_s for x in signatures]
    s.silence_s = max(max(lead) - min(lead), max(trail) - min(trail))
    s.pauses = max(x.pauses for x in signatures) - min(x.pauses for x in signatures)
    means = [_mean(x.zcr) for x in signatures]
    s.zcr_rel = (max(means) - min(means)) / max(means) if max(means) else 0.0
    for i, a in enumerate(signatures):
        for b in signatures[i + 1 :]:
            s.envelope_r = min(s.envelope_r, _pearson(a.envelope, b.envelope))
            s.zcr_r = min(s.zcr_r, _pearson(a.zcr, b.zcr))
    return s
