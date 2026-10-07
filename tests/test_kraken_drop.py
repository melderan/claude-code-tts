"""`claude-tts kraken --drop`: release the hold over an empty queue.

A hold is a pause: held time does not age a message and the depth cap skips held messages,
so `kraken` after a long hold replays everything held, in order. With --drop the CLI writes a
drop-all control naming the ids it saw; the daemon takes it at the top of its next pass, held or
not, drops exactly those (hook and bridge alike, the half-played one too) with a `dropped (kraken)`
outcome each, and leaves other controls and speech the CLI had not seen. Ids, never times: hooks
on other machines or in containers stamp with their own clocks.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import claude_code_tts.daemon as d
import claude_code_tts.msgqueue as mq
import claude_code_tts.state as st

SESSION_A = "-session-a"
SESSION_B = "-session-b"


def _reply(text: str, session: str = SESSION_A, **over) -> Path:
    fields: dict = {
        "project": text.split()[0], "text": text, "persona": "claude-prime",
        "speed": 2.0, "speed_method": "playback", "session_id": session,
    }
    fields.update(over)
    path, _ = mq.write_message(fields)
    time.sleep(0.002)
    return path


def _ids() -> list[str]:
    return [str(m["id"]) for m in mq.scan() if m.get("type") != "control"]


@pytest.fixture
def queue(tmp_path, monkeypatch):
    monkeypatch.setattr(mq, "QUEUE_DIR", tmp_path / "queue")
    monkeypatch.setattr(st, "PLAYBACK_STATE_FILE", tmp_path / "playback.json")
    monkeypatch.setattr(st, "VERSION_FILE", tmp_path / "daemon.version")
    return tmp_path / "queue"


class TestApplyDropAll:
    def test_the_named_messages_go_other_controls_and_unseen_speech_stay(self, queue):
        a = _reply("a from session a")
        b = _reply("b from session b", SESSION_B)
        page = _reply("page reading", session="browser", source="page")
        restart = mq.write_control_message(post_action="restart")
        ctl = mq.write_drop_all_message(_ids())
        after = _reply("after the ask", timestamp=time.time() - 3600)  # an old clock on another machine
        removed: list[dict] = []
        said: list[str] = []
        kept = mq.apply_drop_all(mq.scan(), log=lambda m, level="INFO": said.append(m), on_removed=removed.append)
        assert sorted(m["_file"] for m in kept) == sorted([restart, after])
        assert sorted(m["text"] for m in removed) == ["a from session a", "b from session b", "page reading"]
        for p in (a, b, page, ctl):
            assert not p.exists()
        assert restart.exists() and after.exists()
        assert said == ["Drop-all: 3 of 3 named message(s) dropped on release"]

    def test_without_a_control_nothing_is_touched(self, queue):
        a = _reply("a")
        scanned = mq.scan()
        assert mq.apply_drop_all(scanned, on_removed=lambda m: pytest.fail("nothing to remove")) is scanned
        assert a.exists()

    def test_two_controls_name_their_union(self, queue):
        first_ids = _ids() + ["gone-already"]
        a = _reply("a")
        first = mq.write_drop_all_message(first_ids + _ids())
        b = _reply("b")
        second = mq.write_drop_all_message(_ids())
        kept = mq.apply_drop_all(mq.scan())
        assert kept == []
        for p in (a, b, first, second):
            assert not p.exists()

    def test_a_file_gone_already_is_no_error(self, queue):
        a = _reply("a")
        mq.write_drop_all_message(_ids())
        scanned = mq.scan()
        a.unlink()
        removed: list[dict] = []
        assert mq.apply_drop_all(scanned, on_removed=removed.append) == []
        assert removed == []

    def test_a_control_that_will_not_go_is_logged_not_raised(self, queue):
        mq.write_drop_all_message([])
        scanned = mq.scan()
        said: list[tuple[str, str]] = []
        with patch.object(mq.Path, "unlink", side_effect=PermissionError("nope")):
            assert mq.apply_drop_all(scanned, log=lambda m, level="INFO": said.append((level, m))) == []
        assert said[0][0] == "WARN" and "could not be removed" in said[0][1]

    def test_a_control_without_ids_drops_nothing(self, queue):
        a = _reply("a")
        mq.write_message({"type": "control", "session_id": "system", "text": "", "pre_action": mq.DROP_ALL})
        kept = mq.apply_drop_all(mq.scan())
        assert [m["_file"] for m in kept] == [a]

    def test_is_drop_all_names_only_the_control(self):
        assert mq.is_drop_all({"type": "control", "pre_action": "drop_all"})
        assert not mq.is_drop_all({"type": "control", "pre_action": "supersede"})
        assert not mq.is_drop_all({"type": "speech", "pre_action": "drop_all"})


class TestDaemonSide:
    def test_outcomes_and_the_interrupted_message(self, queue):
        _reply("held reply", id="held-1")
        _reply("page reading", session="browser", source="page", id="job-1")
        mq.write_drop_all_message(_ids())
        st.write_playback_state(current_message={"id": "half-1", "session_id": SESSION_B, "project": "b", "text": "half played"})
        outcomes: list[tuple[str, str, str, str]] = []
        jobs: list[tuple[object, dict]] = []
        with patch.object(d.voice_ledger, "record_outcome", lambda sid, mid, out, reason, extra=None: outcomes.append((sid, mid, out, reason)) or True), \
             patch.object(d.JOBS, "update", lambda job_id, **kw: jobs.append((job_id, kw))), \
             patch.object(d, "log", lambda m, level="INFO": None):
            kept = d.drop_all_requested(mq.scan())
        assert kept == []
        assert sorted(outcomes) == [(SESSION_A, "held-1", "dropped", "kraken"), (SESSION_B, "half-1", "dropped", "kraken")]
        assert jobs == [("job-1", {"state": "cancelled", "position_ms": 0})]
        assert st.read_playback_state().get("current_message") is None

    def test_the_interrupted_message_is_taken_before_the_control_goes(self, queue):
        """The CLI releases the hold the moment the control file is gone; the daemon's last
        rewrite of playback.json must come before that, or it lands on top of the release."""
        _reply("held", id="held-1")
        ctl = mq.write_drop_all_message(_ids())
        st.hold([])
        st.write_playback_state(current_message={"id": "half-1", "session_id": SESSION_B, "project": "b"})
        order: list[str] = []
        real_take = d.get_interrupted_message

        def take() -> dict | None:
            order.append("control gone" if not ctl.exists() else "interrupted taken")
            return real_take()

        with patch.object(d, "get_interrupted_message", take), patch.object(d, "log", lambda m, level="INFO": None), \
             patch.object(d.voice_ledger, "record_outcome", lambda *a, **k: True):
            d.drop_all_requested(mq.scan())
        assert order == ["interrupted taken"]
        assert st.read_playback_state()["paused"] is True  # the daemon never touched the hold itself

    def test_one_outcome_when_the_interrupted_message_is_also_queued(self, queue):
        _reply("replayed after a restart", id="same-1")
        mq.write_drop_all_message(_ids())
        st.write_playback_state(current_message={"id": "same-1", "session_id": SESSION_A, "project": "a"})
        outcomes: list[str] = []
        with patch.object(d.voice_ledger, "record_outcome", lambda sid, mid, out, reason, extra=None: outcomes.append(mid) or True), \
             patch.object(d, "log", lambda m, level="INFO": None):
            d.drop_all_requested(mq.scan())
        assert outcomes == ["same-1"]

    def test_without_a_control_the_scan_is_returned_as_is(self, queue):
        _reply("a")
        scanned = mq.scan()
        with patch.object(d, "get_interrupted_message", lambda: pytest.fail("not asked")):
            assert d.drop_all_requested(scanned) is scanned


def _daemon(version: str | None) -> None:
    if version is not None:
        st.VERSION_FILE.with_name("daemon.release").write_text(version)


class TestCli:
    def test_drop_names_the_ids_waits_for_the_daemon_then_releases(self, queue, capsys, monkeypatch):
        from claude_code_tts import cli

        monkeypatch.setattr(cli.time, "sleep", lambda s: None)
        st.hold([])
        _reply("held a")
        _reply("held b", SESSION_B)
        expected = sorted(_ids())
        _daemon("9.49.0")
        written: list[dict] = []
        real_write = mq.write_drop_all_message

        def write_and_take(ids: list[str]) -> Path:
            p = real_write(ids)
            written.append(next(m for m in mq.scan() if mq.is_drop_all(m)))
            p.unlink()  # the daemon took it at once
            return p

        with patch.object(mq, "write_drop_all_message", write_and_take), \
             patch("claude_code_tts.state.is_daemon_running", lambda: (True, 4242)):
            cli.cmd_kraken(argparse.Namespace(drop=True))
        out = capsys.readouterr().out
        assert len(written) == 1 and sorted(written[0]["drop_ids"]) == expected
        assert "Dropped 2 queued message(s)" in out
        assert "kraken is released" in out
        assert st.read_playback_state()["paused"] is False

    def test_drop_without_a_daemon_leaves_the_control_for_the_next_one(self, queue, capsys, monkeypatch):
        from claude_code_tts import cli

        monkeypatch.setattr(cli.time, "sleep", lambda s: None)
        st.hold([])
        _reply("held a")
        with patch("claude_code_tts.state.is_daemon_running", lambda: (False, None)):
            cli.cmd_kraken(argparse.Namespace(drop=True))
        out = capsys.readouterr().out
        assert "Daemon not running; 1 queued message(s) are dropped when it next starts" in out
        controls = [m for m in mq.scan() if mq.is_drop_all(m)]
        assert len(controls) == 1 and len(controls[0]["drop_ids"]) == 1
        assert st.read_playback_state()["paused"] is False

    @pytest.mark.parametrize("version", ["9.48.0", None, "garbage"])
    def test_an_older_daemon_gets_the_files_dropped_directly(self, queue, capsys, monkeypatch, version):
        from claude_code_tts import cli

        monkeypatch.setattr(cli.time, "sleep", lambda s: None)
        st.hold([])
        a = _reply("held a")
        restart = mq.write_control_message(post_action="restart")
        _daemon(version)
        with patch("claude_code_tts.state.is_daemon_running", lambda: (True, 7)), \
             patch.object(mq, "write_drop_all_message", lambda ids: pytest.fail("an older daemon never takes it")):
            cli.cmd_kraken(argparse.Namespace(drop=True))
        out = capsys.readouterr().out
        assert "Dropped 1 queued message(s) directly" in out and "older than 9.49.0" in out
        assert not a.exists() and restart.exists()
        assert st.read_playback_state()["paused"] is False

    def test_a_slow_daemon_is_reported_not_waited_for_forever(self, queue, capsys, monkeypatch):
        from claude_code_tts import cli

        st.hold([])
        _daemon("9.49.0")
        clock = {"t": 0.0}

        def monotonic() -> float:
            clock["t"] += 3.0
            return clock["t"]

        monkeypatch.setattr(cli.time, "monotonic", monotonic)
        monkeypatch.setattr(cli.time, "sleep", lambda s: None)
        with patch("claude_code_tts.state.is_daemon_running", lambda: (True, 1)):
            cli.cmd_kraken(argparse.Namespace(drop=True))
        assert "has not taken the request yet" in capsys.readouterr().out

    def test_a_release_the_daemon_wrote_over_is_said_again(self, queue, capsys, monkeypatch):
        from claude_code_tts import cli

        _daemon("9.49.0")
        st.hold([])
        releases = {"n": 0}
        real_release = st.release

        def release_then_lose_the_first() -> dict:
            releases["n"] += 1
            state = real_release()
            if releases["n"] == 1:
                st.write_playback_state(paused=True, paused_by="user")  # the daemon's stale rewrite lands
            return state

        monkeypatch.setattr(cli.time, "sleep", lambda s: None)
        with patch("claude_code_tts.state.release", release_then_lose_the_first), \
             patch("claude_code_tts.state.is_daemon_running", lambda: (False, None)):
            cli.cmd_kraken(argparse.Namespace(drop=True))
        assert releases["n"] == 2
        assert st.read_playback_state()["paused"] is False
        assert "kraken is released" in capsys.readouterr().out

    def test_a_mic_hold_that_began_meanwhile_is_left_alone(self, queue, capsys, monkeypatch):
        from claude_code_tts import cli

        _daemon("9.49.0")
        st.hold([])
        real_release = st.release

        def release_then_mic() -> dict:
            state = real_release()
            st.write_playback_state(paused=True, paused_by="mic")
            return state

        monkeypatch.setattr(cli.time, "sleep", lambda s: None)
        with patch("claude_code_tts.state.release", release_then_mic), \
             patch("claude_code_tts.state.is_daemon_running", lambda: (False, None)):
            cli.cmd_kraken(argparse.Namespace(drop=True))
        assert st.read_playback_state()["paused_by"] == "mic"
        assert "Could not release" in capsys.readouterr().out

    def test_plain_kraken_drops_nothing_and_reads_nothing_back(self, queue, capsys, monkeypatch):
        from claude_code_tts import cli

        st.hold([])
        a = _reply("held a")
        monkeypatch.setattr(cli.time, "sleep", lambda s: pytest.fail("plain kraken never waits"))
        cli.cmd_kraken(argparse.Namespace())
        assert a.exists()
        assert not any(mq.is_drop_all(m) for m in mq.scan())
        assert st.read_playback_state()["paused"] is False


class TestReleaseAtLeast:
    @pytest.mark.parametrize(
        "have, ok",
        [("9.49.0", True), ("9.49.1", True), ("10.0.0", True), ("9.48.9", False), (None, False), ("", False), ("x.y", False), ("9.49", False)],
    )
    def test_floor(self, have, ok):
        assert st.release_at_least(have, "9.49.0") is ok
