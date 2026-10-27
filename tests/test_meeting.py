"""The meeting hold: while a call app has the microphone, no voice plays (meeting.py)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import claude_code_tts.daemon as d
import claude_code_tts.meeting as meeting
import claude_code_tts.state as st
from claude_code_tts.bridge import pause_view
from claude_code_tts.meeting import MeetingWatch, describe
from claude_code_tts.msgqueue import NOW_LANE
from claude_code_tts.state import hold, in_meeting, may_play, release, set_meeting
from tests import loop_passes
from tests.test_hold import _enqueue, _ledger_helpers, _wait_for


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _watch(seen: list, clock: _Clock | None = None, **kw):
    """A watch whose probe answers from `seen` (the last answer repeats) and whose writes are recorded."""
    writes: list = []
    answers = list(seen)

    def probe():
        return answers.pop(0) if len(answers) > 1 else answers[0]

    w = MeetingWatch(
        log_fn=MagicMock(),
        write_meeting=writes.append,
        probe=probe,
        platform=lambda: "macos",
        release_after_s=10.0,
        clock=clock or _Clock(),
        **kw,
    )
    return w, writes, answers


class TestWatch:
    def test_a_call_holds_at_once_and_ends_only_after_the_mic_is_free_for_release_after_s(self):
        clock = _Clock()
        w, writes, answers = _watch([["us.zoom.xos"]], clock)
        assert w.poll_once() == ["us.zoom.xos"]
        assert writes == [["us.zoom.xos"]]
        answers[:] = [[]]
        clock.t += 9.0
        w.poll_once()
        assert writes == [["us.zoom.xos"]], "a mic free for under release_after_s ends nothing"
        clock.t += 1.0
        assert w.poll_once() == []
        assert writes == [["us.zoom.xos"], None]

    def test_the_mic_back_inside_release_after_s_keeps_the_hold(self):
        clock = _Clock()
        w, writes, answers = _watch([["us.zoom.xos"]], clock)
        w.poll_once()
        answers[:] = [[]]
        clock.t += 8.0
        w.poll_once()
        answers[:] = [["us.zoom.xos"]]
        w.poll_once()
        answers[:] = [[]]
        clock.t += 8.0
        w.poll_once()
        assert writes == [["us.zoom.xos"]], "a mute that closed the mic for a moment is still the call"

    def test_an_ignored_app_holds_nothing(self):
        w, writes, _ = _watch([["com.pais.handy"]], ignore=lambda: ["com.pais.handy"])
        assert w.poll_once() == []
        assert writes == []

    def test_an_ignored_app_beside_a_call_is_left_out_of_the_hold(self):
        w, writes, _ = _watch([["com.pais.handy", "us.zoom.xos"]], ignore=lambda: ["com.pais.handy"])
        w.poll_once()
        assert writes == [["us.zoom.xos"]]

    def test_a_probe_that_cannot_see_keeps_the_hold(self):
        clock = _Clock()
        w, writes, answers = _watch([["us.zoom.xos"]], clock)
        w.poll_once()
        answers[:] = [None]
        clock.t += 600.0
        assert w.poll_once() == ["us.zoom.xos"]
        assert writes == [["us.zoom.xos"]]
        warn = [c for c in w._log.call_args_list if c.args[1] == "WARN"]
        w.poll_once()
        assert len([c for c in w._log.call_args_list if c.args[1] == "WARN"]) == len(warn) == 1, "one WARN per blind stretch"

    def test_a_hold_carried_from_the_last_daemon_stands_until_the_mic_is_seen_free(self):
        clock = _Clock()
        w, writes, answers = _watch([[]], clock, carried=["us.zoom.xos"])
        w.poll_once()
        assert writes == [] and w.apps == ["us.zoom.xos"]
        clock.t += 10.0
        w.poll_once()
        assert writes == [None]

    def test_the_watch_never_clears_a_hold_it_did_not_set(self):
        w, writes, _ = _watch([[]])
        w.poll_once()
        assert writes == []

    def test_a_probe_that_outlives_stop_writes_nothing(self):
        w, writes, _ = _watch([["us.zoom.xos"]])
        w._stop.set()
        w.poll_once()
        assert writes == []

    def test_start_polls_on_the_callers_thread_before_it_returns(self):
        w, writes, _ = _watch([["us.zoom.xos"]])
        try:
            assert w.start()
            assert writes[0] == ["us.zoom.xos"], "the start's own poll holds before the daemon says a word"
        finally:
            w.stop()

    def _cli(self, monkeypatch, raw: dict, *argv: str) -> int:
        from claude_code_tts import cli

        monkeypatch.setattr(cli, "load_raw_config", lambda: raw)
        monkeypatch.setattr("sys.argv", ["claude-tts", "meeting", *argv])
        with pytest.raises(SystemExit) as e:
            cli.main()
        return int(e.value.code or 0)

    def test_claude_tts_meeting_says_it_cannot_see_off_macos_and_exits_1(self, capsys, monkeypatch):
        import claude_code_tts.meeting as m

        monkeypatch.setattr(m, "detect_platform", lambda: "linux")
        assert self._cli(monkeypatch, {}) == 1
        out = capsys.readouterr().out
        assert "Meeting hold: off; ignoring com.pais.handy" in out and "cannot see: not macOS" in out

    def test_claude_tts_meeting_names_what_it_would_hold_for_against_the_configs_ignore(self, capsys, monkeypatch):
        import claude_code_tts.meeting as m

        monkeypatch.setattr(m, "apps_using_input", lambda: ["com.pais.handy", "us.zoom.xos"])
        assert self._cli(monkeypatch, {"meeting_hold": True}) == 0
        assert "would hold: yes, for us.zoom.xos" in capsys.readouterr().out
        assert self._cli(monkeypatch, {"meeting_ignore": ["us.zoom.xos"]}) == 0
        assert "would hold: yes, for com.pais.handy" in capsys.readouterr().out

    def test_nothing_starts_off_macos(self):
        w = MeetingWatch(log_fn=MagicMock(), write_meeting=MagicMock(), probe=MagicMock(), platform=lambda: "linux")
        assert not w.start()
        w._probe.assert_not_called()


@pytest.fixture
def state_file(tmp_path, monkeypatch):
    monkeypatch.setattr(st, "PLAYBACK_STATE_FILE", tmp_path / "playback.json")
    return tmp_path / "playback.json"


class TestState:
    def test_a_meeting_holds_everyone_and_a_room_let_through(self, state_file):
        """2026-10-26: 43 replies of the one room a 24-hour hold let through played into a recorded call."""
        hold(["tts"])
        set_meeting(["us.zoom.xos"])
        state = st.read_playback_state()
        assert in_meeting(state)
        assert not may_play(state, "alice--claude--claude-code-tts")
        assert not may_play(state, "alice--claude--other")

    def test_only_a_click_made_during_the_call_plays_in_it(self, state_file):
        hold()
        set_meeting(["us.zoom.xos"])
        state = st.read_playback_state()
        began = state["meeting"]["since"]
        assert may_play(state, "page", NOW_LANE, queued_at=began + 1), "the person chose to be heard in the call"
        assert not may_play(state, "page", NOW_LANE, queued_at=began - 1), "a click from before the call waits for it"
        assert not may_play(state, "page", "", queued_at=began + 1), "a page's queued read is not a click"

    def test_a_meeting_holds_with_no_pause_at_all(self, state_file):
        set_meeting(["us.zoom.xos"])
        state = st.read_playback_state()
        assert not state.get("paused")
        assert st.held(state) and not may_play(state, "alice--claude--a")

    def test_the_kraken_leaves_the_meeting_and_the_meeting_leaves_the_hold(self, state_file):
        hold(["tts"])
        set_meeting(["us.zoom.xos"])
        release()
        assert in_meeting(st.read_playback_state()), "a release is not the call ending"
        hold(["tts"])
        set_meeting(None)
        state = st.read_playback_state()
        assert not in_meeting(state) and state["paused"] and state["let_through"] == ["tts"]
        assert may_play(state, "alice--claude--claude-code-tts")

    def test_since_is_kept_while_the_apps_change(self, state_file):
        set_meeting(["us.zoom.xos"])
        since = st.read_playback_state()["meeting"]["since"]
        set_meeting(["com.granola.app", "us.zoom.xos"])
        assert st.read_playback_state()["meeting"] == {"apps": ["com.granola.app", "us.zoom.xos"], "since": since}

    def test_the_bridge_and_the_status_line_show_it(self, state_file):
        set_meeting(["us.zoom.xos"])
        state = st.read_playback_state()
        view = pause_view(state)
        assert view["meeting"]["apps"] == ["us.zoom.xos"] and view["meeting"]["since"].endswith("Z")
        assert view["paused"] is False
        assert describe(state["meeting"], now=state["meeting"]["since"] + 125) == "us.zoom.xos has the microphone, for 2m"
        set_meeting(None)
        assert pause_view(st.read_playback_state())["meeting"] is None
        assert describe(None) == ""


@pytest.mark.skipif(shutil.which("jq") is None, reason="play-sound.sh reads its files with jq")
def test_the_sound_hook_plays_nothing_during_a_meeting(tmp_path):
    """Chimes go out the same speakers a call's recorder takes: one gate for every sound."""
    home = tmp_path / "home"
    (home / ".claude-tts").mkdir(parents=True)
    sound = tmp_path / "ding.wav"
    sound.write_bytes(b"RIFF")
    (home / ".claude-tts" / "config.json").write_text(json.dumps({"sounds": {"events": {"thinking": str(sound)}}}))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for player in ("afplay", "paplay"):
        fake = bin_dir / player
        fake.write_text(f'#!/bin/bash\ntouch "{tmp_path}/played-$PLAY_TAG"\n')
        fake.chmod(0o755)
    hook = Path(__file__).parent.parent / "hooks" / "play-sound.sh"

    def run(tag: str) -> int:
        env = {**os.environ, "HOME": str(home), "PATH": f"{bin_dir}:{os.environ['PATH']}", "PLAY_TAG": tag}
        return subprocess.run(["bash", str(hook), "thinking"], env=env, timeout=30).returncode

    playback = home / ".claude-tts" / "playback.json"
    playback.write_text(json.dumps({"paused": False, "meeting": {"apps": ["us.zoom.xos"], "since": 1.0}}))
    assert run("in-call") == 0
    playback.write_text(json.dumps({"paused": False}))
    assert run("no-call") == 0
    assert _wait_for(lambda: (tmp_path / "played-no-call").exists()), "the control: with no call the sound plays"
    assert not (tmp_path / "played-in-call").exists(), "a sound during a call is a sound in its recording"


def _run_loop(tmp_path, raw_config):
    loop_harness, stop_loop = _ledger_helpers()
    state_dir, queue_dir, spoken, patches = loop_harness(tmp_path, raw_config)
    for p in patches:
        p.start()

    def run():
        d._shutdown_requested = False
        d._daemon_mode = True
        d.daemon_loop()

    def stop(runner):
        stop_loop(runner)
        for p in reversed(patches):
            p.stop()

    return state_dir, queue_dir, spoken, run, stop


class TestLoop:
    def test_planted_call_holds_a_test_utterance_and_its_end_plays_it(self, tmp_path):
        """The planted control: a fake "in call" probe on the playpen bus must hold what is queued."""
        state_dir, queue_dir, spoken, run, stop = _run_loop(tmp_path, {"meeting_hold": False})
        runner = None
        try:
            st.write_playback_state(paused=False, paused_by=None, audio_pid=None, current_message=None)
            runner = threading.Thread(target=run, daemon=True)
            runner.start()
            assert _wait_for(lambda: (state_dir / "daemon.heartbeat").exists()), "loop never reached its idle pass"
            clock = _Clock()
            in_call = [["us.zoom.xos"]]
            watch = MeetingWatch(
                log_fn=d.log, write_meeting=set_meeting, probe=lambda: in_call[0],
                platform=lambda: "macos", release_after_s=10.0, clock=clock,
            )
            watch.poll_once()
            hold(["b"])
            _enqueue(queue_dir, "a waits for the call", "alice--claude--a")
            _enqueue(queue_dir, "b waits for the call too", "alice--claude--b")
            assert loop_passes.wait_passes(3), "the loop stopped passing"
            assert spoken == [], "nothing may play while a call has the microphone, not even a room let through"
            in_call[0] = []
            clock.t += 10.0
            watch.poll_once()
            assert _wait_for(lambda: "b waits for the call too" in spoken), "the let-through room plays after the call"
            assert loop_passes.wait_passes(3), "the loop stopped passing"
            assert "a waits for the call" not in spoken, "the person's own hold still stands"
            release()
            assert _wait_for(lambda: "a waits for the call" in spoken)
        finally:
            if runner is not None:
                stop(runner)
        log = (state_dir / "daemon.log").read_text()
        assert "Meeting: us.zoom.xos has the microphone" in log and "Meeting over" in log

    def test_a_call_with_no_hold_of_a_person_holds_the_whole_queue(self, tmp_path):
        state_dir, queue_dir, spoken, run, stop = _run_loop(tmp_path, {"meeting_hold": False})
        runner = None
        try:
            st.write_playback_state(paused=False, paused_by=None, audio_pid=None, current_message=None)
            runner = threading.Thread(target=run, daemon=True)
            runner.start()
            assert _wait_for(lambda: (state_dir / "daemon.heartbeat").exists()), "loop never reached its idle pass"
            set_meeting(["us.zoom.xos"])
            assert loop_passes.wait_passes(3), "the loop stopped passing"
            # Older than max_age (300 s): a held queue ages nothing, an unheld one expires it.
            held_reply = _enqueue(queue_dir, "waits for the call", "alice--claude--a", ago=400)
            assert loop_passes.wait_passes(3), "the loop stopped passing"
            assert spoken == [] and held_reply.exists(), "a call holds the queue with nobody's hand on it, and ages nothing"
            set_meeting(None)
            # Its 400 s by the clock are past max_age once the queue is unheld; the release is the point here.
            assert _wait_for(lambda: "Resumed with 1 message(s) waiting" in (state_dir / "daemon.log").read_text())
        finally:
            if runner is not None:
                stop(runner)
        assert "Paused by meeting; holding the queue" in (state_dir / "daemon.log").read_text()

    def test_the_hold_is_off_until_config_turns_it_on(self, tmp_path, monkeypatch):
        """9.82.0 ships the probe ahead of the hold: a real call is measured before anything is held."""
        started = []
        monkeypatch.setattr(d.MeetingWatch, "start", lambda self: started.append(self) or True)
        monkeypatch.setattr(d.MeetingWatch, "stop", lambda self: None)
        for raw, want in (({}, 0), ({"meeting_hold": True}, 1)):
            run_dir = tmp_path / f"run-{want}"
            run_dir.mkdir()
            state_dir, queue_dir, spoken, run, stop = _run_loop(run_dir, raw)
            runner = threading.Thread(target=run, daemon=True)
            try:
                runner.start()
                beat = state_dir / "daemon.heartbeat"
                assert _wait_for(beat.exists), "loop never reached its idle pass"
            finally:
                stop(runner)
            assert len(started) == want
            assert ("Meeting hold off" in (state_dir / "daemon.log").read_text()) == (want == 0)

    def test_a_meeting_left_by_a_daemon_with_no_watch_is_released_at_start(self, tmp_path):
        state_dir, queue_dir, spoken, run, stop = _run_loop(tmp_path, {"meeting_hold": False})
        runner = None
        try:
            st.write_playback_state(paused=False, paused_by=None, audio_pid=None, current_message=None)
            set_meeting(["us.zoom.xos"])
            _enqueue(queue_dir, "plays", "alice--claude--a")
            runner = threading.Thread(target=run, daemon=True)
            runner.start()
            assert _wait_for(lambda: "plays" in spoken)
        finally:
            if runner is not None:
                stop(runner)
        assert "Released the previous daemon's meeting hold" in (state_dir / "daemon.log").read_text()


@pytest.mark.skipif(sys.platform != "darwin", reason="the real Core Audio calls; CI's macos-latest job runs them")
class TestRealCoreAudio:
    """The selectors were checked against published bindings only; here macOS answers them."""

    def test_the_probe_gets_an_answer_from_core_audio(self) -> None:
        assert meeting.apps_using_input() is not None, meeting.last_error()

    def test_every_process_answers_the_input_and_pid_selectors(self) -> None:
        ca = meeting._CoreAudio()
        procs = ca.processes()
        if not procs:
            pytest.skip("Core Audio lists no process on this machine, so piri and ppid cannot be asked")
        for obj in procs:
            assert ca.u32(obj, meeting._PROCESS_IS_RUNNING_INPUT) in (0, 1)
            assert ca.pid(obj) > 0
