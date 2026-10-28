"""A person's pause names its writer (2026-10-28).

On 2026-10-27 a "user" pause no one remembered writing kept JMO pressing Resume after every
recording: the mic watcher rightly leaves a person's pause alone, and nothing said which
process had written it. Every writer of a person's pause now leaves paused_from, and the
daemon's "Paused by" line, the mic watcher's "staying paused" line and status print it.
"""

from __future__ import annotations

import argparse
import os
import socket
from unittest.mock import MagicMock, patch

import pytest

import claude_code_tts.daemon as d
import claude_code_tts.state as st
from claude_code_tts.mic_watcher import MicWatcher
from tests import loop_passes
from tests.test_pause_ledger import loop_harness, start_loop, stop_loop, wait_for_idle_pass


@pytest.fixture
def state_file(tmp_path, monkeypatch):
    f = tmp_path / "playback.json"
    monkeypatch.setattr(st, "PLAYBACK_STATE_FILE", f)
    return f


def _pause_toggle() -> None:
    from claude_code_tts import cli

    with patch.object(cli, "_notify"):
        cli.cmd_pause(argparse.Namespace())


class TestPausedFrom:
    def test_the_pause_toggle_names_itself_its_pid_and_its_host(self, state_file, monkeypatch):
        monkeypatch.setenv("CLAUDE_TTS_SESSION", "alice--claude--notes")
        _pause_toggle()
        src = st.read_playback_state()["paused_from"]
        assert src["what"] == "claude-tts pause"
        assert src["pid"] == os.getpid()
        assert src["host"] == socket.gethostname()
        assert src["session"] == "alice--claude--notes"

    def test_the_chain_above_the_writer_is_named(self, state_file):
        # The process that ran claude-tts (a hotkey app, a shell under Claude) is the clue.
        src = st.pause_source("claude-tts pause")
        assert src.get("parents"), "ps found no process above the writer"
        assert all(isinstance(p, str) and p for p in src["parents"])

    def test_a_release_forgets_the_writer(self, state_file):
        _pause_toggle()
        _pause_toggle()
        assert "paused_from" not in st.read_playback_state()

    def test_a_hold_names_itself_and_a_let_over_it_names_the_new_writer(self, state_file):
        from claude_code_tts import cli

        cli.cmd_hold(argparse.Namespace(let=[], me=False, clear=True))
        assert st.read_playback_state()["paused_from"]["what"] == "claude-tts hold --clear"
        cli.cmd_hold(argparse.Namespace(let=["jmo"], me=False, clear=False))
        # let_rooms over a hold writes only the list; the hold's writer stays the one named.
        assert st.read_playback_state()["paused_from"]["what"] == "claude-tts hold --clear"

    def test_a_mic_pause_carries_no_person_writer(self, state_file):
        st.set_paused(True, "user", st.pause_source("claude-tts pause"))
        st.set_paused(False)
        st.write_playback_state(paused=True, paused_by="mic")
        assert "paused_from" not in st.read_playback_state()

    def test_the_bridge_names_itself_without_the_daemons_pid(self, state_file):
        st.set_paused(True, source=st.pause_source("bridge POST /pause", process=False))
        src = st.read_playback_state()["paused_from"]
        assert src["what"] == "bridge POST /pause" and "pid" not in src and "parents" not in src


class TestPauseOverAFocusHold:
    """Haley, 2026-10-28: `claude-tts pause` over a hold released it, twice, while JMO talked to Fox."""

    def test_the_toggle_leaves_a_focus_hold_and_names_the_kraken(self, state_file, capsys):
        st.hold(["jmo", "k8s"], st.pause_source("claude-tts hold --clear"))
        with pytest.raises(SystemExit) as exit_info:
            _pause_toggle()
        assert exit_info.value.code == 1
        after = st.read_playback_state()
        assert after["paused"] is True and after["let_through"] == ["jmo", "k8s"]
        err = capsys.readouterr().err
        assert "A hold is set, letting jmo, k8s through; nothing changed" in err
        assert "claude-tts kraken releases everyone" in err

    def test_a_hold_of_everyone_still_toggles_off(self, state_file):
        st.hold([])
        _pause_toggle()
        assert st.read_playback_state()["paused"] is False


class TestDescribe:
    def test_the_words(self):
        src = {"what": "claude-tts pause", "pid": 42, "parents": ["bash", "claude"], "host": "mac",
               "session": "a--b--c", "at": 0}
        assert st.describe_pause_source(src) == (
            "claude-tts pause, pid 42 under bash < claude, on mac, session a--b--c, at 00:00:00Z"
        )

    def test_nothing_to_say_is_empty(self):
        assert st.describe_pause_source(None) == ""
        assert st.describe_pause_source({"pid": 1}) == ""

    def test_the_daemon_note_says_when_an_old_writer_named_nothing(self):
        assert d.pause_source_note({"paused": True, "paused_by": "user"}) == (
            " (from a writer that named nothing: an older claude-tts)"
        )
        assert d.pause_source_note({"paused": True, "paused_by": "mic"}) == ""
        note = d.pause_source_note({"paused": True, "paused_by": "user", "paused_from": {"what": "claude-tts hold"}})
        assert note == " (from claude-tts hold)"


class TestMicWatcherNamesThePause:
    def test_staying_paused_names_the_writer(self):
        log = MagicMock()
        state = {"paused": True, "paused_by": "user", "paused_from": {"what": "claude-tts pause", "pid": 7}}
        w = MicWatcher(log_fn=log, read_playback_state=lambda: dict(state), write_playback_state=MagicMock())
        w._resume_after_mic()
        words = " ".join(str(c.args[0]) for c in log.call_args_list)
        assert "staying paused; that pause is from claude-tts pause, pid 7" in words

    def test_staying_paused_says_when_nothing_is_named(self):
        log = MagicMock()
        w = MicWatcher(
            log_fn=log, read_playback_state=lambda: {"paused": True, "paused_by": "user"}, write_playback_state=MagicMock()
        )
        w._resume_after_mic()
        assert "that pause names no writer" in str(log.call_args_list[-1].args[0])


class TestTheLoopLogsTheWriter:
    def test_paused_by_user_names_the_toggle_and_a_hold_over_it(self, tmp_path):
        from claude_code_tts import cli

        state_dir, _queue_dir, _spoken, patches = loop_harness(tmp_path, {})
        for p in patches:
            p.start()
        runner = None
        try:
            st.write_playback_state(paused=False, paused_by=None, audio_pid=None, current_message=None)
            runner = start_loop()
            wait_for_idle_pass(state_dir)
            log_file = state_dir / "daemon.log"
            _pause_toggle()
            assert loop_passes.wait_for(
                lambda: f"Paused by user; holding the queue (from claude-tts pause, pid {os.getpid()}"
                in log_file.read_text()
            ), log_file.read_text()
            cli.cmd_hold(argparse.Namespace(let=["jmo"], me=False, clear=True))
            assert loop_passes.wait_for(
                lambda: "Paused by user again, letting jmo through (from claude-tts hold --clear" in log_file.read_text()
            ), log_file.read_text()
        finally:
            if runner is not None:
                stop_loop(runner)
            for p in reversed(patches):
                p.stop()


class TestStatusWords:
    def _status(self, pb: dict, sid: str, capsys) -> str:
        from claude_code_tts import cli

        with patch.object(cli, "get_session_id", lambda **_: sid), patch(
            "claude_code_tts.state.read_playback_state", lambda: pb
        ):
            try:
                cli.cmd_status(argparse.Namespace())
            except Exception:  # noqa: BLE001  config may be missing in a bare test env; the lines we need print first
                pass
        return capsys.readouterr().out

    def test_a_hold_that_lets_this_room_through_is_not_a_pause_for_it(self, capsys):
        pb = {"paused": True, "paused_by": "user", "let_through": ["bob--claude--jmo"],
              "paused_from": {"what": "claude-tts hold --clear"}}
        out = self._status(pb, "bob--claude--jmo", capsys)
        assert "Paused:   false (a hold lets this room through)" in out
        assert "Set by:   claude-tts hold --clear" in out

    def test_a_room_the_hold_keeps_reads_held(self, capsys):
        pb = {"paused": True, "paused_by": "user", "let_through": ["bob--claude--jmo"]}
        out = self._status(pb, "alice--claude--notes", capsys)
        assert "Paused:   true (by user; a hold, this room is not let through)" in out

    def test_a_plain_pause_reads_as_before_and_names_its_writer(self, capsys):
        pb = {"paused": True, "paused_by": "user", "paused_from": {"what": "claude-tts pause", "pid": 9}}
        out = self._status(pb, "alice--claude--notes", capsys)
        assert "Paused:   true (by user)\n" in out
        assert "Set by:   claude-tts pause, pid 9" in out

    def test_a_mic_pause_names_no_person(self, capsys):
        out = self._status({"paused": True, "paused_by": "mic"}, "alice--claude--notes", capsys)
        assert "Paused:   true (by mic)" in out and "Set by:" not in out
