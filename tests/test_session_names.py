"""One Claude session's own name, over its room's (`claude-tts name --here|--session ID`).

Two sessions awake in one room shared one session key and so one name; the person could not
tell them apart (11-09). The name is keyed by Claude Code's session id from the hook input,
the hook puts it on the queue message, and the daemon counts a different name as a different
speaker, so the floor changes between the two.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import claude_code_tts.config as config
import claude_code_tts.daemon as daemon_mod
import claude_code_tts.msgqueue as mq
import claude_code_tts.session as session_mod
from claude_code_tts.cli import _speak_from_hook, cmd_name, cmd_supersede
from claude_code_tts.config import TTSConfig

ROOM = "alice--claude--ops"
PRIME = "3f2a9c1e-0b7d-4e55-9a10-2c6f1d8e4b71"
SECOND = "9d41e7b2-5c08-4f3a-8e6d-71b0a2c9f354"


@pytest.fixture
def room(tmp_path, monkeypatch):
    """Session store, name store and pin directory in tmp_path; the room is named Pat."""
    monkeypatch.setattr(config, "TTS_SESSIONS_DIR", tmp_path / "sessions.d")
    monkeypatch.setattr(config, "VOICE_CARDS_DIR", tmp_path / "voice.d")
    monkeypatch.setattr(config, "CLAUDE_NAMES_DIR", tmp_path / "names")
    monkeypatch.setattr(config, "TTS_CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(session_mod, "ACTIVE_DIR", tmp_path / "active")
    monkeypatch.setattr(session_mod, "find_claude_ancestor_pid", lambda *a, **k: 4242)
    (tmp_path / "config.json").write_text('{"queue": {"speaker_transition": "chime+announce"}, "speaker_lines": ["rooms"]}')
    monkeypatch.setenv("CLAUDE_TTS_SESSION", ROOM)
    config.session_set(ROOM, "name", "Pat")
    return tmp_path


def _name(name=None, here=False, session=None) -> None:
    cmd_name(argparse.Namespace(name=name, here=here, session=session))


class TestTheNameCommand:
    def test_a_session_named_by_id_keeps_the_rooms_name_for_the_others(self, room, capsys):
        _name("Pat Tangerine", session=SECOND)
        out = capsys.readouterr().out
        assert f"Name set for Claude session {SECOND}: Pat Tangerine" in out
        assert "Says:     Pat Tangerine of House claude, in room ops" in out
        assert f"Session:  {SECOND}: its own name; the room's is Pat" in out
        assert config.claude_session_name(SECOND) == "Pat Tangerine"
        assert config.claude_session_name(PRIME) == ""
        assert config.session_read(ROOM)["name"] == "Pat"

    def test_reset_gives_the_session_the_rooms_name_again(self, room, capsys):
        _name("Pat Tangerine", session=SECOND)
        _name("reset", session=SECOND)
        out = capsys.readouterr().out
        assert "it says the room's name again" in out and "Says:     Pat of House claude, in room ops" in out
        assert not (room / "names" / f"{SECOND}.json").exists()
        assert config.session_read(ROOM)["name"] == "Pat"

    def test_here_names_the_session_the_hook_pinned(self, room, capsys):
        session_mod.pin_claude_session(SECOND)
        _name("Pat Tangerine", here=True)
        assert config.claude_session_name(SECOND) == "Pat Tangerine"

    def test_here_with_no_pin_says_so_and_names_nobody(self, room, capsys):
        with pytest.raises(SystemExit) as e:
            _name("Pat Tangerine", here=True)
        assert e.value.code == 1
        assert "pass --session ID" in capsys.readouterr().out
        assert not (room / "names").exists() or not list((room / "names").iterdir())

    @pytest.mark.parametrize("bad", ["short", "../../etc/passwd-aaaa", "a" * 65, "with space in it"])
    def test_an_id_that_is_not_one_is_refused(self, room, bad):
        with pytest.raises(SystemExit) as e:
            _name("X", session=bad)
        assert e.value.code == 2

    def test_a_name_unchanged_for_a_month_is_pruned_at_the_next_write(self, room):
        config.set_claude_session_name(PRIME, "Old")
        old = room / "names" / f"{PRIME}.json"
        stale = time.time() - config.CLAUDE_NAME_KEEP_S - 60
        os.utime(old, (stale, stale))
        config.set_claude_session_name(SECOND, "New")
        assert not old.exists() and config.claude_session_name(SECOND) == "New"

    def test_a_fresh_name_survives_another_write(self, room):
        config.set_claude_session_name(PRIME, "Pat Prime")
        config.set_claude_session_name(SECOND, "Pat Tangerine")
        assert config.claude_session_name(PRIME) == "Pat Prime"


class TestThePromptHookPinsTheSession:
    def test_the_prompt_hook_pins_the_claude_session_for_here(self, room, monkeypatch):
        monkeypatch.setattr("claude_code_tts.cli._supersede_from_hook", lambda *a, **k: None)
        with patch("sys.stdin", io.StringIO(json.dumps({"session_id": SECOND, "prompt": "hi"}))):
            cmd_supersede(argparse.Namespace(from_hook=True, deferred=False))
        assert session_mod.read_pinned_claude_session() == SECOND


@pytest.fixture
def hook_state(tmp_path, monkeypatch):
    """The hook's spoken records and watermark locks go to tmp_path (as in test_ledger)."""
    real_path = Path
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    def fake_path(arg):
        s = str(arg)
        if s.startswith("/tmp/claude_tts_spoken_") or s.startswith("/tmp/claude_tts_wm_"):
            return state_dir / real_path(s).name
        return real_path(arg)

    monkeypatch.setattr("claude_code_tts.cli.Path", fake_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(mq, "QUEUE_DIR", tmp_path / "queue")
    return tmp_path


def _speak(claude_session: str, transcript: Path, text: str) -> None:
    transcript.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        {"type": "user", "message": {"content": [{"type": "text", "text": "hi"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}},
    ]
    transcript.write_text("".join(json.dumps(line) + "\n" for line in lines))
    payload = json.dumps({"transcript_path": str(transcript), "session_id": claude_session, "last_assistant_message": text})
    cfg = TTSConfig(mode="queue", muted=False, intermediate=True, session_id=ROOM, project_name="ops", name="Pat")
    with (
        patch("sys.stdin", io.StringIO(payload)),
        patch("claude_code_tts.cli.load_config", return_value=cfg),
        patch("claude_code_tts.audio.daemon_healthy", return_value=True),
        patch("claude_code_tts.session.pin_session"),
    ):
        _speak_from_hook(argparse.Namespace(hook_type="stop"))


class TestTheHookAndTheDaemon:
    def test_each_session_says_its_own_name_and_the_floor_changes_between_them(self, room, hook_state):
        config.set_claude_session_name(SECOND, "Pat Tangerine")
        _speak(PRIME, hook_state / "p" / "prime.jsonl", "The cards are answered, all three of them.")
        _speak(SECOND, hook_state / "p" / "second.jsonl", "The duty sweep is done and nothing is due.")
        msgs = sorted((json.loads(p.read_text()) for p in (hook_state / "queue").glob("*.json")), key=lambda m: m["timestamp"])
        assert [m["name"] for m in msgs] == ["Pat", "Pat Tangerine"]
        assert [m["claude_session_id"] for m in msgs] == [PRIME, SECOND]
        assert session_mod.read_pinned_claude_session() == SECOND  # the Stop hook pins it too

        monkeypatch_persona = {"voice": "en_US-lessac-medium"}
        with patch.object(daemon_mod, "get_persona_config", lambda _n: monkeypatch_persona):
            keys = [daemon_mod.prepare_message({**m, "_file": Path("/nowhere/x.json")}, {}).speaker_key for m in msgs]
        assert keys[0] != keys[1]

    def test_two_unnamed_sessions_of_one_room_are_one_speaker_as_before(self):
        base = {"session_id": ROOM, "project": "ops", "text": "hello", "persona": "claude-prime", "_file": Path("/nowhere/x.json")}
        with patch.object(daemon_mod, "get_persona_config", lambda _n: {}):
            a = daemon_mod.prepare_message({**base, "claude_session_id": PRIME}, {})
            b = daemon_mod.prepare_message({**base, "claude_session_id": SECOND}, {})
        assert a.speaker_key == b.speaker_key == f"{ROOM}:ops"
