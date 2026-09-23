"""Sentence streaming: play_sentences, stream_message, speech_unit, log hygiene.

Synthesis is a fake that writes short silent WAVs; the player is a shell script
that sleeps, as in test_daemon_integration. Timing tolerances are loose on purpose.
"""

import stat
import threading
import time
import wave
from pathlib import Path
from unittest.mock import patch

import pytest

import claude_code_tts.daemon as d
from claude_code_tts.bridge import JOBS
from claude_code_tts.daemon import (
    StreamResult,
    play_sentences,
    read_playback_state,
    speech_unit,
    stream_message,
    write_playback_state,
)


def make_wav(path: Path, seconds: float, rate: int = 22050) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(seconds * rate))
    return path


def fake_player(tmp_path: Path, seconds: float) -> Path:
    script = tmp_path / "fake-player"
    script.write_text(f"#!/bin/bash\nsleep {seconds}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


class Synth:
    """Fake generate(): records calls, writes a 1 s WAV, can fail or dawdle at one index."""

    def __init__(
        self,
        fail_at: int | None = None,
        delay: float = 0.0,
        delays: dict[int, float] | None = None,
    ):
        self.calls: list[str] = []
        self.started_at: list[float] = []
        self.fail_at = fail_at
        self.delay = delay
        self.delays = delays or {}

    def __call__(self, text: str, path: Path) -> bool:
        self.started_at.append(time.monotonic())
        self.calls.append(text)
        idx = len(self.calls) - 1
        time.sleep(self.delays.get(idx, self.delay))
        if self.fail_at is not None and idx == self.fail_at:
            return False
        make_wav(path, 1.0)
        return True


def suffixes(parts: list[Path]) -> list[str]:
    """Part names without the per-pass token: m_ab12cd_s0.wav -> s0.wav."""
    return [p.name.rsplit("_", 1)[1] for p in parts]


@pytest.fixture
def env(tmp_path, monkeypatch):
    state = tmp_path / ".claude-tts"
    state.mkdir()
    # test_daemon_integration stops daemon_loop by setting this and leaves it set;
    # the stream honours it, so start each test with the daemon not shutting down.
    monkeypatch.setattr(d, "_shutdown_requested", False)
    with (
        patch.object(d, "PLAYBACK_STATE_FILE", state / "playback.json"),
        patch.object(d, "HEARTBEAT_FILE", state / "daemon.heartbeat"),
        patch.object(d, "LOG_FILE", state / "daemon.log"),
    ):
        write_playback_state(paused=False, paused_by=None, current_message=None, audio_pid=None)
        yield {"tmp": tmp_path, "state": state, "log": state / "daemon.log"}


SENTENCES = ["First one.", "Second one here.", "And the third."]


class TestPlaySentences:
    def test_plays_all_in_order_and_keeps_parts(self, env):
        synth = Synth()
        player = fake_player(env["tmp"], 0.1)
        out = env["tmp"] / "msg.wav"
        with patch.object(d, "detect_player", return_value=[str(player)]):
            r = play_sentences(SENTENCES, out, synth, speed=2.0)
        assert r.outcome == "done"
        assert r.index == 3
        assert synth.calls == SENTENCES
        assert suffixes(r.parts) == ["s0.wav", "s1.wav", "s2.wav"]
        assert all(p.exists() for p in r.parts)
        # played_s is listening time: 1 s of WAV per sentence at 2x playback
        assert r.played_s == pytest.approx(1.5, abs=0.01)

    def test_synthesizes_ahead_while_playing(self, env):
        synth = Synth(delay=0.05)
        player = fake_player(env["tmp"], 0.4)
        out = env["tmp"] / "msg.wav"
        with patch.object(d, "detect_player", return_value=[str(player)]):
            t0 = time.monotonic()
            r = play_sentences(SENTENCES, out, synth)
        assert r.outcome == "done"
        # All three syntheses started well before the first sentence finished playing.
        assert synth.started_at[2] - t0 < 0.4
        # Total time is playback bound, not synth + playback in series.
        assert time.monotonic() - t0 < 3 * 0.4 + 0.5

    def test_start_index_skips_spoken_sentences(self, env):
        synth = Synth()
        player = fake_player(env["tmp"], 0.05)
        with patch.object(d, "detect_player", return_value=[str(player)]):
            r = play_sentences(SENTENCES, env["tmp"] / "m.wav", synth, start_index=1)
        assert r.outcome == "done"
        assert synth.calls == SENTENCES[1:]
        assert suffixes(r.parts) == ["s1.wav", "s2.wav"]

    def test_start_past_end_is_done(self, env):
        r = play_sentences(SENTENCES, env["tmp"] / "m.wav", Synth(), start_index=3)
        assert r == StreamResult("done", 3)

    def test_pause_while_last_sentence_still_synthesizing(self, env):
        # Sentences 0 and 1 play in 0.1 s; sentence 2 takes 1.5 s to synthesize.
        synth = Synth(delays={2: 1.5})
        player = fake_player(env["tmp"], 0.05)

        def pause_later():
            time.sleep(0.5)
            write_playback_state(paused=True, paused_by="user")

        th = threading.Thread(target=pause_later)
        with patch.object(d, "detect_player", return_value=[str(player)]):
            th.start()
            r = play_sentences(SENTENCES, env["tmp"] / "m.wav", synth)
            th.join()
        assert r.outcome == "paused"
        assert r.index == 2
        assert r.cut_played is False  # it never started, so it is not "nearly finished"
        assert suffixes(r.parts) == ["s0.wav", "s1.wav"]
        # The abandoned worker removes its own late output.
        time.sleep(1.3)
        assert not list(env["tmp"].glob("m_*_s2.wav"))

    def test_bridge_stop_between_plays_is_seen_while_waiting(self, env):
        JOBS.create("job-w", state="playing")
        synth = Synth(delays={1: 1.0})
        player = fake_player(env["tmp"], 0.05)

        def stop_later():
            time.sleep(0.3)  # while waiting for sentence 1 to synthesize
            JOBS.update("job-w", state="cancelled")  # what bridge /stop does with no player

        th = threading.Thread(target=stop_later)
        with patch.object(d, "detect_player", return_value=[str(player)]):
            th.start()
            r = play_sentences(SENTENCES, env["tmp"] / "m.wav", synth, job_id="job-w")
            th.join()
        assert r.outcome == "cancelled"
        assert r.index == 1

    def test_shutdown_stops_at_a_sentence_boundary(self, env, monkeypatch):
        synth = Synth()
        player = fake_player(env["tmp"], 0.4)

        def shutdown_later():
            time.sleep(0.2)
            monkeypatch.setattr(d, "_shutdown_requested", True)

        th = threading.Thread(target=shutdown_later)
        with patch.object(d, "detect_player", return_value=[str(player)]):
            th.start()
            r = play_sentences(SENTENCES, env["tmp"] / "m.wav", synth)
            th.join()
        assert r.outcome == "paused"
        assert r.index == 1  # sentence 0 finished, sentence 1 never started
        assert r.cut_played is False

    def test_played_seconds_accumulate_across_passes(self, env):
        player = fake_player(env["tmp"], 0.05)
        with patch.object(d, "detect_player", return_value=[str(player)]):
            r = play_sentences(
                SENTENCES, env["tmp"] / "m.wav", Synth(), start_index=1, played_before_s=5.0
            )
        assert r.outcome == "done"
        assert r.played_s == pytest.approx(7.0, abs=0.01)

    def test_pause_mid_sentence_reports_that_sentence(self, env):
        synth = Synth()
        player = fake_player(env["tmp"], 0.6)
        out = env["tmp"] / "m.wav"

        def pause_later():
            time.sleep(0.9)  # into the second sentence
            write_playback_state(paused=True, paused_by="mic")

        th = threading.Thread(target=pause_later)
        with patch.object(d, "detect_player", return_value=[str(player)]):
            th.start()
            r = play_sentences(SENTENCES, out, synth, speed=1.0)
            th.join()
        assert r.outcome == "paused"
        assert r.index == 1
        assert r.cut_played is True
        assert r.remaining_s == pytest.approx(0.7, abs=0.25)
        # 1 s of listening for sentence 0 plus the ~0.3 s heard of sentence 1
        assert r.played_s == pytest.approx(1.3, abs=0.25)
        # Parts synthesized ahead but not spoken are removed; spoken ones remain.
        assert not list(env["tmp"].glob("m_*_s2.wav"))
        assert list(env["tmp"].glob("m_*_s0.wav"))

    def test_failed_synthesis_stops_after_what_played(self, env):
        synth = Synth(fail_at=1)
        player = fake_player(env["tmp"], 0.05)
        with patch.object(d, "detect_player", return_value=[str(player)]):
            r = play_sentences(SENTENCES, env["tmp"] / "m.wav", synth)
        assert r.outcome == "failed"
        assert r.index == 1
        assert suffixes(r.parts) == ["s0.wav"]
        assert synth.calls == SENTENCES[:2]  # the worker stops at the failure

    def test_bridge_cancel_between_sentences(self, env):
        JOBS.create("job-x", state="playing")
        synth = Synth()
        player = fake_player(env["tmp"], 0.3)

        def cancel_later():
            time.sleep(0.15)
            JOBS.request_cancel("job-x")

        th = threading.Thread(target=cancel_later)
        with patch.object(d, "detect_player", return_value=[str(player)]):
            th.start()
            r = play_sentences(SENTENCES, env["tmp"] / "m.wav", synth, job_id="job-x")
            th.join()
        assert r.outcome == "cancelled"
        assert r.index == 0
        assert JOBS.state("job-x") == "cancelled"


class TestStreamMessage:
    def msg(self, **extra):
        base = {
            "session_id": "sess",
            "project": "proj",
            "text": " ".join(SENTENCES),
            "persona": "claude-prime",
            "speed": 1.0,
            "speed_method": "playback",
        }
        base.update(extra)
        return base

    def test_done_clears_state_and_saves_history(self, env):
        player = fake_player(env["tmp"], 0.05)
        saved: list[dict] = []
        with (
            patch.object(d, "detect_player", return_value=[str(player)]),
            patch.object(d, "save_speech_wav", lambda p, **kw: saved.append(kw) or p),
        ):
            stream_message(self.msg(), Synth())
        assert read_playback_state()["current_message"] is None
        assert saved and saved[0]["text"] == " ".join(SENTENCES)
        assert not list(env["tmp"].glob("*_s*.wav"))

    def test_pause_records_sentence_index(self, env):
        player = fake_player(env["tmp"], 0.6)

        def pause_later():
            time.sleep(0.9)
            write_playback_state(paused=True, paused_by="mic")

        th = threading.Thread(target=pause_later)
        with patch.object(d, "detect_player", return_value=[str(player)]):
            th.start()
            stream_message(self.msg(), Synth())
            th.join()
        cur = read_playback_state()["current_message"]
        assert cur is not None
        assert cur["sentence_index"] == 1
        assert cur["text"] == " ".join(SENTENCES)
        assert "audio_position" not in cur
        assert "interrupted at sentence 2/3" in env["log"].read_text()

    def test_resume_starts_at_recorded_sentence(self, env):
        synth = Synth()
        player = fake_player(env["tmp"], 0.05)
        with patch.object(d, "detect_player", return_value=[str(player)]), \
             patch.object(d, "save_speech_wav", lambda p, **kw: p):
            stream_message(self.msg(sentence_index=2), synth, start_index=2)
        assert synth.calls == [SENTENCES[2]]
        assert read_playback_state()["current_message"] is None

    def test_pause_near_end_of_last_sentence_skips_replay(self, env):
        player = fake_player(env["tmp"], 0.6)

        def pause_later():
            time.sleep(1.5)  # into the last sentence, more than 0.5 s of 1 s played
            write_playback_state(paused=True, paused_by="mic")

        th = threading.Thread(target=pause_later)
        with patch.object(d, "detect_player", return_value=[str(player)]):
            th.start()
            stream_message(self.msg(), Synth())
            th.join()
        assert read_playback_state()["current_message"] is None
        assert "skipping replay" in env["log"].read_text()

    def test_failure_before_any_audio_marks_job_failed(self, env):
        JOBS.create("job-f", source="page", state="queued")
        with patch.object(d, "detect_player", return_value=["true"]):
            stream_message(self.msg(id="job-f", source="page"), Synth(fail_at=0))
        assert JOBS.state("job-f") == "failed"
        assert read_playback_state()["current_message"] is None

    def test_failure_on_resume_pass_counts_as_spoken(self, env):
        JOBS.create("job-r", source="page", state="paused")
        with patch.object(d, "detect_player", return_value=["true"]):
            stream_message(
                self.msg(id="job-r", source="page", sentence_index=2, played_s=4.0),
                Synth(fail_at=0),
                start_index=2,
            )
        assert JOBS.state("job-r") == "done"

    def test_pause_during_last_sentence_synthesis_is_not_skipped(self, env):
        player = fake_player(env["tmp"], 0.05)

        def pause_later():
            time.sleep(0.5)
            write_playback_state(paused=True, paused_by="user")

        th = threading.Thread(target=pause_later)
        with patch.object(d, "detect_player", return_value=[str(player)]):
            th.start()
            stream_message(self.msg(), Synth(delays={2: 1.5}))
            th.join()
        cur = read_playback_state()["current_message"]
        assert cur is not None and cur["sentence_index"] == 2
        assert "skipping replay" not in env["log"].read_text()

    def test_pause_carries_cumulative_listening_time(self, env):
        player = fake_player(env["tmp"], 0.6)

        def pause_later():
            time.sleep(0.9)
            write_playback_state(paused=True, paused_by="mic")

        th = threading.Thread(target=pause_later)
        with patch.object(d, "detect_player", return_value=[str(player)]):
            th.start()
            stream_message(self.msg(played_s=5.0, sentence_index=0), Synth())
            th.join()
        cur = read_playback_state()["current_message"]
        assert cur["played_s"] == pytest.approx(6.3, abs=0.3)

    def test_queue_file_removed_only_after_settle(self, env):
        player = fake_player(env["tmp"], 0.3)
        msg_file = env["tmp"] / "queued.json"
        msg_file.write_text("{}")
        seen: list[bool] = []

        def peek():
            time.sleep(0.15)  # sentence 0 is playing
            seen.append(msg_file.exists())

        th = threading.Thread(target=peek)
        with patch.object(d, "detect_player", return_value=[str(player)]), \
             patch.object(d, "save_speech_wav", lambda p, **kw: p):
            th.start()
            stream_message(self.msg(), Synth(), msg_file=msg_file)
            th.join()
        assert seen == [True]
        assert not msg_file.exists()


class TestSpeechUnit:
    @pytest.mark.parametrize(
        ("raw", "want"),
        [({}, "message"), ({"speech_unit": "sentence"}, "sentence"),
         ({"speech_unit": " Sentence "}, "sentence"), ({"speech_unit": "word"}, "message"),
         ({"speech_unit": 3}, "message")],
    )
    def test_values(self, monkeypatch, raw, want):
        monkeypatch.setattr(d, "load_raw_config", lambda: raw)
        assert speech_unit() == want


class TestLogHygiene:
    def test_log_rotates_past_max(self, env):
        env["log"].write_bytes(b"x" * (d.LOG_MAX_BYTES + 1))
        d.log("after rotation")
        assert env["log"].with_suffix(".log.1").stat().st_size == d.LOG_MAX_BYTES + 1
        assert "after rotation" in env["log"].read_text()
        assert env["log"].stat().st_size < 200

    def test_playback_does_not_log_every_second(self, env):
        wav = make_wav(env["tmp"] / "a.wav", 1.0)
        player = fake_player(env["tmp"], 1.3)
        hb = env["state"] / "daemon.heartbeat"
        with patch.object(d, "detect_player", return_value=[str(player)]):
            d.daemon_play_audio(wav)
        text = env["log"].read_text()
        assert "Poll #" not in text
        assert "Still playing" not in text  # first status line comes at 30 s
        assert hb.exists()  # heartbeat still refreshed during playback
