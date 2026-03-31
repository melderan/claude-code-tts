"""Tests for the Handy audio analyzer."""

import math
import sqlite3
import struct
import time
import wave
from pathlib import Path

import pytest

from claude_code_tts.handy import (
    ANALYSIS_DB,
    AnalysisResult,
    AudioFeatures,
    HandyWatcher,
    _autocorrelation_pitch,
    _detect_pauses,
    _read_pcm_samples,
    _rms,
    _windowed_rms,
    analyze_all_recordings,
    analyze_wav,
    get_recent_analysis,
    get_recent_tone,
    get_speech_history,
    save_speech_wav,
    store_analysis,
    summarize_tone,
)


# --- Test helpers ---

def make_sine_wav(path: Path, freq_hz: float = 200.0, duration: float = 1.0,
                  sample_rate: int = 16000, amplitude: float = 0.5) -> None:
    """Generate a sine wave WAV file."""
    n_samples = int(sample_rate * duration)
    samples = []
    for i in range(n_samples):
        t = i / sample_rate
        value = int(amplitude * 32767 * math.sin(2 * math.pi * freq_hz * t))
        samples.append(max(-32768, min(32767, value)))

    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(struct.pack(f"<{len(samples)}h", *samples))


def make_silent_wav(path: Path, duration: float = 1.0,
                    sample_rate: int = 16000) -> None:
    """Generate a silent WAV file."""
    make_sine_wav(path, freq_hz=0.0, duration=duration, sample_rate=sample_rate,
                  amplitude=0.0)


def make_speech_like_wav(path: Path, duration: float = 5.0,
                         sample_rate: int = 16000) -> None:
    """Generate a WAV that mimics speech: voiced segments with pauses."""
    n_samples = int(sample_rate * duration)
    samples = []
    for i in range(n_samples):
        t = i / sample_rate
        # Voiced for 0.8s, silent for 0.4s, repeating
        cycle_pos = t % 1.2
        if cycle_pos < 0.8:
            # Voiced segment at ~150 Hz
            value = int(0.3 * 32767 * math.sin(2 * math.pi * 150 * t))
        else:
            value = 0
        samples.append(max(-32768, min(32767, value)))

    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(struct.pack(f"<{len(samples)}h", *samples))


# --- Unit tests ---

class TestRMS:
    def test_silence(self):
        assert _rms([0.0] * 100) == 0.0

    def test_unit_signal(self):
        assert abs(_rms([1.0] * 100) - 1.0) < 0.001

    def test_known_value(self):
        # RMS of [1, -1, 1, -1] = 1.0
        assert abs(_rms([1.0, -1.0, 1.0, -1.0]) - 1.0) < 0.001

    def test_empty(self):
        assert _rms([]) == 0.0


class TestWindowedRMS:
    def test_returns_list(self):
        samples = [0.5] * 1600  # 100ms at 16kHz
        energies = _windowed_rms(samples, 16000, window_ms=30)
        assert isinstance(energies, list)
        assert len(energies) > 0

    def test_silence_is_zero(self):
        samples = [0.0] * 1600
        energies = _windowed_rms(samples, 16000, window_ms=30)
        assert all(e == 0.0 for e in energies)


class TestPauseDetection:
    def test_no_pauses_in_constant_signal(self):
        energies = [0.1] * 100
        count, total = _detect_pauses(energies)
        assert count == 0

    def test_detects_silence_gap(self):
        # 100 voiced + 50 silent + 100 voiced (at 30ms/2 = 15ms hop)
        # 50 windows * 15ms = 750ms > 300ms threshold
        energies = [0.1] * 100 + [0.001] * 50 + [0.1] * 100
        count, total = _detect_pauses(energies, window_ms=30, min_pause_ms=300)
        assert count >= 1

    def test_short_silence_ignored(self):
        # 5 silent windows * 15ms hop = 75ms < 300ms threshold
        energies = [0.1] * 50 + [0.001] * 5 + [0.1] * 50
        count, _ = _detect_pauses(energies, window_ms=30, min_pause_ms=300)
        assert count == 0


class TestAutocorrelationPitch:
    def test_detects_known_frequency(self):
        """Generate a 200 Hz sine and verify pitch detection."""
        sample_rate = 16000
        freq = 200.0
        duration = 0.05  # 50ms window
        n = int(sample_rate * duration)
        samples = [math.sin(2 * math.pi * freq * i / sample_rate) for i in range(n)]
        detected = _autocorrelation_pitch(samples, sample_rate)
        # Allow 10% tolerance
        assert abs(detected - freq) / freq < 0.10, f"Expected ~{freq}Hz, got {detected}Hz"

    def test_silence_returns_zero(self):
        samples = [0.0] * 800
        assert _autocorrelation_pitch(samples, 16000) == 0.0

    def test_low_pitch(self):
        """Detect a 100 Hz tone (male voice range)."""
        sample_rate = 16000
        freq = 100.0
        n = int(sample_rate * 0.05)
        samples = [math.sin(2 * math.pi * freq * i / sample_rate) for i in range(n)]
        detected = _autocorrelation_pitch(samples, sample_rate)
        assert abs(detected - freq) / freq < 0.15


class TestAnalyzeWav:
    def test_sine_wave(self, tmp_path):
        wav = tmp_path / "test.wav"
        make_sine_wav(wav, freq_hz=200, duration=2.0, amplitude=0.3)
        features = analyze_wav(wav, transcript="hello world test words here now")
        assert features.duration_seconds == pytest.approx(2.0, abs=0.1)
        assert features.rms_energy > 0
        assert features.pitch_mean_hz > 0
        assert features.speaking_rate_wps > 0

    def test_silent_wav(self, tmp_path):
        wav = tmp_path / "silent.wav"
        make_silent_wav(wav, duration=1.0)
        features = analyze_wav(wav)
        assert features.rms_energy == pytest.approx(0.0, abs=0.001)
        assert features.energy_label == "low"

    def test_speech_like(self, tmp_path):
        wav = tmp_path / "speech.wav"
        make_speech_like_wav(wav, duration=5.0)
        features = analyze_wav(wav, transcript="one two three four five six seven eight")
        assert features.pause_count >= 1
        assert features.pause_total_seconds > 0
        assert features.speaking_rate_wps > 0

    def test_labels_assigned(self, tmp_path):
        wav = tmp_path / "test.wav"
        make_sine_wav(wav, duration=1.0, amplitude=0.5)
        features = analyze_wav(wav)
        assert features.energy_label in ("low", "medium", "high")
        assert features.expressiveness_label in ("flat", "moderate", "animated")
        assert features.pace_label in ("slow", "moderate", "fast")


class TestSummarizeTone:
    def test_high_energy_animated(self):
        features = AudioFeatures(
            duration_seconds=5.0, rms_energy=0.15, energy_variance=0.08,
            peak_energy=0.3, pitch_mean_hz=200, pitch_range_hz=150,
            pitch_variance=40, speaking_rate_wps=4.0, pause_count=0,
            pause_total_seconds=0, pause_ratio=0.0,
            energy_label="high", expressiveness_label="animated", pace_label="fast",
        )
        summary = summarize_tone(features)
        assert "high energy" in summary
        assert "animated" in summary
        assert "fast" in summary

    def test_neutral_tone(self):
        features = AudioFeatures(
            duration_seconds=5.0, rms_energy=0.08, energy_variance=0.03,
            peak_energy=0.15, pitch_mean_hz=150, pitch_range_hz=30,
            pitch_variance=10, speaking_rate_wps=2.5, pause_count=1,
            pause_total_seconds=0.5, pause_ratio=0.1,
            energy_label="medium", expressiveness_label="moderate", pace_label="moderate",
        )
        summary = summarize_tone(features)
        assert summary == "neutral tone"

    def test_frequent_pauses(self):
        features = AudioFeatures(
            duration_seconds=10.0, rms_energy=0.05, energy_variance=0.02,
            peak_energy=0.1, pitch_mean_hz=120, pitch_range_hz=20,
            pitch_variance=5, speaking_rate_wps=2.0, pause_count=5,
            pause_total_seconds=4.0, pause_ratio=0.4,
            energy_label="medium", expressiveness_label="moderate", pace_label="moderate",
        )
        summary = summarize_tone(features)
        assert "pauses" in summary


class TestSQLiteStorage:
    def test_store_and_retrieve(self, tmp_path):
        db = tmp_path / "test_analysis.db"
        features = AudioFeatures(
            duration_seconds=5.0, rms_energy=0.08, energy_variance=0.03,
            peak_energy=0.15, pitch_mean_hz=150, pitch_range_hz=50,
            pitch_variance=15, speaking_rate_wps=3.0, pause_count=2,
            pause_total_seconds=1.5, pause_ratio=0.3,
            energy_label="medium", expressiveness_label="moderate", pace_label="moderate",
        )
        result = AnalysisResult(
            file_name="test.wav", timestamp=1000, transcript="hello world",
            features=features, analyzed_at=time.time(),
        )
        store_analysis(result, db_path=db)

        # Retrieve
        recent = get_recent_analysis(max_age_seconds=60.0, db_path=db)
        assert recent is not None
        assert recent.file_name == "test.wav"
        assert recent.transcript == "hello world"
        assert recent.features.pitch_mean_hz == 150

    def test_recent_tone(self, tmp_path):
        db = tmp_path / "test_analysis.db"
        features = AudioFeatures(
            duration_seconds=5.0, rms_energy=0.15, energy_variance=0.08,
            peak_energy=0.3, pitch_mean_hz=200, pitch_range_hz=150,
            pitch_variance=40, speaking_rate_wps=4.0, pause_count=0,
            pause_total_seconds=0, pause_ratio=0.0,
            energy_label="high", expressiveness_label="animated", pace_label="fast",
        )
        result = AnalysisResult(
            file_name="test.wav", timestamp=1000, transcript="hello",
            features=features, analyzed_at=time.time(),
        )
        store_analysis(result, db_path=db)

        tone = get_recent_tone(max_age_seconds=60.0, db_path=db)
        assert tone is not None
        assert "high energy" in tone

    def test_no_recent_when_old(self, tmp_path):
        db = tmp_path / "test_analysis.db"
        features = AudioFeatures(
            duration_seconds=5.0, rms_energy=0.08, energy_variance=0.03,
            peak_energy=0.15, pitch_mean_hz=150, pitch_range_hz=50,
            pitch_variance=15, speaking_rate_wps=3.0, pause_count=2,
            pause_total_seconds=1.5, pause_ratio=0.3,
            energy_label="medium", expressiveness_label="moderate", pace_label="moderate",
        )
        result = AnalysisResult(
            file_name="test.wav", timestamp=1000, transcript="hello",
            features=features, analyzed_at=time.time() - 120,  # 2 minutes ago
        )
        store_analysis(result, db_path=db)

        tone = get_recent_tone(max_age_seconds=30.0, db_path=db)
        assert tone is None

    def test_upsert_on_duplicate(self, tmp_path):
        db = tmp_path / "test_analysis.db"
        features = AudioFeatures(
            duration_seconds=5.0, rms_energy=0.08, energy_variance=0.03,
            peak_energy=0.15, pitch_mean_hz=150, pitch_range_hz=50,
            pitch_variance=15, speaking_rate_wps=3.0, pause_count=2,
            pause_total_seconds=1.5, pause_ratio=0.3,
            energy_label="medium", expressiveness_label="moderate", pace_label="moderate",
        )
        result = AnalysisResult(
            file_name="same.wav", timestamp=1000, transcript="v1",
            features=features, analyzed_at=time.time(),
        )
        store_analysis(result, db_path=db)
        result.transcript = "v2"
        store_analysis(result, db_path=db)

        conn = sqlite3.connect(str(db))
        count = conn.execute("SELECT COUNT(*) FROM voice_analysis").fetchone()[0]
        conn.close()
        assert count == 1  # Upserted, not duplicated


class TestAnalyzeAllRecordings:
    def test_skips_already_analyzed(self, tmp_path):
        recordings_dir = tmp_path / "recordings"
        recordings_dir.mkdir()
        db = tmp_path / "analysis.db"

        # Create two WAV files
        make_sine_wav(recordings_dir / "a.wav", duration=0.5)
        make_sine_wav(recordings_dir / "b.wav", duration=0.5)

        # Analyze once
        results1 = analyze_all_recordings(recordings_dir, db)
        assert len(results1) == 2

        # Analyze again -- should skip both
        results2 = analyze_all_recordings(recordings_dir, db)
        assert len(results2) == 0

    def test_picks_up_new_files(self, tmp_path):
        recordings_dir = tmp_path / "recordings"
        recordings_dir.mkdir()
        db = tmp_path / "analysis.db"

        make_sine_wav(recordings_dir / "a.wav", duration=0.5)
        analyze_all_recordings(recordings_dir, db)

        # Add a new file
        make_sine_wav(recordings_dir / "b.wav", duration=0.5)
        results = analyze_all_recordings(recordings_dir, db)
        assert len(results) == 1
        assert results[0].file_name == "b.wav"


class TestSpeechHistory:
    def test_save_and_retrieve(self, tmp_path):
        hist_dir = tmp_path / "history"
        db = tmp_path / "analysis.db"
        wav = tmp_path / "test.wav"
        make_sine_wav(wav, duration=0.5)

        result = save_speech_wav(
            wav, session_id="test-session", project="test-project",
            persona="claude-prime", text="Hello world", speed=2.0,
            history_dir=hist_dir, db_path=db,
        )
        assert result is not None
        assert result.exists()
        assert hist_dir.exists()

        history = get_speech_history(limit=10, db_path=db)
        assert len(history) == 1
        assert history[0]["text"] == "Hello world"
        assert history[0]["persona"] == "claude-prime"
        assert history[0]["duration_seconds"] > 0

    def test_enforces_limit(self, tmp_path):
        hist_dir = tmp_path / "history"
        db = tmp_path / "analysis.db"

        for i in range(5):
            wav = tmp_path / f"test_{i}.wav"
            make_sine_wav(wav, duration=0.2)
            save_speech_wav(
                wav, text=f"msg {i}", history_limit=3,
                history_dir=hist_dir, db_path=db,
            )

        # Only 3 should remain
        history = get_speech_history(limit=10, db_path=db)
        assert len(history) == 3
        # Most recent should be last one saved
        assert history[0]["text"] == "msg 4"

        # Only 3 WAV files on disk
        wav_files = list(hist_dir.glob("*.wav"))
        assert len(wav_files) == 3

    def test_missing_wav_returns_none(self, tmp_path):
        result = save_speech_wav(
            tmp_path / "nonexistent.wav",
            history_dir=tmp_path / "hist", db_path=tmp_path / "db",
        )
        assert result is None

    def test_preserves_original(self, tmp_path):
        """Original WAV is not moved, only copied."""
        hist_dir = tmp_path / "history"
        db = tmp_path / "analysis.db"
        wav = tmp_path / "original.wav"
        make_sine_wav(wav, duration=0.3)

        save_speech_wav(wav, history_dir=hist_dir, db_path=db)
        assert wav.exists()  # Original still there


class TestReadPCMSamples:
    def test_reads_16bit_mono(self, tmp_path):
        wav = tmp_path / "test.wav"
        make_sine_wav(wav, duration=0.1, sample_rate=16000)
        samples, rate = _read_pcm_samples(wav)
        assert rate == 16000
        assert len(samples) == 1600  # 0.1s * 16000

    def test_normalized_range(self, tmp_path):
        wav = tmp_path / "test.wav"
        make_sine_wav(wav, duration=0.1, amplitude=1.0)
        samples, _ = _read_pcm_samples(wav)
        assert all(-1.0 <= s <= 1.0 for s in samples)
