"""A room's one voice switch (roomvoice.py): the CLI and the bridge flip the same thing.

JMO, 2026-11-10: "There shouldn't be two mechanisms. Just one toggle with two interfaces to
reach please." The case that prompted it: a room muted with `claude-tts mute`, then turned
on in hear (which only let it through his hold), stayed silent while both said on.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import claude_code_tts.config as config_mod
import claude_code_tts.state as st
from claude_code_tts import roomvoice
from claude_code_tts.cli import main
from claude_code_tts.state import hold, read_playback_state, write_playback_state

NOTES = "alice--claude--notes"
NOTES_FOLDER = "-Users-alice-code-notes"
TTS = "alice--claude--claude-code-tts"


@pytest.fixture
def home(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions.d"
    sessions.mkdir()
    for sid in (NOTES, NOTES_FOLDER, TTS):
        (sessions / f"{sid}.json").write_text("{}")
    raw: dict = {"mode": "queue"}
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(raw))
    monkeypatch.setattr(config_mod, "TTS_SESSIONS_DIR", sessions)
    monkeypatch.setattr(config_mod, "load_raw_config", lambda: raw)
    monkeypatch.setattr("claude_code_tts.cli.load_raw_config", lambda: raw)
    monkeypatch.setattr("claude_code_tts.cli.TTS_CONFIG_FILE", config_file)
    monkeypatch.setattr("claude_code_tts.cli.TTS_SESSIONS_DIR", sessions)
    monkeypatch.setattr(config_mod, "load_config", lambda *a, **k: None)  # the voice card is not this test's
    monkeypatch.setattr(st, "PLAYBACK_STATE_FILE", tmp_path / "playback.json")
    monkeypatch.setattr(st, "HEARTBEAT_FILE", tmp_path / "daemon.heartbeat")
    return {"sessions": sessions, "raw": raw}


def muted(home, sid: str) -> object:
    return json.loads((home["sessions"] / f"{sid}.json").read_text()).get("muted")


class TestOneSwitch:
    def test_on_over_a_mute_under_a_hold_is_heard(self, home):
        """The 2026-11-10 case: muted by the CLI, a hold letting another room through, turned on."""
        roomvoice.set_room_voice("notes", False)
        hold([TTS])
        view = roomvoice.set_room_voice("notes", True, daemon_up=True)
        assert view is not None and view["on"] and view["audible"], view["reasons"]
        assert muted(home, NOTES) is False and muted(home, NOTES_FOLDER) is False
        assert read_playback_state()["let_through"] == [TTS, "notes"]

    def test_off_mutes_and_takes_the_room_off_the_list_only(self, home):
        hold([TTS, NOTES, "notes"])
        view = roomvoice.set_room_voice("notes", False, daemon_up=True)
        assert view is not None and not view["on"] and not view["audible"]
        assert muted(home, NOTES) is True and muted(home, NOTES_FOLDER) is True and muted(home, TTS) is None
        state = read_playback_state()
        assert state["paused"] is True and state["let_through"] == [TTS], "the hold stays; only this room leaves it"

    @pytest.mark.parametrize("on", [True, False])
    def test_the_switch_never_starts_a_hold(self, home, on):
        roomvoice.set_room_voice("notes", on, daemon_up=True)
        assert not read_playback_state().get("paused")

    def test_on_twice_lists_the_room_once(self, home):
        hold([])
        roomvoice.set_room_voice("notes", True)
        roomvoice.set_room_voice("notes", True)
        assert read_playback_state()["let_through"] == ["notes"]

    def test_the_cli_and_the_function_are_the_same_switch(self, home, capsys):
        hold([TTS])
        main(["mute", "--room", "notes"])
        assert muted(home, NOTES) is True
        main(["unmute", "--room", "notes"])
        assert muted(home, NOTES) is False and read_playback_state()["let_through"] == [TTS, "notes"]
        out = capsys.readouterr().out
        assert "2 session(s) of room notes unmuted" in out
        assert "Room notes: on" in out

    def test_no_session_is_none_and_the_cli_exits_1(self, home, capsys):
        assert roomvoice.room_voice("nope") is None and roomvoice.set_room_voice("nope", True) is None
        with pytest.raises(SystemExit) as e:
            main(["voice", "on", "--room", "nope"])
        assert e.value.code == 1
        assert not read_playback_state().get("paused")


class TestReadBack:
    def test_a_global_mute_counts_for_a_session_with_no_flag(self, home):
        home["raw"]["muted"] = True
        view = roomvoice.room_voice("notes", daemon_up=True)
        assert view is not None and not view["on"] and view["reasons"][0]["reason"] == "muted"

    def test_one_muted_session_means_off(self, home):
        config_mod.session_set(NOTES_FOLDER, "muted", True)
        view = roomvoice.room_voice("notes", daemon_up=True)
        assert view is not None and not view["on"]
        assert [s["muted"] for s in view["sessions"]] == [True, False]  # sorted: the folder id first

    def test_an_on_room_a_hold_does_not_let_through_says_held(self, home):
        hold([TTS])  # the room was never muted; the hold alone keeps it quiet
        view = roomvoice.room_voice("notes", daemon_up=True)
        assert view is not None and view["on"] and not view["audible"]
        assert [r["reason"] for r in view["reasons"]] == ["held"]
        assert roomvoice.describe(view) == "Room notes: on, not heard: a person's hold does not let this room through"

    def test_a_recording_and_a_dead_daemon_are_named(self, home):
        write_playback_state(paused=True, paused_by="mic")
        view = roomvoice.room_voice("notes")  # no heartbeat file: no daemon
        assert view is not None and [r["reason"] for r in view["reasons"]] == ["recording", "daemon"]

    def test_a_meeting_is_named_over_everything_held(self, home):
        state = {"paused": True, "paused_by": "user", "meeting": {"apps": ["Zoom"], "since": 1.0}}
        st.atomic_write_json(st.PLAYBACK_STATE_FILE, state)
        view = roomvoice.room_voice("notes", daemon_up=True)
        assert view is not None and [r["reason"] for r in view["reasons"]] == ["meeting"]
        assert "Zoom" in view["reasons"][0]["detail"]

    def test_heard_when_nothing_is_in_the_way(self, home):
        view = roomvoice.room_voice("notes", daemon_up=True)
        assert view is not None and view["audible"] and roomvoice.describe(view) == "Room notes: on, heard"

    def test_voice_json_is_the_view(self, home, capsys):
        main(["voice", "--room", "notes", "--json"])
        view = json.loads(capsys.readouterr().out)
        assert view["room"] == "notes" and view["on"] is True and {s["session"] for s in view["sessions"]} == {
            NOTES, NOTES_FOLDER,
        }


def test_room_of_names_the_room_a_session_is_reached_by():
    assert roomvoice.room_of(TTS) == "tts"
    assert roomvoice.room_of(NOTES) == "notes"
    assert roomvoice.room_of(NOTES_FOLDER) == NOTES_FOLDER


def test_room_sessions_reads_the_patched_directory(home):
    assert roomvoice.room_sessions("tts") == [TTS]
    assert isinstance(config_mod.TTS_SESSIONS_DIR, Path)
