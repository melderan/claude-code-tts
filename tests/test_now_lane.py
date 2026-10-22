"""Lane "now": a block a person clicked to hear plays first, cutting off a room mid-message.

The room's message resumes where it was cut once no now block is left (JMO 2026-10-13: "a
click on hear is where his eyes are, so it speaks FIRST"). A click plays through a person's
hold set before it; the pause hotkey during that read stops only the read (JMO, D112 B). A
hold begun during the read, and a recording, hold it.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import claude_code_tts.daemon as d
import claude_code_tts.msgqueue as mq
import claude_code_tts.state as st
from claude_code_tts.bridge import JOBS, write_bridge_message
from claude_code_tts.state import hold, may_play
from tests import loop_passes


def _m(ts: float, **extra: object) -> dict:
    return {"timestamp": ts, "text": f"t{ts}", "_file": Path(f"{ts}.json"), **extra}


def test_now_goes_before_everything_and_keeps_its_own_order() -> None:
    msgs = [_m(1.0, type="control"), _m(2.0, lane="background"), _m(3.0), _m(4.0, lane="now"), _m(5.0, lane="now")]
    assert [x["timestamp"] for x in mq.play_order(msgs)] == [4.0, 5.0, 1.0, 3.0, 2.0]


class TestMayPlay:
    held = {"paused": True, "paused_by": "user", "paused_since": 100.0}

    def test_a_click_after_a_persons_hold_plays(self) -> None:
        assert may_play(self.held, "browser", "now", 101.0)

    def test_a_hold_begun_during_the_read_holds_it(self) -> None:
        assert not may_play(self.held, "browser", "now", 99.0)

    def test_a_recording_holds_it(self) -> None:
        assert not may_play({**self.held, "paused_by": "mic"}, "browser", "now", 101.0)
        assert not may_play({**self.held, "mic_held": True}, "browser", "now", 101.0)

    def test_other_lanes_and_a_missing_stamp_wait_as_before(self) -> None:
        assert not may_play(self.held, "browser", "background", 101.0)
        assert not may_play(self.held, "browser", "now", None)
        assert not may_play(self.held, "browser", "now", True)


class TestClickReadThroughHold:
    held = {"paused": True, "paused_by": "user", "paused_since": 100.0}
    read = {"id": "j1", "session_id": "browser", "lane": "now", "timestamp": 101.0}

    def test_names_the_click_the_hold_lets_play(self) -> None:
        assert st.click_read_through_hold({**self.held, "current_message": self.read}) == "j1"

    def test_none_without_a_persons_hold(self) -> None:
        assert st.click_read_through_hold({"paused": False, "current_message": self.read}) is None
        assert st.click_read_through_hold({**self.held, "paused_by": "mic", "current_message": self.read}) is None

    def test_none_for_a_click_the_hold_holds_or_a_room(self) -> None:
        assert st.click_read_through_hold({**self.held, "current_message": {**self.read, "timestamp": 99.0}}) is None
        assert st.click_read_through_hold({**self.held, "current_message": {**self.read, "lane": ""}}) is None
        assert st.click_read_through_hold(self.held) is None


def _harness():
    spec = importlib.util.spec_from_file_location("test_pause_ledger", Path(__file__).parent / "test_pause_ledger.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod.loop_harness, mod.stop_loop


def _wait_for(pred, timeout=5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def _enqueue_room(queue_dir: Path, text: str, session_id: str) -> None:
    ts = time.time()
    msg_id = f"{abs(hash(text)) % 10**8:08x}"
    f = queue_dir / f"{ts:.6f}_{msg_id}.json"
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps({"id": msg_id, "session_id": session_id, "project": session_id, "text": text, "timestamp": ts}))
    tmp.rename(f)


def _click(text: str) -> dict:
    """What the bridge does for POST /speak with lane "now"."""
    msg = write_bridge_message(text, persona="claude-prime", persona_config={}, source="hear", lane="now")
    d.request_preempt()
    return msg


def _run_loop(tmp_path: Path, player_body: str, raw_config: dict | None = None, click_s: float = 0.1):
    loop_harness, stop_loop = _harness()
    state_dir, queue_dir, spoken, patches = loop_harness(tmp_path, raw_config or {})
    player = tmp_path / "fake-player"
    player.write_text(f"#!/bin/bash\n{player_body}\n")
    player.chmod(0o755)
    def generate(text, persona, output_file, **kw):
        # Room speech is 6 s of WAV, so a cut early in it is far from the near-end skip
        # (NEAR_END_THRESHOLD) and the resume really plays; a click is click_s long,
        # a short block unless the test cuts it and needs it resumed.
        spoken.append(text)
        import wave

        with wave.open(str(output_file), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(8000)
            w.writeframes(b"\x00\x00" * (int(8000 * click_s) if "clicked" in text else 48000))
        return True

    patches.append(patch.object(d, "daemon_generate_speech", side_effect=generate))
    for p in patches:
        p.start()
    JOBS._jobs.clear()
    JOBS._cancel.clear()
    st.write_playback_state(paused=False, paused_by=None, audio_pid=None, current_message=None)
    d._PREEMPT.update(asked=0, settled=0)

    def run() -> None:
        d._shutdown_requested = False
        d._daemon_mode = True
        d.daemon_loop()

    runner = threading.Thread(target=run, daemon=True)
    runner.start()
    assert _wait_for(lambda: (state_dir / "daemon.heartbeat").exists()), "loop never reached its idle pass"

    def stop() -> None:
        stop_loop(runner)
        for p in reversed(patches):
            p.stop()
        d._PREEMPT.update(asked=0, settled=0)

    return state_dir, queue_dir, spoken, stop


@pytest.mark.parametrize("unit", ["message", "sentence"])
def test_a_click_cuts_a_room_off_and_the_room_resumes_after(tmp_path: Path, unit: str) -> None:
    playing = tmp_path / "room-playing"
    # The room's audio runs long; the page's is short. The WAV name carries the session.
    body = f'case "$*" in *browser*) sleep 0.05 ;; *) echo "$*" >> {playing}; sleep 1 ;; esac'
    state_dir, queue_dir, spoken, stop = _run_loop(tmp_path, body, {"speech_unit": unit})
    try:
        _enqueue_room(queue_dir, "a long room reply", "alice--claude--a")
        assert _wait_for(lambda: playing.exists()), "the room never started playing"
        started = time.monotonic()
        _click("what JMO clicked")
        assert _wait_for(lambda: "what JMO clicked" in spoken)
        assert time.monotonic() - started < 1.5, "the click waited for the room's message to end"
        assert _wait_for(lambda: len(playing.read_text().splitlines()) == 2), "the room's message never played again"
        assert spoken == ["a long room reply", "what JMO clicked", "a long room reply"]
        assert not st.read_playback_state().get("preempted_message")
    finally:
        stop()
    log = (state_dir / "daemon.log").read_text()
    assert "Skipping replay" not in log
    if unit == "sentence":
        assert "Message interrupted at sentence 1/1" in log
    else:
        assert "Replaying interrupted message from start" in log or "Resuming from" in log
    assert "Cut off alice--claude--a for a now block" in log
    assert "Resuming alice--claude--a, cut off by a now block" in log
    assert not d._preempt_pending()


def test_a_click_plays_through_a_hold_set_before_it(tmp_path: Path) -> None:
    state_dir, queue_dir, spoken, stop = _run_loop(tmp_path, "sleep 0.05")
    try:
        hold([])
        time.sleep(0.05)
        _enqueue_room(queue_dir, "a room waits", "alice--claude--a")
        _click("what JMO clicked")
        assert _wait_for(lambda: "what JMO clicked" in spoken)
        assert loop_passes.wait_passes(3), "the loop stopped passing"
        assert spoken == ["what JMO clicked"], "the room must wait for the hold"
        assert st.read_playback_state()["paused"] is True
    finally:
        stop()


def _pause_hotkey() -> None:
    from claude_code_tts import cli

    with patch.object(cli, "_notify"):
        cli.cmd_pause(argparse.Namespace())


def test_the_hotkey_during_a_click_through_a_hold_stops_only_that_read(tmp_path: Path) -> None:
    """Review of 6c0187d: the toggle released every room to stop one read (JMO, D112 B)."""
    playing = tmp_path / "click-playing"
    body = f'case "$*" in *browser*) echo x >> {playing}; sleep 2 ;; *) sleep 0.05 ;; esac'
    state_dir, queue_dir, spoken, stop = _run_loop(tmp_path, body)
    try:
        hold([])
        time.sleep(0.05)
        _enqueue_room(queue_dir, "a room waits", "alice--claude--a")
        click = _click("what JMO clicked")
        assert _wait_for(lambda: playing.exists())
        _pause_hotkey()
        assert _wait_for(lambda: JOBS.state(click["id"]) == "cancelled")
        assert _wait_for(lambda: not st.read_playback_state().get("current_message"))
        assert loop_passes.wait_passes(3), "the loop stopped passing"
        state = st.read_playback_state()
        assert state["paused"] is True and state["paused_by"] == "user", "the hold stays"
        assert "stop_read" not in state
        assert spoken == ["what JMO clicked"], "the room waits and the read does not resume"
        _pause_hotkey()
        assert st.read_playback_state()["paused"] is False, "the next press releases as before"
        assert _wait_for(lambda: spoken == ["what JMO clicked", "a room waits"])
    finally:
        stop()
    assert "played through the hold; the hold stays" in (state_dir / "daemon.log").read_text()


def test_a_pause_during_a_click_stops_it_and_it_resumes_first(tmp_path: Path) -> None:
    playing = tmp_path / "click-playing"
    body = f'case "$*" in *browser*) echo x >> {playing}; sleep 1 ;; *) sleep 0.05 ;; esac'
    # The click is longer than NEAR_END_THRESHOLD, so a pause during it resumes it. With
    # 0.1 s of audio, a pause seen after BARELY_STARTED_S counted the click as played to
    # its end and dropped it: a slow macOS runner did, Linux did not.
    state_dir, queue_dir, spoken, stop = _run_loop(tmp_path, body, click_s=6.0)
    try:
        _click("what JMO clicked")
        assert _wait_for(lambda: playing.exists())
        # Land the pause late on every runner, the way a slow one does.
        time.sleep(0.2)
        st.set_paused(True)
        _enqueue_room(queue_dir, "a room waits", "alice--claude--a")
        assert _wait_for(lambda: st.read_playback_state().get("current_message", {}) and not st.read_playback_state().get("audio_pid"))
        assert loop_passes.wait_passes(3), "the loop stopped passing"
        assert len(playing.read_text().splitlines()) == 1, "the pause stopped the click"
        st.set_paused(False)
        assert _wait_for(lambda: spoken.count("what JMO clicked") == 2 and "a room waits" in spoken)
        assert spoken.index("a room waits") > 1, "the click resumes before the room"
    finally:
        stop()


def _cut_room_off(tmp_path: Path):
    """A room playing, a click cutting it off, and the loop holding it in preempted_message."""
    playing = tmp_path / "room-playing"
    body = f'case "$*" in *browser*) sleep 1.5 ;; *) echo "$*" >> {playing}; sleep 1 ;; esac'
    state_dir, queue_dir, spoken, stop = _run_loop(tmp_path, body)
    _enqueue_room(queue_dir, "a long room reply", "alice--claude--a")
    assert _wait_for(lambda: playing.exists())
    click = _click("what JMO clicked")
    assert _wait_for(lambda: bool(st.read_playback_state().get("preempted_message")))
    return state_dir, queue_dir, spoken, stop, click


def test_a_drop_drops_the_message_a_click_cut_off(tmp_path: Path) -> None:
    """Review of 6c0187d: kraken --drop and POST /queue/drop left it to resume after the click."""
    state_dir, queue_dir, spoken, stop, _ = _cut_room_off(tmp_path)
    try:
        mq.write_drop_all_message([str(m["id"]) for m in mq.scan() if m.get("type") != "control"])
        assert _wait_for(lambda: not st.read_playback_state().get("preempted_message"))
        assert loop_passes.wait_passes(3), "the loop stopped passing"
        assert spoken.count("a long room reply") == 1, f"a dropped message spoke again: {spoken}"
    finally:
        stop()
    assert "a now block cut off is dropped too" in (state_dir / "daemon.log").read_text()


def test_a_stale_cut_off_message_is_cleared_at_start(tmp_path: Path) -> None:
    """Review of 6c0187d: a cold start days later spoke the old cut-off reply."""
    loop_harness, stop_loop = _harness()
    state_dir, queue_dir, spoken, patches = loop_harness(tmp_path, {})
    for p in patches:
        p.start()
    try:
        old = {"id": "aaaa0001", "session_id": "alice--claude--a", "project": "p", "text": "days old",
               "audio_position": 1.0, "preempted_at": time.time() - 3 * 86400}
        st.write_playback_state(paused=False, audio_pid=None, current_message=None, preempted_message=old)

        def run() -> None:
            d._shutdown_requested = False
            d._daemon_mode = True
            d.daemon_loop()

        runner = threading.Thread(target=run, daemon=True)
        runner.start()
        try:
            assert _wait_for(lambda: (state_dir / "daemon.heartbeat").exists())
            assert loop_passes.wait_passes(3), "the loop stopped passing"
            assert spoken == []
            assert not st.read_playback_state().get("preempted_message")
        finally:
            stop_loop(runner)
    finally:
        for p in reversed(patches):
            p.stop()
    assert "preempted_message" in (state_dir / "daemon.log").read_text()
