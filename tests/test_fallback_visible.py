"""A reply spoken by the Piper spare instead of the engine the persona asked for says so.

2026-10-08 18:38Z: a reply planned for Kokoro under mlx came out in the Piper voice while a long
GPU job saturated the Mac. The mlx worker gave no answer in 120 s and was killed, Piper stood in,
and the daemon log's "Speaking for" line still named mlx: the only trace was one debug line. Now
generate_speech records which engine produced the WAV and why a spare stood in, and the daemon
logs a WARN naming both. Also: a status check that cannot see the daemon's pid no longer removes
its pid file, and the daemon writes the file again if it is gone.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

import claude_code_tts.audio as audio
import claude_code_tts.daemon as d
import claude_code_tts.state as st


def _piper_ok(cmd, **kw):
    out = Path(cmd[cmd.index("--output_file") + 1])
    out.write_bytes(b"RIFF")
    return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


@pytest.fixture
def piper(tmp_path, monkeypatch):
    voice = tmp_path / "en_US-hfc_male-medium.onnx"
    voice.write_bytes(b"onnx")
    monkeypatch.setattr(audio.shutil, "which", lambda name: "/usr/bin/piper" if name == "piper" else None)
    monkeypatch.setattr(audio, "_apply_pitch_filter", lambda *a, **k: None)
    audio._spoke_with("", "")
    audio._set_last_error("")
    return voice


class TestGenerateSpeechSaysWhoSpoke:
    def test_mlx_timeout_then_piper_is_a_named_fallback(self, piper, tmp_path):
        def mlx_fails(text, **kw):
            audio._set_last_error("mlx worker for m produced no audio (see worker.log)")
            return None

        with patch.object(audio, "_generate_mlx", mlx_fails), patch.object(audio.subprocess, "run", _piper_ok):
            wav = audio.generate_speech("hello", voice_path=piper, voice_mlx="m", speaker_mlx="af_heart", output_path=tmp_path / "o.wav")
        assert wav is not None
        assert audio.last_engine() == "piper:en_US-hfc_male-medium"
        assert audio.last_fallback() == "mlx:m#af_heart: mlx worker for m produced no audio (see worker.log)"
        assert audio.last_error() == ""

    def test_mlx_that_answers_is_no_fallback(self, piper, tmp_path):
        def mlx_ok(text, *, output_path, **kw):
            output_path.write_bytes(b"RIFF")
            return output_path

        with patch.object(audio, "_generate_mlx", mlx_ok):
            audio.generate_speech("hello", voice_path=piper, voice_mlx="m", speaker_mlx="af_heart", output_path=tmp_path / "o.wav")
        assert audio.last_engine() == "mlx:m#af_heart"
        assert audio.last_fallback() == ""

    def test_a_piper_only_persona_is_no_fallback(self, piper, tmp_path):
        with patch.object(audio.subprocess, "run", _piper_ok):
            audio.generate_speech("hello", voice_path=piper, output_path=tmp_path / "o.wav")
        assert audio.last_engine() == "piper:en_US-hfc_male-medium"
        assert audio.last_fallback() == ""

    def test_kokoro_that_wrote_no_file_is_a_named_fallback(self, piper, tmp_path, monkeypatch):
        monkeypatch.setattr(audio.shutil, "which", lambda name: f"/usr/bin/{name}" if name in ("piper", "swift-kokoro") else None)

        def run(cmd, **kw):
            if cmd[0] == "swift-kokoro":
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
            return _piper_ok(cmd, **kw)

        with patch.object(audio.subprocess, "run", run):
            audio.generate_speech("hello", voice_path=piper, voice_kokoro="bf_emma", output_path=tmp_path / "o.wav")
        assert audio.last_engine() == "piper:en_US-hfc_male-medium"
        assert audio.last_fallback() == "kokoro:bf_emma: swift-kokoro produced no file for voice bf_emma"

    def test_the_fallback_is_forgotten_once_the_asked_engine_speaks_again(self, piper, tmp_path):
        audio._spoke_with("piper:x", "mlx:m: earlier")

        def mlx_ok(text, *, output_path, **kw):
            output_path.write_bytes(b"RIFF")
            return output_path

        with patch.object(audio, "_generate_mlx", mlx_ok):
            audio.generate_speech("hello", voice_path=piper, voice_mlx="m", output_path=tmp_path / "o.wav")
        assert audio.last_fallback() == ""


class TestDaemonLogsTheFallback:
    def test_one_warn_line_names_engine_reason_and_text(self, tmp_path):
        lines: list[tuple[str, str]] = []
        with patch.object(d, "_generate_speech_unleveled", lambda *a, **k: True), \
             patch.object(d, "level_after_synthesis", lambda *a, **k: None), \
             patch.object(d, "audio_last_fallback", lambda: "mlx:m#af_heart: mlx worker for m produced no audio"), \
             patch.object(d, "audio_last_engine", lambda: "piper:en_US-hfc_male-medium"), \
             patch.object(d, "log", lambda m, level="INFO": lines.append((level, m))):
            assert d.daemon_generate_speech("Stopped.  Sorry about that.", "jmo-heart", tmp_path / "o.wav") is True
        assert lines == [(
            "WARN",
            "Fallback voice for jmo-heart: spoke with piper:en_US-hfc_male-medium because "
            "mlx:m#af_heart: mlx worker for m produced no audio | Stopped. Sorry about that....",
        )]

    def test_silent_when_the_asked_engine_spoke(self, tmp_path):
        lines: list[str] = []
        with patch.object(d, "_generate_speech_unleveled", lambda *a, **k: True), \
             patch.object(d, "level_after_synthesis", lambda *a, **k: None), \
             patch.object(d, "audio_last_fallback", lambda: ""), \
             patch.object(d, "log", lambda m, level="INFO": lines.append(m)):
            d.daemon_generate_speech("hello", "jmo-heart", tmp_path / "o.wav")
        assert lines == []


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(st, "PID_FILE", tmp_path / "daemon.pid")
    monkeypatch.setattr(st, "HEARTBEAT_FILE", tmp_path / "daemon.heartbeat")
    return tmp_path


class TestPidFileSurvivesAStatusCheck:
    def test_fresh_heartbeat_without_a_pid_file_is_running(self, state_dir):
        st.write_heartbeat(force=True)
        assert st.is_daemon_running() == (True, None)

    def test_fresh_heartbeat_with_a_pid_file_names_the_pid(self, state_dir):
        st.HEARTBEAT_FILE.write_text(str(__import__("time").time()))
        st.PID_FILE.write_text("4242")
        assert st.is_daemon_running() == (True, 4242)

    @pytest.mark.parametrize("heartbeat", ["", "not a number"])
    def test_an_unreadable_heartbeat_and_an_invisible_pid_keeps_the_pid_file(self, state_dir, heartbeat):
        """The 00:49Z case: the heartbeat read empty through the mount, the pid is the host's."""
        st.HEARTBEAT_FILE.write_text(heartbeat)
        st.PID_FILE.write_text("4242")
        with patch.object(st.os, "kill", side_effect=ProcessLookupError):
            assert st.is_daemon_running() == (False, None)
        assert st.PID_FILE.exists()

    def test_a_readable_old_heartbeat_and_a_dead_pid_clears_the_pid_file(self, state_dir):
        st.HEARTBEAT_FILE.write_text("1.0")
        st.PID_FILE.write_text("4242")
        with patch.object(st.os, "kill", side_effect=ProcessLookupError):
            assert st.is_daemon_running() == (False, None)
        assert not st.PID_FILE.exists()

    def test_heartbeat_age_is_none_when_unreadable(self, state_dir):
        assert st.heartbeat_age() is None
        st.HEARTBEAT_FILE.write_text("")
        assert st.heartbeat_age() is None
        st.HEARTBEAT_FILE.write_text("1.0")
        assert st.heartbeat_age() is not None and st.heartbeat_age() > 1000

    def test_no_heartbeat_file_and_a_dead_pid_removes_the_pid_file(self, state_dir):
        st.PID_FILE.write_text("4242")
        with patch.object(st.os, "kill", side_effect=ProcessLookupError):
            assert st.is_daemon_running() == (False, None)
        assert not st.PID_FILE.exists()

    def test_a_live_pid_without_a_heartbeat_is_running(self, state_dir):
        st.PID_FILE.write_text("4242")
        with patch.object(st.os, "kill", lambda pid, sig: None):
            assert st.is_daemon_running() == (True, 4242)

    def test_nothing_at_all_is_not_running(self, state_dir):
        assert st.is_daemon_running() == (False, None)

    def test_an_unreadable_pid_file_with_no_heartbeat_file_is_removed(self, state_dir):
        st.PID_FILE.write_text("garbage")
        assert st.is_daemon_running() == (False, None)
        assert not st.PID_FILE.exists()

    def test_an_unreadable_pid_file_with_an_unreadable_heartbeat_is_kept(self, state_dir):
        st.PID_FILE.write_text("garbage")
        st.HEARTBEAT_FILE.write_text("")
        assert st.is_daemon_running() == (False, None)
        assert st.PID_FILE.exists()

    def test_the_daemon_writes_its_pid_file_again(self, state_dir):
        assert st.ensure_pid_file() is True
        assert st.PID_FILE.read_text() == str(__import__("os").getpid())
        assert st.ensure_pid_file() is False


def _wav(path: Path, frames: bytes, rate: int = 22050) -> Path:
    import wave

    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)
    return path


def _frames(path: Path) -> bytes:
    import wave

    with wave.open(str(path), "rb") as w:
        return w.readframes(w.getnframes())


class TestTheSpareSaysSoFirst:
    """D48 (2026-10-08, A): the spare speaks a short tell before a reply its engine could not make."""

    @pytest.fixture
    def fallen_back(self, tmp_path):
        """A funnel whose engine step writes a WAV from the Piper spare and names the fallback."""
        tells: list[str] = []
        lines: list[tuple[str, str]] = []

        def body(text, persona, output_file, *a, **k):
            _wav(output_file, b"BB" * 100)
            audio._spoke_with("piper:en_US-hfc_male-medium", "mlx:m#af_heart: no answer in 120 s")
            return True

        def tell(text, *, voice_path, output_path, **k):
            tells.append(f"{text}@{voice_path.name}")
            return _wav(output_path, b"TT" * 10)

        with patch.object(d, "_generate_speech_unleveled", body), \
             patch.object(d, "_generate_speech", tell), \
             patch.object(d, "level_after_synthesis", lambda *a, **k: None), \
             patch.object(d, "load_raw_config", lambda: {}), \
             patch.object(d, "log", lambda m, level="INFO": lines.append((level, m))):
            yield tells, lines

    def test_a_message_path_starts_with_the_tell_in_the_spare_voice(self, fallen_back, tmp_path):
        tells, lines = fallen_back
        out = tmp_path / "o.wav"
        assert d.daemon_generate_speech("the reply", "jmo-heart", out, tell_on_fallback=True) is True
        assert tells == ["Spare voice standing in.@en_US-hfc_male-medium.onnx"]
        assert _frames(out) == b"TT" * 10 + b"BB" * 100
        assert not out.with_name("o_tell.wav").exists()
        assert lines[0][0] == "WARN" and "; said so first |" in lines[0][1]

    def test_an_announcement_path_has_no_tell(self, fallen_back, tmp_path):
        tells, lines = fallen_back
        out = tmp_path / "o.wav"
        assert d.daemon_generate_speech("Voice daemon online", "jmo-heart", out) is True
        assert tells == []
        assert _frames(out) == b"BB" * 100
        assert "said so first" not in lines[0][1]

    def test_one_tell_per_message_when_sentences_stream(self, fallen_back, tmp_path):
        tells, _ = fallen_back
        gen = d.sentence_generator("jmo-heart")
        for i in range(3):
            assert gen(f"sentence {i}", tmp_path / f"s{i}.wav") is True
        assert len(tells) == 1
        assert _frames(tmp_path / "s0.wav").startswith(b"TT" * 10)
        assert _frames(tmp_path / "s1.wav") == b"BB" * 100

    def test_a_new_message_tells_again(self, fallen_back, tmp_path):
        tells, _ = fallen_back
        d.sentence_generator("jmo-heart")("one", tmp_path / "a.wav")
        d.sentence_generator("jmo-heart")("two", tmp_path / "b.wav")
        assert len(tells) == 2

    def test_an_empty_fallback_tell_keeps_the_log_line_only(self, fallen_back, tmp_path):
        tells, lines = fallen_back
        out = tmp_path / "o.wav"
        with patch.object(d, "load_raw_config", lambda: {"fallback_tell": ""}):
            d.daemon_generate_speech("the reply", "jmo-heart", out, tell_on_fallback=True)
        assert tells == []
        assert _frames(out) == b"BB" * 100
        assert lines[0][0] == "WARN" and "said so first" not in lines[0][1]

    def test_a_tell_the_spare_cannot_make_leaves_the_reply_whole(self, fallen_back, tmp_path):
        tells, lines = fallen_back
        out = tmp_path / "o.wav"
        with patch.object(d, "_generate_speech", lambda *a, **k: None):
            d.daemon_generate_speech("the reply", "jmo-heart", out, tell_on_fallback=True)
        assert _frames(out) == b"BB" * 100
        assert ("WARN", "Fallback tell not made: ") == lines[0]

    def test_a_tell_with_other_wav_parameters_is_dropped_not_joined(self, fallen_back, tmp_path):
        out = tmp_path / "o.wav"
        with patch.object(d, "_generate_speech", lambda text, *, output_path, **k: _wav(output_path, b"TT", rate=24000)):
            d.daemon_generate_speech("the reply", "jmo-heart", out, tell_on_fallback=True)
        assert _frames(out) == b"BB" * 100

    def test_when_the_asked_engine_spoke_nothing_is_added(self, tmp_path):
        tells: list[str] = []

        def body(text, persona, output_file, *a, **k):
            _wav(output_file, b"BB" * 100)
            audio._spoke_with("mlx:m#af_heart", "")
            return True

        out = tmp_path / "o.wav"
        with patch.object(d, "_generate_speech_unleveled", body), \
             patch.object(d, "_generate_speech", lambda *a, **k: tells.append("x")), \
             patch.object(d, "level_after_synthesis", lambda *a, **k: None):
            d.daemon_generate_speech("the reply", "jmo-heart", out, tell_on_fallback=True)
        assert tells == [] and _frames(out) == b"BB" * 100
