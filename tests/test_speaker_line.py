"""A room introduces itself when it takes the floor: the boop, then "<name> of <house> house in
<room> room" in its own voice (asked 2026-10-08). The name is a session key set with
`claude-tts name`; "Friend" until then. Sessions that are not rooms keep "<project> says:".
"""

from __future__ import annotations

from pathlib import Path

import pytest

import claude_code_tts.audio as audio
import claude_code_tts.config as config
import claude_code_tts.daemon as d
from claude_code_tts.cli import cmd_name
from claude_code_tts.state import house_tag


class TestHouseTag:
    @pytest.mark.parametrize(
        "sid, tag",
        [
            ("alice--claude--claude-code-tts", "claude"),
            ("bob--gpt--notes", "gpt"),
            ("owner--a--b--c", "b"),
            ("plain", ""),
            ("two--parts", ""),
        ],
    )
    def test_house_tag(self, sid, tag):
        assert house_tag(sid) == tag


class TestSpeakerLine:
    def test_a_named_room_says_name_house_and_room(self):
        msg = {"session_id": "alice--claude--claude-code-tts", "name": "Connery", "project": "tts"}
        assert d.speaker_line(msg) == "Connery of claude house in tts room"

    def test_an_unnamed_room_is_a_friend(self):
        msg = {"session_id": "alice--claude--k8s", "project": "k8s"}
        assert d.speaker_line(msg) == "Friend of claude house in k8s room"

    def test_a_room_with_no_house_names_the_room_only(self):
        assert d.speaker_line({"session_id": "two--notes", "name": "Lyra"}) == "Lyra in notes room"

    def test_a_plain_session_keeps_project_says(self):
        assert d.speaker_line({"session_id": "-Users-x-proj", "project": "proj"}) == "proj says:"

    def test_a_plain_session_with_a_name_says_the_name(self):
        assert d.speaker_line({"session_id": "-Users-x-proj", "project": "proj", "name": "Pat"}) == "Pat says:"

    def test_blank_name_is_no_name(self):
        assert d.speaker_line({"session_id": "a--h--r", "name": "   "}) == "Friend of h house in r room"


@pytest.fixture
def transition(tmp_path, monkeypatch):
    """speaker_transition with the chime, synthesis and player replaced by recorders."""
    events: list[tuple] = []
    monkeypatch.setattr(d, "AUDIO_TMP_DIR", tmp_path)
    monkeypatch.setattr(d, "play_chime", lambda: events.append(("chime",)))
    monkeypatch.setattr(d, "log", lambda *a, **k: None)
    monkeypatch.setattr(d.time, "sleep", lambda s: None)

    def gen(text, persona, path, **kw):
        events.append(("say", text, persona))
        Path(path).write_bytes(b"RIFF")
        return True

    monkeypatch.setattr(d, "daemon_generate_speech", gen)
    monkeypatch.setattr(d, "daemon_play_audio", lambda *a, **k: events.append(("play",)))
    return events


class TestSpeakerTransition:
    WHO = "Connery of claude house in tts room"

    def run(self, mode, who=WHO):
        d.speaker_transition(mode, "a", "b", "tts", "claude-connery", 1.8, "playback", who=who)

    def test_chime_is_the_boop_alone(self, transition):
        self.run("chime")
        assert transition == [("chime",)]

    def test_announce_is_the_words_in_the_speakers_voice(self, transition):
        self.run("announce")
        assert transition == [("say", self.WHO, "claude-connery"), ("play",)]

    def test_chime_then_announce(self, transition):
        self.run("chime+announce")
        assert transition == [("chime",), ("say", self.WHO, "claude-connery"), ("play",)]

    def test_none_is_silence(self, transition):
        self.run("none")
        assert transition == []

    def test_announce_without_who_keeps_project_says(self, transition):
        self.run("announce", who="")
        assert transition[0] == ("say", "tts says:", "claude-connery")


@pytest.fixture
def room(tmp_path, monkeypatch):
    """A session file store and a room id, for the name command and the hook message."""
    monkeypatch.setattr(config, "TTS_SESSIONS_DIR", tmp_path / "sessions.d")
    monkeypatch.setattr(config, "VOICE_CARDS_DIR", tmp_path / "voice.d")
    monkeypatch.setattr(config, "TTS_CONFIG_FILE", tmp_path / "config.json")
    (tmp_path / "config.json").write_text('{"queue": {"speaker_transition": "chime+announce"}}')
    sid = "alice--claude--claude-code-tts"
    monkeypatch.setenv("CLAUDE_TTS_SESSION", sid)
    return sid


class TestNameCommand:
    def test_set_show_and_reset(self, room, capsys):
        import argparse

        cmd_name(argparse.Namespace(name="Connery"))
        out = capsys.readouterr().out
        assert "Name set: Connery" in out
        assert "Says:     Connery of claude house in tts room" in out
        assert config.session_read(room)["name"] == "Connery"

        cmd_name(argparse.Namespace(name=None))
        out = capsys.readouterr().out
        assert "Name:     Connery" in out and "House:    claude" in out and "Room:     tts" in out
        assert "Usage: /tts-name" in out

        cmd_name(argparse.Namespace(name="reset"))
        assert config.session_read(room)["name"] == ""
        assert "introduces itself as Friend" in capsys.readouterr().out
        cmd_name(argparse.Namespace(name=None))
        assert "Friend of claude house in tts room" in capsys.readouterr().out

    def test_says_when_the_transition_will_not_speak_it(self, room, tmp_path, capsys):
        import argparse

        (tmp_path / "config.json").write_text('{"queue": {"speaker_transition": "chime"}}')
        cmd_name(argparse.Namespace(name=None))
        assert 'set it to "chime+announce"' in capsys.readouterr().out

    def test_too_long_is_refused(self, room):
        import argparse

        with pytest.raises(SystemExit):
            cmd_name(argparse.Namespace(name="x" * 65))


class TestTheNameRidesTheMessageAndTheCard:
    def test_hook_message_and_voice_card_carry_the_name(self, room, tmp_path, monkeypatch):
        config.session_set(room, "name", "Connery")
        cfg = config.load_config(room)
        assert cfg.name == "Connery"
        assert config.voice_card(cfg)["name"] == "Connery"
        written: list[dict] = []
        monkeypatch.setattr(audio.msgqueue, "write_message", lambda m: (written.append(m) or (tmp_path / "q.json", m)))
        audio.write_queue_message("hello", cfg)
        assert written[0]["name"] == "Connery"

    def test_no_name_means_no_key(self, room, tmp_path, monkeypatch):
        cfg = config.load_config(room)
        written: list[dict] = []
        monkeypatch.setattr(audio.msgqueue, "write_message", lambda m: (written.append(m) or (tmp_path / "q.json", m)))
        audio.write_queue_message("hello", cfg)
        assert "name" not in written[0]
        assert config.voice_card(cfg)["name"] == ""
