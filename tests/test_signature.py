"""Voice signatures: two WAVs that sound the same compare equal; a planted change does not.

The synthetic voice here is a run of tone bursts with silences between them, one burst per
"word", so length, level, envelope and pauses are all under the test's control.
"""

from __future__ import annotations

import array
import math
import wave
from pathlib import Path

import pytest

from claude_code_tts.signature import Signature, Tolerance, compare, sign, sign_samples, spread

RATE = 22050


def voice(words: int = 6, *, gain: float = 0.3, word_s: float = 0.25, gap_s: float = 0.08,
          pitch: float = 180.0, lead_s: float = 0.1, seed: int = 0) -> array.array:
    """Deterministic pseudo-speech: `words` bursts of a buzzy tone separated by gaps."""
    out = array.array("h")
    out.extend([0] * int(lead_s * RATE))
    rnd = seed
    for w in range(words):
        n = int(word_s * RATE)
        f = pitch * (1 + 0.15 * math.sin(w))
        for i in range(n):
            rnd = (rnd * 1103515245 + 12345) & 0x7FFFFFFF  # tiny noise so runs differ slightly
            noise = ((rnd >> 16) / 32768.0 - 0.5) * 0.02
            env = math.sin(math.pi * i / n)  # rise and fall inside the word
            s = env * (math.sin(2 * math.pi * f * i / RATE) + 0.4 * math.sin(4 * math.pi * f * i / RATE)) + noise
            out.append(int(max(-1.0, min(1.0, s * gain)) * 32767))
        out.extend([0] * int(gap_s * RATE))
    out.extend([0] * int(0.15 * RATE))
    return out


def write(path: Path, samples: array.array) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(samples.tobytes())
    return path


class TestSameVoiceSameText:
    def test_identical_audio_has_no_differences(self):
        a = sign_samples(voice(), RATE)
        assert compare(a, a) == []

    def test_run_to_run_noise_is_inside_tolerance(self):
        a = sign_samples(voice(seed=1), RATE)
        b = sign_samples(voice(seed=2), RATE)
        assert compare(a, b) == []

    def test_spread_of_repeated_runs_is_small_and_widens_tolerance_with_evidence(self):
        sigs = [sign_samples(voice(seed=s), RATE) for s in range(4)]
        sp = spread(sigs)
        assert sp.runs == 4
        assert sp.speech_db < 0.2 and sp.seconds_rel == 0.0
        tol = sp.tolerance(margin=3.0)
        assert tol.speech_db == Tolerance().speech_db  # never tighter than the base


class TestPlantedChanges:
    """Each change is one plausible regression in the speech path; each must be caught."""

    def test_quieter_by_3db(self):
        a = sign_samples(voice(gain=0.3), RATE)
        b = sign_samples(voice(gain=0.3 / math.sqrt(2)), RATE)
        diffs = compare(a, b)
        assert any(d.startswith("speech level") for d in diffs), diffs

    def test_faster_speech(self):
        a = sign_samples(voice(word_s=0.25), RATE)
        b = sign_samples(voice(word_s=0.20), RATE)
        diffs = compare(a, b)
        assert any(d.startswith("length") for d in diffs), diffs

    def test_dropped_pause_between_words(self):
        a = sign_samples(voice(gap_s=0.2), RATE)
        b = sign_samples(voice(gap_s=0.02), RATE)
        diffs = compare(a, b)
        assert any(d.startswith("pauses") for d in diffs), diffs

    def test_clipped_start(self):
        a = sign_samples(voice(lead_s=0.5), RATE)
        b = sign_samples(voice(lead_s=0.0), RATE)
        diffs = compare(a, b)
        assert any(d.startswith("leading silence") for d in diffs), diffs

    def test_different_voice_pitch_shows_in_zero_crossings(self):
        a = sign_samples(voice(pitch=120.0), RATE)
        b = sign_samples(voice(pitch=320.0), RATE)
        diffs = compare(a, b)
        assert any("zero-crossing" in d or "envelope" in d for d in diffs), diffs

    def test_different_text_is_named_before_any_audio_measure(self):
        a = sign_samples(voice(), RATE, text="hello there")
        b = sign_samples(voice(), RATE, text="goodbye now")
        assert compare(a, b)[0].startswith("text differs")


class TestFilesAndJson:
    def test_sign_reads_a_wav_and_round_trips_json(self, tmp_path):
        p = write(tmp_path / "v.wav", voice())
        s = sign(p, text="hello", voice="piper:test")
        assert s is not None and s.sample_rate == RATE and s.voice == "piper:test"
        back = Signature.from_json(s.to_json())
        assert back == s
        assert compare(s, back) == []

    def test_sign_returns_none_for_a_non_wav(self, tmp_path):
        p = tmp_path / "x.wav"
        p.write_bytes(b"not a wav")
        assert sign(p) is None

    @pytest.mark.parametrize("seconds", [0.05, 0.5])
    def test_very_short_audio_still_signs(self, seconds):
        n = int(seconds * RATE)
        samples = array.array("h", [int(3000 * math.sin(i / 10.0)) for i in range(n)])
        s = sign_samples(samples, RATE)
        assert len(s.envelope) == len(s.zcr) == 32
        assert s.seconds == pytest.approx(seconds, abs=0.01)
