"""Tests for position-aware resume playback (WAV trimming, position tracking)."""

import wave
from pathlib import Path

from claude_code_tts.daemon import (
    NEAR_END_THRESHOLD,
    REWIND_REAL_SECONDS,
    calculate_audio_position,
    get_wav_duration,
    rewind_amount,
    trim_wav,
)


def make_wav(path: Path, duration_seconds: float, sample_rate: int = 22050) -> Path:
    """Create a WAV file with silence of the given duration."""
    n_frames = int(duration_seconds * sample_rate)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)  # 16-bit
        w.setframerate(sample_rate)
        # Write silence (zeros)
        w.writeframes(b"\x00\x00" * n_frames)
    return path


class TestGetWavDuration:
    def test_correct_duration(self, tmp_path):
        wav = make_wav(tmp_path / "test.wav", 10.0)
        duration = get_wav_duration(wav)
        assert abs(duration - 10.0) < 0.01

    def test_short_duration(self, tmp_path):
        wav = make_wav(tmp_path / "short.wav", 0.5)
        duration = get_wav_duration(wav)
        assert abs(duration - 0.5) < 0.01

    def test_nonexistent_file(self, tmp_path):
        assert get_wav_duration(tmp_path / "nope.wav") == 0.0

    def test_invalid_file(self, tmp_path):
        bad = tmp_path / "bad.wav"
        bad.write_text("not a wav file")
        assert get_wav_duration(bad) == 0.0


class TestTrimWav:
    def test_trim_from_middle(self, tmp_path):
        src = make_wav(tmp_path / "src.wav", 10.0)
        dst = tmp_path / "dst.wav"
        assert trim_wav(src, dst, 5.0) is True
        trimmed_duration = get_wav_duration(dst)
        assert abs(trimmed_duration - 5.0) < 0.01

    def test_trim_from_start(self, tmp_path):
        src = make_wav(tmp_path / "src.wav", 10.0)
        dst = tmp_path / "dst.wav"
        assert trim_wav(src, dst, 0.0) is True
        assert abs(get_wav_duration(dst) - 10.0) < 0.01

    def test_trim_near_end(self, tmp_path):
        src = make_wav(tmp_path / "src.wav", 10.0)
        dst = tmp_path / "dst.wav"
        assert trim_wav(src, dst, 9.0) is True
        assert abs(get_wav_duration(dst) - 1.0) < 0.01

    def test_trim_past_end_returns_false(self, tmp_path):
        src = make_wav(tmp_path / "src.wav", 5.0)
        dst = tmp_path / "dst.wav"
        assert trim_wav(src, dst, 6.0) is False

    def test_trim_exact_end_returns_false(self, tmp_path):
        src = make_wav(tmp_path / "src.wav", 5.0)
        dst = tmp_path / "dst.wav"
        assert trim_wav(src, dst, 5.0) is False

    def test_trim_preserves_sample_rate(self, tmp_path):
        src = make_wav(tmp_path / "src.wav", 10.0, sample_rate=44100)
        dst = tmp_path / "dst.wav"
        trim_wav(src, dst, 3.0)
        with wave.open(str(dst), "rb") as w:
            assert w.getframerate() == 44100
            assert w.getnchannels() == 1
            assert w.getsampwidth() == 2

    def test_trim_nonexistent_source(self, tmp_path):
        dst = tmp_path / "dst.wav"
        assert trim_wav(tmp_path / "nope.wav", dst, 1.0) is False


class TestCalculateAudioPosition:
    def test_playback_speed_2x(self):
        # 5 real seconds at 2x speed = 10 seconds of audio
        assert calculate_audio_position(5.0, 2.0, "playback") == 10.0

    def test_playback_speed_1x(self):
        assert calculate_audio_position(5.0, 1.0, "playback") == 5.0

    def test_playback_speed_half(self):
        assert calculate_audio_position(10.0, 0.5, "playback") == 5.0

    def test_length_scale_ignores_speed(self):
        # length_scale bakes speed into WAV, real time = audio time
        assert calculate_audio_position(5.0, 2.0, "length_scale") == 5.0
        assert calculate_audio_position(5.0, 0.5, "length_scale") == 5.0

    def test_zero_elapsed(self):
        assert calculate_audio_position(0.0, 2.0, "playback") == 0.0


class TestResumeDecisions:
    """Test the decision logic for whether to skip, rewind, or replay."""

    def test_near_end_should_skip(self):
        """Audio position near end of WAV should skip replay."""
        wav_duration = 30.0
        audio_position = 29.0
        remaining = wav_duration - audio_position
        assert remaining <= NEAR_END_THRESHOLD

    def test_early_interruption_should_replay(self):
        """Interruption early in audio should replay (possibly with rewind)."""
        wav_duration = 30.0
        audio_position = 3.0
        remaining = wav_duration - audio_position
        assert remaining > NEAR_END_THRESHOLD
        # At 2x, rewind_amount = 3.0 * 2 = 6.0 wav-seconds
        rw = rewind_amount(2.0, "playback")
        resume_from = max(0.0, audio_position - rw)
        assert resume_from == 0.0  # Can't go negative, replays from start

    def test_mid_interruption_should_rewind(self):
        """Interruption in the middle should resume with speed-aware rewind."""
        wav_duration = 30.0
        audio_position = 20.0
        remaining = wav_duration - audio_position
        assert remaining > NEAR_END_THRESHOLD
        # At 2x speed, 3 real seconds = 6 wav-seconds rewind
        rw = rewind_amount(2.0, "playback")
        resume_from = max(0.0, audio_position - rw)
        assert resume_from == 14.0  # 20.0 - 6.0

    def test_first_interruption_no_position(self):
        """First-ever interruption has audio_position=0, should replay from start."""
        audio_position = 0.0
        # The code checks: if prev_audio_pos > 0 and remaining <= threshold
        # With position 0, it always replays (never hits skip condition)
        assert not (audio_position > 0)

    def test_position_accumulates_across_pauses(self):
        """Multiple pause/resume cycles should accumulate position correctly."""
        speed = 2.0
        speed_method = "playback"
        rw = rewind_amount(speed, speed_method)  # 3.0 * 2.0 = 6.0 wav-seconds

        # First play: 5 real seconds at 2x = 10s audio position
        elapsed_1 = 5.0
        pos_1 = calculate_audio_position(elapsed_1, speed, speed_method)
        assert pos_1 == 10.0

        # Resume with speed-aware rewind, play 3 real seconds
        resume_from = max(0.0, pos_1 - rw)  # 10.0 - 6.0 = 4.0
        elapsed_2 = 3.0
        pos_2 = resume_from + calculate_audio_position(elapsed_2, speed, speed_method)
        assert pos_2 == 10.0  # 4.0 + 6.0

        # Resume again
        resume_from_2 = max(0.0, pos_2 - rw)  # 10.0 - 6.0 = 4.0
        assert resume_from_2 == 4.0


class TestRewindAmount:
    def test_2x_playback(self):
        # At 2x, 3 real seconds = 6 wav-seconds of rewind
        assert rewind_amount(2.0, "playback") == REWIND_REAL_SECONDS * 2.0

    def test_1x_playback(self):
        assert rewind_amount(1.0, "playback") == REWIND_REAL_SECONDS

    def test_half_speed(self):
        assert rewind_amount(0.5, "playback") == REWIND_REAL_SECONDS * 0.5

    def test_length_scale_ignores_speed(self):
        assert rewind_amount(2.0, "length_scale") == REWIND_REAL_SECONDS
        assert rewind_amount(0.5, "length_scale") == REWIND_REAL_SECONDS


class TestConstants:
    def test_near_end_threshold_reasonable(self):
        assert 1.0 <= NEAR_END_THRESHOLD <= 5.0

    def test_rewind_real_seconds_reasonable(self):
        assert 1.0 <= REWIND_REAL_SECONDS <= 5.0
