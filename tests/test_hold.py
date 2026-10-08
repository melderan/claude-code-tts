"""The selective hold (JMO 2026-10-02): hold everyone, let named rooms through, the kraken releases all."""

from __future__ import annotations

import argparse
import json
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import claude_code_tts.daemon as d
import claude_code_tts.state as st
from claude_code_tts.mic_watcher import MicWatcher
from claude_code_tts.state import hold, let_rooms, lets_through, may_play, release, room_tag


class TestRoomKey:
    @pytest.mark.parametrize(
        "sid, tag",
        [
            ("alice--claude--claude-code-tts", "tts"),
            ("alice--claude--notes", "notes"),
            ("bob--claude--k8s", "k8s"),
            ("plain", "plain"),
            ("claude-code-x", "x"),
        ],
    )
    def test_room_tag(self, sid, tag):
        assert room_tag(sid) == tag

    def test_lets_through_by_tag_full_id_or_last_segment(self):
        sid = "alice--claude--claude-code-tts"
        assert lets_through(sid, ["tts"])
        assert lets_through(sid, ["claude-code-tts"])
        assert lets_through(sid, [sid])
        assert not lets_through(sid, ["jmo", "notes"])
        assert not lets_through(sid, [])
        assert not lets_through(sid, None)


class TestMayPlay:
    def test_not_paused_everyone_plays(self):
        assert may_play({"paused": False}, "a--b--c")

    def test_a_mic_hold_holds_everyone_even_the_let_through(self):
        assert not may_play({"paused": True, "paused_by": "mic", "let_through": ["c"]}, "a--b--c")

    def test_a_hand_hold_lets_only_the_list_through(self):
        state = {"paused": True, "paused_by": "user", "let_through": ["c"]}
        assert may_play(state, "a--b--c")
        assert not may_play(state, "a--b--d")
        assert not may_play({"paused": True, "paused_by": "user"}, "a--b--c")

    def test_a_recording_under_a_hand_hold_holds_the_let_through_too(self):
        state = {"paused": True, "paused_by": "user", "let_through": ["c"], "mic_held": True}
        assert not may_play(state, "a--b--c")


@pytest.fixture
def state_file(tmp_path, monkeypatch):
    f = tmp_path / "playback.json"
    monkeypatch.setattr(st, "PLAYBACK_STATE_FILE", f)
    return f


class TestHoldAndRelease:
    def test_hold_holds_everyone_and_names_the_let_through(self, state_file):
        s = hold(["tts", "jmo"])
        assert s["paused"] is True and s["paused_by"] == "user" and s["let_through"] == ["tts", "jmo"]
        assert "paused_since" in s and "mic_held" not in s

    def test_bare_hold_clears_the_list(self, state_file):
        hold(["tts"])
        assert hold()["paused"] is True
        assert "let_through" not in st.read_playback_state()

    def test_let_rooms_adds_to_a_hold_and_holds_first_when_not_held(self, state_file):
        s = let_rooms(["tts"])
        assert s["paused"] and s["let_through"] == ["tts"]
        s = let_rooms(["jmo", "tts"])
        assert s["let_through"] == ["tts", "jmo"], "added once, order kept"

    def test_a_hold_over_a_mic_pause_keeps_the_recording(self, state_file):
        st.write_playback_state(paused=True, paused_by="mic")
        s = hold(["tts"])
        assert s["paused_by"] == "user" and s["mic_held"] is True
        assert not may_play(s, "x--claude-code-tts")

    def test_release_clears_everything(self, state_file):
        hold(["tts"])
        st.write_playback_state(mic_held=True)
        s = release()
        assert s["paused"] is False and s.get("paused_by") is None
        assert "let_through" not in s and "mic_held" not in s and "paused_since" not in s

    def test_set_paused_false_also_clears_the_list(self, state_file):
        hold(["tts"])
        st.set_paused(False)
        assert "let_through" not in st.read_playback_state()


class TestMicUnderAHandHold:
    def _watcher(self, tmp_path, monkeypatch, state):
        log_file = tmp_path / "handy.log"
        log_file.write_text("")
        monkeypatch.setattr("claude_code_tts.mic_watcher.HANDY_LOG", log_file)
        rec = tmp_path / "recordings"
        rec.mkdir()

        def write_state(**kwargs):
            state.update(kwargs)

        return MicWatcher(
            log_fn=MagicMock(),
            read_playback_state=lambda: dict(state),
            write_playback_state=write_state,
            resume_delay_ms=50,
            recordings_dir=rec,
        ), log_file

    def test_a_recording_flags_the_hold_and_its_end_clears_the_flag(self, tmp_path, monkeypatch):
        state = {"paused": True, "paused_by": "user", "let_through": ["tts"]}
        w, log_file = self._watcher(tmp_path, monkeypatch, state)
        w.start()
        try:
            time.sleep(0.1)
            with open(log_file, "a") as f:
                f.write("[DEBUG] TranscribeAction::start called for binding: transcribe\n")
            time.sleep(0.2)
            assert state["paused_by"] == "user" and state.get("mic_held") is True
            assert not may_play(state, "x--claude-code-tts")
            with open(log_file, "a") as f:
                f.write("[DEBUG] TranscribeAction::stop called for binding: transcribe\n")
            time.sleep(0.3)
            assert state["paused"] is True and state["paused_by"] == "user", "the hand hold stays"
            assert not state.get("mic_held")
            assert may_play(state, "x--claude-code-tts")
        finally:
            w.stop()


def _enqueue(queue_dir: Path, text: str, session_id: str) -> Path:
    ts = time.time()
    f = queue_dir / f"{ts:.6f}_{abs(hash(text)) % 10**8:08x}.json"
    tmp = f.with_suffix(".tmp")  # whole or not at all: the loop deletes a half-written file
    tmp.write_text(json.dumps({"id": f"{abs(hash(text)) % 10**8:08x}", "session_id": session_id,
                               "project": "p", "text": text, "timestamp": ts}))
    tmp.rename(f)
    return f


def _ledger_helpers():
    """loop_harness and stop_loop from test_pause_ledger, loaded by path (tests is not a package)."""
    import importlib.util

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


class TestLoopLetsRoomsThrough:
    def test_held_rooms_wait_let_through_rooms_play_and_the_kraken_frees_all(self, tmp_path):
        loop_harness, stop_loop = _ledger_helpers()

        state_dir, queue_dir, spoken, patches = loop_harness(tmp_path, {})
        for p in patches:
            p.start()
        try:
            st.write_playback_state(paused=False, paused_by=None, audio_pid=None, current_message=None)

            def run():
                d._shutdown_requested = False
                d._daemon_mode = True
                d.daemon_loop()

            runner = threading.Thread(target=run, daemon=True)
            runner.start()
            assert _wait_for(lambda: (state_dir / "daemon.heartbeat").exists()), "loop never reached its idle pass"
            time.sleep(0.1)

            hold(["b"])
            _enqueue(queue_dir, "a waits", "alice--claude--a")
            _enqueue(queue_dir, "b speaks", "alice--claude--b")
            assert _wait_for(lambda: "b speaks" in spoken)
            time.sleep(0.3)
            assert spoken == ["b speaks"], "a's message must wait while held"

            st.write_playback_state(mic_held=True)
            _enqueue(queue_dir, "b waits for the recording", "alice--claude--b")
            time.sleep(0.4)
            assert spoken == ["b speaks"], "a recording under the hold holds the let-through room too"
            st.write_playback_state(mic_held=False)
            assert _wait_for(lambda: "b waits for the recording" in spoken)
            assert "a waits" not in spoken

            release()
            assert _wait_for(lambda: "a waits" in spoken)
        finally:
            stop_loop(runner)
            for p in patches:
                p.stop()
        log = (state_dir / "daemon.log").read_text()
        assert "letting b through" in log
        assert "Resumed with" in log

    def test_a_held_rooms_interrupted_message_goes_back_to_the_queue(self, tmp_path):
        """Paused mid-sentence in room a, then b is let through: a's half message is not lost."""
        loop_harness, stop_loop = _ledger_helpers()

        state_dir, queue_dir, spoken, patches = loop_harness(tmp_path, {})
        for p in patches:
            p.start()
        try:
            st.write_playback_state(paused=False, paused_by=None, audio_pid=None, current_message=None)

            def run():
                d._shutdown_requested = False
                d._daemon_mode = True
                d.daemon_loop()

            runner = threading.Thread(target=run, daemon=True)
            runner.start()
            assert _wait_for(lambda: (state_dir / "daemon.heartbeat").exists())
            time.sleep(0.1)
            # A hand hold with a's message on deck, as if paused mid-play.
            hold(["b"])
            st.write_playback_state(current_message={
                "id": "aaaa0001", "session_id": "alice--claude--a", "project": "p", "text": "a was cut off",
                "timestamp": time.time() - 5, "audio_position": 1.0,
            })
            _enqueue(queue_dir, "b speaks", "alice--claude--b")
            assert _wait_for(lambda: "b speaks" in spoken)
            assert "a was cut off" not in spoken
            files = sorted(queue_dir.glob("*.json"))
            assert any("aaaa0001" in f.name for f in files), "a's message is back in the queue under its id"
            release()
            assert _wait_for(lambda: "a was cut off" in spoken)
        finally:
            stop_loop(runner)
            for p in patches:
                p.stop()
        assert "put back in the queue" in (state_dir / "daemon.log").read_text()


class TestCli:
    def test_hold_me_then_let_another_then_kraken(self, state_file, monkeypatch, capsys):
        from claude_code_tts import cli

        monkeypatch.setattr(cli, "get_session_id", lambda: "alice--claude--claude-code-tts")
        cli.cmd_hold(argparse.Namespace(let=None, me=True))
        s = st.read_playback_state()
        assert s["paused"] and s["let_through"] == ["alice--claude--claude-code-tts"]
        assert "Letting through: tts" in capsys.readouterr().out
        cli.cmd_hold(argparse.Namespace(let=["jmo"], me=False))
        assert st.read_playback_state()["let_through"] == ["alice--claude--claude-code-tts", "jmo"]
        cli.cmd_hold(argparse.Namespace(let=None, me=False))
        assert "let_through" not in st.read_playback_state()
        assert "Nobody is let through" in capsys.readouterr().out
        cli.cmd_kraken(argparse.Namespace())
        assert st.read_playback_state()["paused"] is False
        assert "kraken is released" in capsys.readouterr().out

    def test_status_names_the_let_through(self, state_file, monkeypatch, capsys):
        from claude_code_tts import cli

        hold(["jmo"])
        st.write_playback_state(mic_held=True)
        pb = st.read_playback_state()
        with patch.object(cli, "get_session_id", lambda: "x"), patch("claude_code_tts.state.read_playback_state", lambda: pb):
            try:
                cli.cmd_status(argparse.Namespace())
            except Exception:  # noqa: BLE001  config may be missing in a bare test env; the lines we need print first
                pass
        out = capsys.readouterr().out
        assert "Paused:   true (by user, recording)" in out
        assert "Letting through: jmo" in out
