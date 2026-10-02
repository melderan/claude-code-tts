"""Sentence streaming: play_sentences, stream_message, speech_unit, log hygiene.

Synthesis is a fake that writes short silent WAVs; the player is a shell script
that sleeps, as in test_daemon_integration. A stop, pause or shutdown lands at a
causal point (a player start, a play that returned, the stream looking again, a
synthesis call), never after a timed sleep racing the player: on a slow runner
(macOS CI, 2026-10-02) a 0.3 s sleep landed a sentence early. A sleep left here is
a lower bound on how long something has played, and the assertions only use it so.
"""

import stat
import threading
import time
import wave
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

import pytest

import claude_code_tts.daemon as d
import claude_code_tts.msgqueue as mq
import claude_code_tts.state as st
from claude_code_tts.bridge import JOBS, flush_source
from claude_code_tts.daemon import (
    StreamResult,
    play_sentences,
    read_playback_state,
    speech_unit,
    stream_message,
    write_playback_state,
)
from tests.test_pause_ledger import loop_harness, start_loop, stop_loop


def make_wav(path: Path, seconds: float, rate: int = 22050) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(seconds * rate))
    return path


def fake_player(tmp_path: Path, seconds: float, started: Path | None = None) -> Path:
    """A player that sleeps. With `started`, it touches that file first, so a test
    can act at a known point of playback instead of guessing with a timed sleep."""
    script = tmp_path / "fake-player"
    touch = f"touch {started}\n" if started else ""
    script.write_text(f"#!/bin/bash\n{touch}sleep {seconds}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def counting_player(tmp_path: Path, durations: list[float], then: float = 30.0) -> tuple[Path, Path]:
    """A player whose k-th call sleeps durations[k] (`then` after the list) and logs
    each start as a line, so a test can act during a known sentence. It execs sleep,
    so the daemon's terminate stops the sleep itself and no orphan outlives a test."""
    starts = tmp_path / "player-starts"
    cases = "".join(f"  {k + 1}) exec sleep {sec} ;;\n" for k, sec in enumerate(durations))
    script = tmp_path / "counting-player"
    script.write_text(
        f"#!/bin/bash\necho started >> {starts}\nn=$(wc -l < {starts})\n"
        f"case $n in\n{cases}  *) exec sleep {then} ;;\nesac\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script, starts


def starts_seen(starts: Path) -> int:
    return len(starts.read_text().splitlines()) if starts.exists() else 0


def wait_until(pred: Callable[[], bool], timeout: float = 20.0) -> None:
    """Poll for a condition; the deadline only stops a broken test from hanging."""
    deadline = time.monotonic() + timeout
    while not pred():
        assert time.monotonic() < deadline, "condition never came true"
        time.sleep(0.01)


class Plays:
    """Stands in for daemon_play_audio, counting the plays that returned.

    after[k] runs on the stream's own thread right after the k-th play returns,
    before the stream looks at the job, the pause flag or the next sentence.
    """

    def __init__(self) -> None:
        self.finished = 0
        self.after: dict[int, Callable[[], None]] = {}
        self._real = d.daemon_play_audio

    def __call__(self, *args, **kwargs):
        result = self._real(*args, **kwargs)
        self.finished += 1
        hook = self.after.get(self.finished)
        if hook is not None:
            hook()
        return result


class Synth:
    """Fake generate(): records calls, writes a 1 s WAV, can fail or dawdle at one index.

    hooks[i] runs inside the call for sentence i, before the WAV is written: a
    test blocks there to hold a sentence "still synthesizing" for as long as it needs.
    """

    def __init__(
        self,
        fail_at: int | None = None,
        delay: float = 0.0,
        delays: dict[int, float] | None = None,
        hooks: dict[int, Callable[[], None]] | None = None,
    ):
        self.calls: list[str] = []
        self.started_at: list[float] = []
        self.fail_at = fail_at
        self.delay = delay
        self.delays = delays or {}
        self.hooks = hooks or {}

    def __call__(self, text: str, path: Path) -> bool:
        self.started_at.append(time.monotonic())
        self.calls.append(text)
        idx = len(self.calls) - 1
        hook = self.hooks.get(idx)
        if hook is not None:
            hook()
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
        patch.object(st, "PLAYBACK_STATE_FILE", state / "playback.json"),
        patch.object(st, "HEARTBEAT_FILE", state / "daemon.heartbeat"),
        patch.object(d, "LOG_FILE", state / "daemon.log"),
    ):
        write_playback_state(paused=False, paused_by=None, current_message=None, audio_pid=None)
        yield {"tmp": tmp_path, "state": state, "log": state / "daemon.log"}


SENTENCES = ["First one.", "Second one here.", "And the third."]


def pause_into_sentence(starts: Path, k: int, heard_s: float = 0.0) -> threading.Thread:
    """A thread that pauses (as the mic) once the k-th player has started and played
    at least heard_s; the sleep is a lower bound on what was heard, not a guess."""

    def run() -> None:
        wait_until(lambda: starts_seen(starts) >= k)
        if heard_s:
            time.sleep(heard_s)
        write_playback_state(paused=True, paused_by="mic")

    return threading.Thread(target=run)


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
        # Sentence 0's player runs until sentence 2's synthesis has begun, so the
        # claim holds by cause, not by a 0.05 s synth outrunning a 0.4 s player.
        # A stream that did not synthesize ahead would hold the player to its cap.
        go = env["tmp"] / "synth-2-began"
        capped = env["tmp"] / "player-hit-its-cap"
        synth = Synth(hooks={2: go.touch})
        _, starts = counting_player(env["tmp"], [])
        player = env["tmp"] / "holding-player"
        player.write_text(
            f"#!/bin/bash\necho started >> {starts}\n"
            f'if [ "$(wc -l < {starts})" -gt 1 ]; then exit 0; fi\n'
            f"for _ in $(seq 1 2000); do [ -e {go} ] && exit 0; sleep 0.01; done\n"
            f"touch {capped}\n"
        )
        player.chmod(0o755)
        out = env["tmp"] / "msg.wav"
        play_ended: list[float] = []
        real_play = d.daemon_play_audio

        def recording_play(*args, **kwargs):
            result = real_play(*args, **kwargs)
            play_ended.append(time.monotonic())
            return result

        with (
            patch.object(d, "detect_player", return_value=[str(player)]),
            patch.object(d, "daemon_play_audio", recording_play),
        ):
            r = play_sentences(SENTENCES, out, synth)
        assert r.outcome == "done"
        # Ordering, not wall time (CI runners are slow): every synthesis had started
        # before the first sentence finished playing, so playback never waited.
        assert len(synth.started_at) == 3
        assert not capped.exists()
        assert synth.started_at[2] < play_ended[0]

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
        # Sentence 2 synthesizes until the test lets it go; the pause lands once
        # sentence 1 has played, so the stream is waiting on sentence 2.
        release = threading.Event()
        synth = Synth(hooks={2: lambda: release.wait(20)})
        player = fake_player(env["tmp"], 0.05)
        plays = Plays()
        plays.after[2] = lambda: write_playback_state(paused=True, paused_by="user")
        with (
            patch.object(d, "detect_player", return_value=[str(player)]),
            patch.object(d, "daemon_play_audio", plays),
        ):
            r = play_sentences(SENTENCES, env["tmp"] / "m.wav", synth)
        assert r.outcome == "paused"
        assert r.index == 2
        assert r.cut_played is False  # it never started, so it is not "nearly finished"
        assert suffixes(r.parts) == ["s0.wav", "s1.wav"]
        # The worker finishes only after the stream has ended, so the late output
        # is its own to remove.
        release.set()
        for t in [t for t in threading.enumerate() if t.name == "tts-synth"]:
            t.join(20)
        assert not list(env["tmp"].glob("m_*_s2.wav"))

    def test_bridge_stop_between_plays_is_seen_while_waiting(self, env):
        # A 0.3 s sleep stood in for "while sentence 1 synthesizes" here; on a slow
        # macOS runner it landed while sentence 0 still played (index 0, 2026-10-02).
        # Now the stop lands when the stream, sentence 0 played, looks again, and
        # sentence 1 stays in synthesis until the stream has settled the job.
        JOBS.create("job-w", state="playing")
        settled = lambda: "position_ms" in (JOBS.get("job-w") or {})  # noqa: E731
        synth = Synth(hooks={1: lambda: wait_until(settled)})
        player = fake_player(env["tmp"], 0.05)
        plays = Plays()
        waiting = threading.Event()
        real_read = d.read_playback_state

        def read_after_first_play():
            if plays.finished == 1:
                waiting.set()
            return real_read()

        def stop_while_waiting():
            waiting.wait(20)
            JOBS.update("job-w", state="cancelled")  # what bridge /stop does with no player

        th = threading.Thread(target=stop_while_waiting)
        with (
            patch.object(d, "detect_player", return_value=[str(player)]),
            patch.object(d, "daemon_play_audio", plays),
            patch.object(d, "read_playback_state", read_after_first_play),
        ):
            th.start()
            r = play_sentences(SENTENCES, env["tmp"] / "m.wav", synth, job_id="job-w")
            th.join()
        assert r.outcome == "cancelled"
        assert r.index == 1
        assert suffixes(r.parts) == ["s0.wav"]
        assert plays.finished == 1

    def test_bridge_stop_while_first_sentence_synthesizes_is_not_overwritten(self, env):
        # The stop lands inside sentence 0's synthesis, which returns at once, so
        # the sentence is ready before the stream's first look. The stream used to
        # mark the job playing over the cancel and speak the whole message.
        JOBS.create("job-s", state="synthesizing")
        synth = Synth(hooks={0: lambda: JOBS.update("job-s", state="cancelled")})
        player, starts = counting_player(env["tmp"], [0.05])
        with patch.object(d, "detect_player", return_value=[str(player)]):
            r = play_sentences(SENTENCES, env["tmp"] / "m.wav", synth, job_id="job-s")
        assert r.outcome == "cancelled"
        assert r.index == 0
        assert r.parts == []
        assert starts_seen(starts) == 0
        assert JOBS.state("job-s") == "cancelled"

    def test_bridge_stop_marked_during_play_stops_the_player(self, env):
        # /stop reads the player pid, then acts: no pid means it marks the job
        # cancelled instead of asking for a cancel. A player the daemon starts in
        # between must still stop, not play out its sentence.
        JOBS.create("job-p", state="playing")
        player, starts = counting_player(env["tmp"], [], then=30.0)

        def mark_once_playing():
            wait_until(lambda: starts_seen(starts) >= 1)
            JOBS.update("job-p", state="cancelled")

        th = threading.Thread(target=mark_once_playing)
        began = time.monotonic()
        with patch.object(d, "detect_player", return_value=[str(player)]):
            th.start()
            r = play_sentences(SENTENCES, env["tmp"] / "m.wav", Synth(), job_id="job-p")
            th.join()
        assert r.outcome == "cancelled"
        assert r.index == 0
        assert starts_seen(starts) == 1
        assert time.monotonic() - began < 20  # killed, not 30 s of a stopped sentence

    def test_shutdown_stops_at_a_sentence_boundary(self, env, monkeypatch):
        synth = Synth()
        # Sentence 0 plays until the test has asked for the shutdown.
        player, starts = counting_player(env["tmp"], [])
        asked = env["tmp"] / "asked"
        player.write_text(
            f"#!/bin/bash\necho started >> {starts}\n"
            f"while [ ! -e {asked} ]; do sleep 0.01; done\n"
        )

        def shutdown_during_first_play():
            wait_until(lambda: starts_seen(starts) >= 1)
            monkeypatch.setattr(d, "_shutdown_requested", True)
            asked.touch()

        th = threading.Thread(target=shutdown_during_first_play)
        with patch.object(d, "detect_player", return_value=[str(player)]):
            th.start()
            r = play_sentences(SENTENCES, env["tmp"] / "m.wav", synth)
            th.join()
        assert starts_seen(starts) == 1
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
        # Sentence 1's player runs until the pause kills it.
        player, starts = counting_player(env["tmp"], [0.05])
        out = env["tmp"] / "m.wav"
        th = pause_into_sentence(starts, 2, heard_s=0.3)
        with patch.object(d, "detect_player", return_value=[str(player)]):
            th.start()
            r = play_sentences(SENTENCES, out, synth, speed=1.0)
            th.join()
        assert r.outcome == "paused"
        assert r.index == 1
        assert r.cut_played is True
        # 1 s of listening for sentence 0 plus at least the 0.3 s heard of sentence 1;
        # what is left of sentence 1 is the rest of its 1 s, however slow the runner.
        heard = r.played_s - 1.0
        assert heard >= 0.3
        assert r.remaining_s == pytest.approx(max(0.0, 1.0 - heard), abs=1e-6)
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
        # The cancel lands once the first sentence is audibly playing. A timed
        # sleep here (0.15 s against a 0.3 s player) landed in the second sentence
        # on a slow CI runner; waiting for the player's own mark cannot.
        started = env["tmp"] / "player-started"
        player = fake_player(env["tmp"], 2.0, started=started)

        def cancel_later():
            deadline = time.monotonic() + 10
            while not started.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            JOBS.request_cancel("job-x")

        th = threading.Thread(target=cancel_later)
        with patch.object(d, "detect_player", return_value=[str(player)]):
            th.start()
            r = play_sentences(SENTENCES, env["tmp"] / "m.wav", synth, job_id="job-x")
            th.join()
        assert r.outcome == "cancelled"
        assert r.index == 0
        assert JOBS.state("job-x") == "cancelled"


class TestUpdateLive:
    """JOBS.update_live: the daemon's "synthesizing" and "playing" marks never undo a /stop."""

    def test_cancelled_stays_cancelled(self):
        JOBS.create("ul-c", state="cancelled", position_ms=0)
        assert JOBS.update_live("ul-c", state="playing", started_at=1.0) is False
        job = JOBS.get("ul-c")
        assert job is not None and job["state"] == "cancelled" and "started_at" not in job

    @pytest.mark.parametrize("before", ["queued", "synthesizing", "playing", "paused"])
    def test_other_states_advance(self, before):
        JOBS.create("ul-o", state=before)
        assert JOBS.update_live("ul-o", state="playing", started_at=1.0) is True
        job = JOBS.get("ul-o")
        assert job is not None and job["state"] == "playing" and job["started_at"] == 1.0

    def test_no_job_is_not_a_cancel(self):
        assert JOBS.update_live(None, state="playing") is True
        assert JOBS.update_live("ul-missing", state="playing") is True


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
        player, starts = counting_player(env["tmp"], [0.05])
        th = pause_into_sentence(starts, 2, heard_s=0.3)
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
        with (
            patch.object(d, "detect_player", return_value=[str(player)]),
            patch.object(d, "save_speech_wav", lambda p, **kw: p),
        ):
            stream_message(self.msg(sentence_index=2), synth, start_index=2)
        assert synth.calls == [SENTENCES[2]]
        assert read_playback_state()["current_message"] is None

    def test_pause_near_end_of_last_sentence_skips_replay(self, env):
        # Paused 0.5 s or more into the last sentence: it was heard (past
        # BARELY_STARTED_S) and at most its 1 s WAV is left, under NEAR_END_THRESHOLD.
        player, starts = counting_player(env["tmp"], [0.05, 0.05])
        th = pause_into_sentence(starts, 3, heard_s=0.5)
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
        plays = Plays()
        plays.after[2] = lambda: write_playback_state(paused=True, paused_by="user")
        # Sentence 2 finishes synthesizing the moment the pause is set, so the
        # stream may find it ready before it looks again; the pause must hold it
        # either way. The stream used to start it, kill it at once and drop the
        # message as "near its end".
        held = lambda: read_playback_state().get("paused") and plays.finished == 2  # noqa: E731
        synth = Synth(hooks={2: lambda: wait_until(held)})
        with (
            patch.object(d, "detect_player", return_value=[str(player)]),
            patch.object(d, "daemon_play_audio", plays),
        ):
            stream_message(self.msg(), synth)
        cur = read_playback_state()["current_message"]
        assert cur is not None and cur["sentence_index"] == 2
        assert "skipping replay" not in env["log"].read_text()

    def test_pause_before_the_last_player_starts_is_not_skipped(self, env):
        # The pause lands after the stream's last look at sentence 2 and before
        # its player starts (here: while the stream reads the part's length). It
        # used to kill the player at 0 s, report it cut with the whole 1 s left,
        # and drop the message as near its end. Nothing of it was heard.
        player, starts = counting_player(env["tmp"], [0.05, 0.05])
        real_duration = d.get_wav_duration

        def duration_then_pause(path):
            if str(path).endswith("_s2.wav"):
                write_playback_state(paused=True, paused_by="mic")
            return real_duration(path)

        with (
            patch.object(d, "detect_player", return_value=[str(player)]),
            patch.object(d, "get_wav_duration", duration_then_pause),
        ):
            stream_message(self.msg(), Synth())
        cur = read_playback_state()["current_message"]
        assert cur is not None and cur["sentence_index"] == 2
        assert starts_seen(starts) == 2
        assert "skipping replay" not in env["log"].read_text()

    @pytest.mark.parametrize(("elapsed", "cut"), [(0.0, False), (0.05, False), (0.5, True)])
    def test_a_kill_before_anything_was_heard_is_not_a_cut(self, env, elapsed, cut):
        # A pause seen at the player's first poll kills it a few ms in. That play
        # was not heard, so it is no "near end" either.
        def play(path, *args, **kwargs):
            return (True, False, 0.0) if not str(path).endswith("_s2.wav") else (False, True, elapsed)

        with patch.object(d, "daemon_play_audio", play):
            r = play_sentences(SENTENCES, env["tmp"] / "m.wav", Synth())
        assert r.outcome == "paused" and r.index == 2
        assert r.cut_played is cut

    def test_stop_before_the_stream_starts_stands(self, env):
        # A /stop that cannot see the message as current (the daemon picked it but
        # is still in the speaker transition) flushes its queue file and marks the
        # job cancelled. The stream's "synthesizing" mark used to write over that,
        # and the whole message spoke.
        JOBS.create("job-t", source="page", state="queued")
        msg_file = env["tmp"] / "queued.json"
        msg_file.write_text("{}")
        JOBS.update("job-t", state="cancelled", position_ms=0)  # what flush_source does
        msg_file.unlink()
        synth = Synth()
        player, starts = counting_player(env["tmp"], [0.05])
        with patch.object(d, "detect_player", return_value=[str(player)]):
            stream_message(self.msg(id="job-t", source="page"), synth, msg_file=msg_file)
        assert synth.calls == []
        assert starts_seen(starts) == 0
        assert JOBS.state("job-t") == "cancelled"
        assert read_playback_state()["current_message"] is None

    def test_pause_carries_cumulative_listening_time(self, env):
        player, starts = counting_player(env["tmp"], [0.05])
        th = pause_into_sentence(starts, 2, heard_s=0.3)
        with patch.object(d, "detect_player", return_value=[str(player)]):
            th.start()
            stream_message(self.msg(played_s=5.0, sentence_index=0), Synth())
            th.join()
        cur = read_playback_state()["current_message"]
        # 5 s carried in, 1 s of sentence 0, at least 0.3 s of sentence 1.
        assert cur["played_s"] >= 6.3

    def test_queue_file_removed_only_after_settle(self, env):
        player = fake_player(env["tmp"], 0.05)
        msg_file = env["tmp"] / "queued.json"
        msg_file.write_text("{}")
        seen: list[bool] = []
        plays = Plays()
        plays.after[1] = lambda: seen.append(msg_file.exists())  # mid-message
        with (
            patch.object(d, "detect_player", return_value=[str(player)]),
            patch.object(d, "daemon_play_audio", plays),
            patch.object(d, "save_speech_wav", lambda p, **kw: p),
        ):
            stream_message(self.msg(), Synth(), msg_file=msg_file)
        assert seen == [True]
        assert not msg_file.exists()


class TestSpeechUnit:
    @pytest.mark.parametrize(
        ("raw", "want"),
        [
            ({}, "message"),
            ({"speech_unit": "sentence"}, "sentence"),
            ({"speech_unit": " Sentence "}, "sentence"),
            ({"speech_unit": "word"}, "message"),
            ({"speech_unit": 3}, "message"),
        ],
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

    def test_playback_heartbeats_on_the_clock_and_logs_rarely(self, env, monkeypatch):
        # Short intervals so the test is quick; the invariant is time-based, not poll-based.
        monkeypatch.setattr(d, "HEARTBEAT_INTERVAL_S", 0.2)
        monkeypatch.setattr(d, "PLAYING_LOG_INTERVAL_S", 0.5)
        wav = make_wav(env["tmp"] / "a.wav", 1.0)
        player = fake_player(env["tmp"], 1.2)
        hb = env["state"] / "daemon.heartbeat"
        with patch.object(d, "detect_player", return_value=[str(player)]):
            _, _, elapsed = d.daemon_play_audio(wav)
        text = env["log"].read_text()
        assert "Poll #" not in text
        # One per 0.5 s interval of however long the play took, not one per 50 ms
        # poll; a slow runner stretches the play, so the bound follows `elapsed`.
        assert 1 <= text.count("Still playing") <= int(elapsed / 0.5) + 1
        assert hb.exists()  # heartbeat refreshed during playback


class TestWholeMessageStop:
    """The one-piece path in daemon_loop: a /stop that lands before or during synthesis stands."""

    def run_loop_on(self, tmp_path, wrap: dict[str, Callable]):
        marker = tmp_path / "player-ran"
        state_dir, queue_dir, spoken, patches = loop_harness(tmp_path, {}, player_marker=marker)
        for name, make in wrap.items():
            patches.append(patch.object(d, name, make(getattr(d, name))))
        for p in patches:
            p.start()
        try:
            write_playback_state(paused=False, paused_by=None, audio_pid=None, current_message=None)
            _, msg = mq.write_message(
                {"session_id": "s", "project": "p", "text": "Stop me.", "source": "page"}
            )
            JOBS.create(msg["id"], source="page", state="queued")
            runner = start_loop()
            try:
                wait_until(lambda: "before speaking" in (state_dir / "daemon.log").read_text()
                           if (state_dir / "daemon.log").exists() else False)
            finally:
                stop_loop(runner)
            state = read_playback_state()
        finally:
            for p in patches:
                p.stop()
            write_playback_state(paused=False, paused_by=None, current_message=None)
        return msg["id"], spoken, marker, state

    def test_stop_between_pick_and_synthesis(self, tmp_path):
        def make(real):
            def prepare_then_stop(msg, raw):
                prepared = real(msg, raw)
                flush_source("page")  # the /stop; the message is not current yet
                return prepared

            return prepare_then_stop

        job, spoken, marker, state = self.run_loop_on(tmp_path, {"prepare_message": make})
        assert spoken == []  # used to be synthesized and spoken whole
        assert not marker.exists()
        assert JOBS.state(job) == "cancelled"

    def test_stop_then_pause_during_synthesis_does_not_revive(self, tmp_path):
        def make(real):
            def synth_then_stop_and_pause(p):
                result = real(p)
                flush_source("page")
                write_playback_state(paused=True, paused_by="user")
                return result

            return synth_then_stop_and_pause

        job, spoken, marker, state = self.run_loop_on(
            tmp_path, {"synthesize_prepared": make}
        )
        assert spoken == ["Stop me."]
        assert not marker.exists()
        # The pause used to record it as the interrupted message, to replay on unpause.
        assert state["current_message"] is None
        assert JOBS.state(job) == "cancelled"
