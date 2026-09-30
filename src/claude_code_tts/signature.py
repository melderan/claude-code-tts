"""Voice signatures: a small, tolerant description of what a WAV sounds like.

A refactor of the speech path can pass every unit test and still sound wrong: quieter,
faster, clipped at the start, a pause dropped between sentences. A signature captures the
shape of a synthesized WAV in a few numbers so two of them can be compared with tolerances
instead of by ear: total length, speech and peak level, the energy envelope over time, the
zero-crossing profile (a cheap stand-in for spectral brightness), leading and trailing
silence, and the number of interior pauses.

Engines are not bit-exact from run to run (Piper samples noise for every utterance and its
word and pause lengths jitter by up to a tenth), so a signature never compares samples, and
its shape measures are built to survive timing jitter: the envelope is binned over the speech
span (leading and trailing silence are separate numbers), compared by dynamic time warping
(mean dB deviation along the best alignment) and by the distribution of its values. Measured
on three-run spreads of real Piper output, positional correlation of the same sentence fell to
0.24; the warp distance stayed near 2 dB while a 30 percent truncation read 3.4 dB. Tolerances
default to what one engine's run-to-run spread looks like, and `spread` measures that spread
from several signatures of the same utterance so a caller can widen them with evidence rather
than guesses. Standard library only; a 12 second WAV signs in well under a second.
"""

from __future__ import annotations

import hashlib
import json
import math
import wave
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from .level import FULL_SCALE, _read, dbfs, measure_samples

VERSION = 2  # bump when the shape measures change; baselines must then be recaptured
BINS = 32
DIST_BINS = 16
SILENCE_BELOW_SPEECH_DB = 25.0  # a window this far under the speech level is silence
SILENCE_FLOOR_DB = -60.0  # digital silence is clamped here so it cannot dominate a distance
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
    envelope: list[float]  # BINS values, dBFS of RMS per equal slice of the speech span
    zcr: list[float]  # BINS values, zero crossings per second per slice of the span, in kHz
    text_sha: str = ""
    voice: str = ""
    version: int = VERSION

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
    envelope_dtw_db: float = 3.0  # mean dB deviation along the best time alignment
    envelope_dist_db: float = 1.5  # mean dB difference between the two envelopes' percentiles
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
    span = slice(first, last + 1) if last >= first else slice(0, len(rms_db))
    span_rms = [max(v, SILENCE_FLOOR_DB) for v in rms_db[span]]
    span_zcr = zcr_khz[span]
    return Signature(
        seconds=round(level.seconds, 3),
        sample_rate=rate,
        speech_dbfs=round(level.speech_dbfs, 2),
        peak_dbfs=round(level.peak_dbfs, 2),
        lead_silence_s=round(first * WINDOW_S, 2),
        trail_silence_s=round(max(0, len(loud) - 1 - last) * WINDOW_S, 2),
        pauses=pauses,
        envelope=[round(v, 1) for v in _bins(span_rms, BINS)],
        zcr=[round(v, 3) for v in _bins(span_zcr, BINS)],
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


def compare(expected: Signature, actual: Signature, tol: Tolerance | None = None) -> list[str]:
    """Every way `actual` drifts from `expected` beyond `tol`; empty means the same sound."""
    t = tol or Tolerance()
    out: list[str] = []
    if expected.version != actual.version:
        return [f"signature version {expected.version} vs {actual.version}: recapture the baseline"]
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
    d = envelope_dtw_db(expected.envelope, actual.envelope)
    if d > t.envelope_dtw_db:
        out.append(f"envelope shape {d:.2f} dB from expected along the best alignment > {t.envelope_dtw_db}")
    d = envelope_dist_db(expected.envelope, actual.envelope)
    if d > t.envelope_dist_db:
        out.append(f"envelope distribution {d:.2f} dB from expected > {t.envelope_dist_db}")
    ea, aa = _mean(expected.zcr), _mean(actual.zcr)
    if ea and abs(ea - aa) / ea > t.zcr_rel:
        out.append(f"brightness (mean zero-crossing rate) {ea:.2f} vs {aa:.2f} kHz")
    return out


def envelope_dtw_db(a: list[float], b: list[float]) -> float:
    """Mean absolute dB deviation between two envelopes along their best time alignment.

    Dynamic time warping: a word that came out a little longer or a pause a little later is
    matched to its counterpart instead of to whatever now sits at the same bin.
    """
    n, m = len(a), len(b)
    if not n or not m:
        return 0.0 if n == m else 99.0
    inf = float("inf")
    prev = [0.0] + [inf] * m
    for i in range(1, n + 1):
        cur = [inf] * (m + 1)
        for j in range(1, m + 1):
            cost = abs(a[i - 1] - b[j - 1])
            cur[j] = cost + min(prev[j], cur[j - 1], prev[j - 1])
        prev = cur
    return prev[m] / (n + m)


def envelope_dist_db(a: list[float], b: list[float]) -> float:
    """Mean absolute dB difference between the two envelopes' value distributions (percentiles)."""
    pa, pb = _bins(sorted(a), DIST_BINS), _bins(sorted(b), DIST_BINS)
    return sum(abs(x - y) for x, y in zip(pa, pb, strict=True)) / DIST_BINS


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
    envelope_dtw_db: float = 0.0  # the largest pairwise warp distance seen
    envelope_dist_db: float = 0.0
    zcr_rel: float = 0.0
    runs: int = 0
    notes: list[str] = field(default_factory=list)
    version: int = VERSION

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=1)

    @classmethod
    def from_json(cls, text: str) -> Spread | None:
        """A Spread from its JSON, or None when it was measured by another signature version
        (its fields would not mean the same thing; measure again)."""
        data = json.loads(text)
        if data.get("version") != VERSION:
            return None
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in names})

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
            envelope_dtw_db=max(b.envelope_dtw_db, self.envelope_dtw_db * margin),
            envelope_dist_db=max(b.envelope_dist_db, self.envelope_dist_db * margin),
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
            s.envelope_dtw_db = max(s.envelope_dtw_db, envelope_dtw_db(a.envelope, b.envelope))
            s.envelope_dist_db = max(s.envelope_dist_db, envelope_dist_db(a.envelope, b.envelope))
    return s
