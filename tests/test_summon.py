"""`claude-tts summon`: one room calls the person through their hold, louder, and learns if it landed.

JMO, 2026-11-07: "If you need GPG you should unmute, increase volume, and say loudly the 5 magic
words 'JMO I need your help!'" Haley asked for one call that does it for one room only: through
the person's hold without letting anyone else through, the output volume raised and put back,
spoken next, and a ledger row saying played or failed. Never through a call.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

import claude_code_tts.daemon as d
import claude_code_tts.state as st
from claude_code_tts import ledger, volume
from claude_code_tts.bridge import JOBS
from claude_code_tts.state import hold, may_play
from tests import loop_passes

ROOM = "carol--claude--summoner"


# --- the volume, raised and put back -------------------------------------------------------


class FakeMac:
    """osascript's volume, as a runner volume.py can call."""

    def __init__(self, level: int | None = 30, muted: bool = False, refuse_set: bool = False) -> None:
        self.level, self.muted, self.refuse_set = level, muted, refuse_set
        self.sets: list[tuple[int, bool]] = []

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        script = argv[-1]
        if script == "get volume settings":
            shown = "missing value" if self.level is None else str(self.level)
            out = f"output volume:{shown}, input volume:58, alert volume:100, output muted:{str(self.muted).lower()}"
            return subprocess.CompletedProcess(argv, 0, out, "")
        if self.refuse_set:
            return subprocess.CompletedProcess(argv, 1, "", "no")
        # osascript -e "set volume output volume N" -e "set volume with|without output muted"
        assert argv[0] == volume.OSASCRIPT and argv[1] == "-e" and argv[3] == "-e", argv
        level_words, mute_words = argv[2].split(), argv[4].split()
        assert level_words[:4] == ["set", "volume", "output", "volume"] and mute_words[3:] == ["output", "muted"]
        self.level, self.muted = int(level_words[4]), mute_words[2] == "with"
        self.sets.append((self.level, self.muted))
        return subprocess.CompletedProcess(argv, 0, "", "")


def _logs() -> tuple[list[str], object]:
    lines: list[str] = []
    return lines, lambda msg, level="INFO": lines.append(f"{level} {msg}")


def test_parse_settings() -> None:
    assert volume.parse_settings("output volume:44, input volume:58, alert volume:100, output muted:false") == (
        volume.Output(44, False)
    )
    assert volume.parse_settings("output volume:7, input volume:1, alert volume:1, output muted:true") == (
        volume.Output(7, True)
    )
    assert volume.parse_settings("output volume:missing value, input volume:58, alert volume:100, output muted:false") is None


def test_a_summon_raises_the_volume_and_puts_it_back(tmp_path: Path) -> None:
    mac, (_, log) = FakeMac(level=30, muted=True), _logs()
    restore = tmp_path / "volume-restore.json"
    with volume.raised(80, log, run=mac, platform="darwin", path=restore):
        assert (mac.level, mac.muted) == (80, False)
        assert json.loads(restore.read_text()) == {"volume": 30, "muted": True}, "no record for a crash to restore"
    assert (mac.level, mac.muted) == (30, True)
    assert mac.sets == [(80, False), (30, True)]
    assert not restore.exists()


def test_the_volume_is_put_back_when_the_play_raises(tmp_path: Path) -> None:
    mac, (_, log) = FakeMac(level=20), _logs()
    with pytest.raises(RuntimeError), volume.raised(80, log, run=mac, platform="darwin", path=tmp_path / "r.json"):
        raise RuntimeError("player died")
    assert mac.level == 20


def test_a_summon_never_turns_anyone_down(tmp_path: Path) -> None:
    mac, (_, log) = FakeMac(level=95), _logs()
    with volume.raised(80, log, run=mac, platform="darwin", path=tmp_path / "r.json"):
        pass
    assert mac.sets == []


def test_an_ordinary_message_and_another_platform_touch_nothing(tmp_path: Path) -> None:
    mac, (lines, log) = FakeMac(level=10), _logs()
    with volume.raised(None, log, run=mac, platform="darwin", path=tmp_path / "r.json"):
        pass
    with volume.raised(80, log, run=mac, platform="linux", path=tmp_path / "r.json"):
        pass
    assert mac.sets == []
    assert any("not on macOS" in line for line in lines)


def test_an_unreadable_volume_is_left_alone(tmp_path: Path) -> None:
    mac, (lines, log) = FakeMac(level=None), _logs()
    with volume.raised(80, log, run=mac, platform="darwin", path=tmp_path / "r.json"):
        pass
    assert mac.sets == []
    assert any("cannot be read" in line for line in lines)


def test_a_refused_raise_leaves_no_record(tmp_path: Path) -> None:
    mac, (_, log) = FakeMac(level=10, refuse_set=True), _logs()
    restore = tmp_path / "r.json"
    with volume.raised(80, log, run=mac, platform="darwin", path=restore):
        pass
    assert not restore.exists()


def test_a_daemon_that_died_mid_summon_is_undone_at_the_next_start(tmp_path: Path) -> None:
    mac, (_, log) = FakeMac(level=80), _logs()
    restore = tmp_path / "volume-restore.json"
    restore.write_text(json.dumps({"volume": 25, "muted": True}))
    assert volume.restore_left_over(log, run=mac, path=restore)
    assert (mac.level, mac.muted) == (25, True)
    assert not restore.exists()
    assert not volume.restore_left_over(log, run=mac, path=restore), "nothing left over is no work"


def test_clamp() -> None:
    assert volume.clamp(80) == 80
    assert volume.clamp(500) == 100
    assert volume.clamp(0) == 1
    assert volume.clamp(None) is None
    assert volume.clamp(True) is None
    assert volume.clamp("80") is None


# --- who may play ------------------------------------------------------------------------------


class TestMayPlay:
    held = {"paused": True, "paused_by": "user", "paused_since": 100.0, "let_through": ["bob--claude--b"]}

    def test_through_a_persons_hold_set_before_it(self) -> None:
        assert may_play(self.held, ROOM, "now", 101.0, summon=True)

    def test_a_hold_set_after_it_holds_it(self) -> None:
        assert not may_play(self.held, ROOM, "now", 99.0, summon=True)

    def test_a_recording_holds_it(self) -> None:
        assert not may_play({**self.held, "paused_by": "mic"}, ROOM, "now", 101.0, summon=True)
        assert not may_play({**self.held, "mic_held": True}, ROOM, "now", 101.0, summon=True)

    def test_never_through_a_call(self) -> None:
        call = {"paused": False, "meeting": {"apps": ["zoom.us"], "since": 100.0}}
        assert not may_play(call, ROOM, "now", 101.0, summon=True)
        assert may_play(call, "browser", "now", 101.0), "a click after the call began is still the person's"

    def test_the_room_itself_stays_held(self) -> None:
        assert not may_play(self.held, ROOM, "", 101.0)


# --- the command -------------------------------------------------------------------------------


def _summon_args(*words: str, volume_level: int = 80, wait: float = 0.0) -> argparse.Namespace:
    return argparse.Namespace(text=list(words), volume=volume_level, wait=wait)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_TTS_SESSION", ROOM)
    return tmp_path


def _run_summon(args: argparse.Namespace, queue_dir: Path) -> int:
    from claude_code_tts import cli, msgqueue

    with (
        patch.object(msgqueue, "QUEUE_DIR", queue_dir),
        patch("claude_code_tts.audio.daemon_healthy", return_value=True),
        patch.object(cli, "get_session_id", return_value=ROOM),
    ):
        try:
            cli.cmd_summon(args)
        except SystemExit as e:
            return int(e.code or 0)
    return 0


def test_summon_queues_one_now_block_in_the_rooms_voice_with_a_ledger_row(home: Path) -> None:
    queue_dir = home / "queue"
    assert _run_summon(_summon_args("JMO", "I", "need", "your", "help!"), queue_dir) == 0
    (f,) = list(queue_dir.glob("*.json"))
    msg = json.loads(f.read_text())
    assert msg["text"] == "JMO I need your help!"
    assert (msg["lane"], msg["source"], msg["summon"], msg["volume"]) == ("now", "summon", True, 80)
    assert msg["session_id"] == ROOM
    (row,) = ledger.read(ROOM)
    assert (row["id"], row["event"], row["text"]) == (msg["id"], "summon", "JMO I need your help!")


def test_summon_changes_neither_the_hold_nor_the_mute(home: Path) -> None:
    state_file = home / ".claude-tts" / "playback.json"
    with patch.object(st, "PLAYBACK_STATE_FILE", state_file):
        hold(["bob--claude--b"])
        before = st.read_playback_state()
        assert _run_summon(_summon_args("help"), home / "queue") == 0
        after = st.read_playback_state()
    assert {k: after.get(k) for k in ("paused", "paused_by", "let_through")} == {
        k: before.get(k) for k in ("paused", "paused_by", "let_through")
    }


@pytest.mark.parametrize(("outcome", "code"), [("played", 0), ("dropped", 1), ("failed", 1)])
def test_summon_exits_on_the_daemons_outcome(home: Path, outcome: str, code: int) -> None:
    with patch.object(ledger, "new_id", return_value="feedfacecafe0001"):
        ledger.record_outcome(ROOM, "feedfacecafe0001", outcome, "" if outcome == "played" else "age")
        assert _run_summon(_summon_args("help", wait=5.0), home / "queue") == code


def test_summon_without_an_outcome_times_out_with_exit_1(home: Path) -> None:
    assert _run_summon(_summon_args("help", wait=0.3), home / "queue") == 1


@pytest.mark.parametrize("bad", [0, 101])
def test_a_volume_out_of_range_is_a_usage_error(home: Path, bad: int) -> None:
    assert _run_summon(_summon_args("help", volume_level=bad), home / "queue") == 2


def test_no_daemon_is_exit_1_and_nothing_queued(home: Path) -> None:
    from claude_code_tts import cli, msgqueue

    with (
        patch.object(msgqueue, "QUEUE_DIR", home / "queue"),
        patch("claude_code_tts.audio.daemon_healthy", return_value=False),
        patch.object(cli, "get_session_id", return_value=ROOM),
        pytest.raises(SystemExit) as e,
    ):
        cli.cmd_summon(_summon_args("help"))
    assert e.value.code == 1
    assert not list((home / "queue").glob("*.json")) if (home / "queue").exists() else True


# --- the daemon --------------------------------------------------------------------------------


def _harness():
    import importlib.util

    spec = importlib.util.spec_from_file_location("test_pause_ledger", Path(__file__).parent / "test_pause_ledger.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod.loop_harness, mod.stop_loop


def _wait_for(pred, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


@pytest.mark.parametrize("unit", ["message", "sentence"])
def test_the_daemon_plays_a_summon_through_a_hold_loud_and_writes_played(
    home: Path, monkeypatch: pytest.MonkeyPatch, unit: str
) -> None:
    loop_harness, stop_loop = _harness()
    state_dir, queue_dir, spoken, patches = loop_harness(home, {"speech_unit": unit})
    raised_with: list[object] = []

    @contextmanager
    def recording_raise(level: object, log: object) -> Iterator[None]:
        raised_with.append(level)
        yield

    patches.append(patch.object(d.volume, "raised", recording_raise))
    for p in patches:
        p.start()
    JOBS._jobs.clear()
    JOBS._cancel.clear()
    st.write_playback_state(paused=False, paused_by=None, audio_pid=None, current_message=None)

    def run() -> None:
        d._shutdown_requested = False
        d._daemon_mode = True
        d.daemon_loop()

    runner = threading.Thread(target=run, daemon=True)
    runner.start()
    try:
        assert _wait_for(lambda: (state_dir / "daemon.heartbeat").exists()), "loop never reached its idle pass"
        hold([])
        time.sleep(0.05)  # sleep-ok: the summon must be queued after the hold began, by the clock
        from claude_code_tts import msgqueue

        msgqueue.write_message({"session_id": "alice--claude--a", "project": "a", "text": "a held room"})
        with patch("claude_code_tts.audio.daemon_healthy", return_value=True):
            from claude_code_tts import cli

            with patch.object(cli, "get_session_id", return_value=ROOM):
                cli.cmd_summon(_summon_args("JMO I need your help!"))
        assert _wait_for(lambda: "JMO I need your help!" in spoken), "the summon never played through the hold"
        assert _wait_for(
            lambda: any(o.get("outcome") == "played" for o in ledger.outcomes(ROOM))
        ), "no played row in the room's ledger"
        assert loop_passes.wait_passes(3), "the loop stopped passing"
        assert spoken == ["JMO I need your help!"], "another room spoke through the hold"
        assert raised_with and set(raised_with) == {80}
        assert st.read_playback_state()["paused"] is True, "the summon released the hold"
        (row,) = ledger.read(ROOM)
        assert row["outcome"] == "played"
    finally:
        stop_loop(runner)
        for p in reversed(patches):
            p.stop()


def test_the_pause_hotkey_during_a_summon_stops_only_that_read(home: Path) -> None:
    """The person silences the call with the key they know; the hold stays and nobody else floods in."""
    from claude_code_tts import cli, msgqueue
    from tests.test_now_lane import _pause_hotkey, _run_loop

    playing = home / "summon-playing"
    body = f'case "$*" in *summoner*) echo x >> {playing}; sleep 2 ;; *) sleep 0.05 ;; esac'
    state_dir, queue_dir, spoken, stop = _run_loop(home, body)
    try:
        hold([])
        time.sleep(0.05)  # sleep-ok: the summon must be queued after the hold began, by the clock
        msgqueue.write_message({"session_id": "alice--claude--a", "project": "a", "text": "a room waits"})
        with (
            patch("claude_code_tts.audio.daemon_healthy", return_value=True),
            patch.object(cli, "get_session_id", return_value=ROOM),
        ):
            cli.cmd_summon(_summon_args("JMO I need your help!"))
        assert _wait_for(lambda: playing.exists()), "the summon never started"
        _pause_hotkey()
        assert _wait_for(
            lambda: any(o.get("outcome") == "cancelled" for o in ledger.outcomes(ROOM))
        ), "the hotkey did not stop the summon"
        assert loop_passes.wait_passes(3), "the loop stopped passing"
        state = st.read_playback_state()
        assert state["paused"] is True and state["paused_by"] == "user", "the hold stays"
        assert spoken == ["JMO I need your help!"], "the room waits and the summon does not resume"
    finally:
        stop()
