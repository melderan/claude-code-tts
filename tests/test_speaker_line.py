"""A session introduces itself when it takes the floor: the boop, then a line in its own voice.

The line comes from the config's `speaker_lines` rules (speakerline.py); the shipped "rooms" set
says "<name> of House <house>, in room <room>" for owner--house--room ids, "Friend" until named.
With no rule matching, "<name or project> says:". The name is a session key set with
`claude-tts name`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import claude_code_tts.audio as audio
import claude_code_tts.config as config
import claude_code_tts.daemon as d
from claude_code_tts.cli import cmd_name
from claude_code_tts.speakerline import speak_line
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


def rooms(msg: dict) -> str:
    return speak_line(msg, ["rooms"]).line


class TestTheRoomsRuleSet:
    def test_a_named_room_says_name_house_and_room(self):
        msg = {"session_id": "alice--claude--claude-code-tts", "name": "Connery", "project": "tts"}
        assert rooms(msg) == "Connery of House claude, in room tts"

    def test_an_unnamed_room_is_a_friend(self):
        assert rooms({"session_id": "alice--claude--k8s", "project": "k8s"}) == "Friend of House claude, in room k8s"

    def test_a_room_with_no_house_names_the_room_only(self):
        assert rooms({"session_id": "two--notes", "name": "Lyra"}) == "Lyra, in room notes"

    def test_more_segments_take_the_last_two_as_house_and_room(self):
        assert rooms({"session_id": "owner--a--b--c", "name": "Z"}) == "Z of House b, in room c"

    def test_a_plain_session_keeps_project_says(self):
        assert rooms({"session_id": "-Users-x-proj", "project": "proj"}) == "proj says:"

    def test_a_plain_session_with_a_name_says_the_name(self):
        assert rooms({"session_id": "-Users-x-proj", "project": "proj", "name": "Pat"}) == "Pat says:"

    @pytest.mark.parametrize("sid", ["-Users-alice--dotfiles", "-Users-alice-work-my--app--v2", "a---b"])
    def test_a_project_folder_with_a_double_dash_is_not_a_room(self, sid):
        assert rooms({"session_id": sid, "project": sid}) == f"{sid} says:"

    def test_a_page_with_no_session_says_its_label_not_its_source(self):
        """JMO 10-15: 'Still hearing "codex-tts codex says"'; the source is plumbing."""
        msg = {"session_id": "browser", "source": "codex-tts", "project": "codex-tts:Codex"}
        assert rooms(msg) == "Codex says:"
        assert rooms({**msg, "name": "Aster"}) == "Aster says:"
        assert rooms({"session_id": "browser", "source": "hear", "project": "hear"}) == "hear says:"
        assert rooms({"session_id": "x--y", "source": "s", "project": "s:L"}) != "L says:"

    def test_blank_name_is_no_name(self):
        assert rooms({"session_id": "a--h--r", "name": "   "}) == "Friend of House h, in room r"


class TestRules:
    def test_no_rules_is_the_plain_line_even_for_a_room_id(self):
        for entries in (None, []):
            assert speak_line({"session_id": "a--h--r", "project": "r"}, entries).line == "r says:"

    def test_a_custom_rule_fills_its_groups_and_wins_when_first(self):
        rule = {"match": r"(?P<team>\w+)--(?P<svc>\w+)", "say": "{name} from team {team} on {svc}", "default_name": "A teammate"}
        spoken = speak_line({"session_id": "infra--api", "project": "api"}, [rule, "rooms"])
        assert spoken.line == "A teammate from team infra on api"
        assert spoken.rule == "#0" and spoken.fields == {"team": "infra", "svc": "api"}

    def test_the_first_match_wins_and_later_rules_are_a_fallback(self):
        rule = {"match": r"never", "say": "x"}
        assert speak_line({"session_id": "a--h--r", "name": "N"}, [rule, "rooms"]).rule == "rooms[0]"

    def test_a_name_with_no_default_falls_back_to_the_project(self):
        rule = {"match": r".*", "say": "{name} is up"}
        assert speak_line({"session_id": "-x", "project": "proj"}, [rule]).line == "proj is up"

    @pytest.mark.parametrize(
        "entry, why",
        [
            ({"match": "(", "say": "x"}, "does not compile"),
            ({"match": ".*", "say": "{nope}"}, "say names nope"),
            ({"match": ".*"}, "needs string keys"),
            ("nosuchset", "no rule set named"),
        ],
    )
    def test_a_broken_rule_is_skipped_and_said_why(self, entry, why):
        spoken = speak_line({"session_id": "s", "project": "p"}, [entry])
        assert spoken.line == "p says:"
        assert any(why in s for s in spoken.skipped)

    def test_not_a_list_is_reported(self):
        assert speak_line({"session_id": "s", "project": "p"}, "rooms").skipped == ["speaker_lines is not a list"]

    def test_a_field_value_is_not_a_template(self):
        rule = {"match": r"(?P<w>.+)", "say": "{w} here"}
        assert speak_line({"session_id": "{name}"}, [rule]).line == "{name} here"

    def test_the_daemon_reads_the_rules_from_config(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "TTS_CONFIG_FILE", tmp_path / "config.json")
        msg = {"session_id": "a--h--r", "name": "N", "project": "r"}
        assert d.speaker_line(msg) == "N says:"
        (tmp_path / "config.json").write_text('{"speaker_lines": ["rooms"]}')
        assert d.speaker_line(msg) == "N of House h, in room r"


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
    WHO = "Connery of House claude, in room tts"

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
    (tmp_path / "config.json").write_text('{"queue": {"speaker_transition": "chime+announce"}, "speaker_lines": ["rooms"]}')
    sid = "alice--claude--claude-code-tts"
    monkeypatch.setenv("CLAUDE_TTS_SESSION", sid)
    return sid


class TestNameCommand:
    def test_set_show_and_reset(self, room, capsys):
        import argparse

        cmd_name(argparse.Namespace(name="Connery"))
        out = capsys.readouterr().out
        assert "Name set: Connery" in out
        assert "Says:     Connery of House claude, in room tts" in out
        assert config.session_read(room)["name"] == "Connery"

        cmd_name(argparse.Namespace(name=None))
        out = capsys.readouterr().out
        assert "Name:     Connery" in out and "House:    claude" in out and "Room:     tts" in out
        assert "Rule:     rooms[0]" in out
        assert "Usage: /tts-name" in out

        cmd_name(argparse.Namespace(name="reset"))
        assert config.session_read(room)["name"] == ""
        out = capsys.readouterr().out
        assert "Name cleared" in out and "Says:     Friend of House claude, in room tts" in out

    def test_shows_a_skipped_rule_and_the_plain_line(self, room, tmp_path, capsys):
        import argparse

        (tmp_path / "config.json").write_text('{"speaker_lines": [{"match": "(", "say": "x"}]}')
        cmd_name(argparse.Namespace(name=None))
        out = capsys.readouterr().out
        assert "Skipped:  #0: match does not compile" in out
        assert "Rule:     (none matched" in out and f"Says:     {room} says:" in out

    def test_shows_the_line_as_the_pronounce_map_says_it(self, room, tmp_path, capsys):
        import argparse

        cfg = '{"speaker_lines": ["rooms"], "pronounce": {"tts": "tee tee ess"}}'
        (tmp_path / "config.json").write_text(cfg)
        cmd_name(argparse.Namespace(name="Connery"))
        out = capsys.readouterr().out
        assert "Says:     Connery of House claude, in room tts" in out
        assert "Said as:  Connery of House claude, in room tee tee ess" in out

    def test_no_said_as_line_when_the_map_changes_nothing(self, room, capsys):
        import argparse

        cmd_name(argparse.Namespace(name="Connery"))
        assert "Said as:" not in capsys.readouterr().out

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
