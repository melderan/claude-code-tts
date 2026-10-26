"""Tests for cli.py — command handler integration tests."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

import claude_code_tts.config as config_mod
from claude_code_tts.cli import main


@pytest.fixture
def tts_home(tmp_path):
    """Create a fake HOME with TTS directory structure and config."""
    tts_dir = tmp_path / ".claude-tts"
    tts_dir.mkdir()
    sessions_dir = tts_dir / "sessions.d"
    sessions_dir.mkdir()
    projects_dir = tmp_path / ".claude" / "projects"
    projects_dir.mkdir(parents=True)

    config = {
        "version": 1,
        "mode": "direct",
        "muted": False,
        "default_muted": False,
        "active_persona": "claude-prime",
        "personas": {
            "claude-prime": {
                "description": "Default voice",
                "voice": "en_US-hfc_male-medium",
                "speed": 2.0,
                "speed_method": "playback",
                "max_chars": 10000,
                "ai_type": "claude",
            },
            "claude-chill": {
                "description": "Relaxed voice",
                "voice": "en_US-joe-medium",
                "speed": 1.5,
                "speed_method": "length_scale",
                "max_chars": 5000,
                "ai_type": "claude",
            },
        },
    }
    (tts_dir / "config.json").write_text(json.dumps(config))
    return tmp_path


@pytest.fixture
def patched_env(tts_home):
    """Patch config paths and session ID to use fake home."""
    with patch.object(config_mod, "HOME", tts_home), \
         patch.object(config_mod, "TTS_CONFIG_DIR", tts_home / ".claude-tts"), \
         patch.object(config_mod, "TTS_CONFIG_FILE", tts_home / ".claude-tts" / "config.json"), \
         patch.object(config_mod, "TTS_SESSIONS_DIR", tts_home / ".claude-tts" / "sessions.d"), \
         patch.object(config_mod, "PROJECTS_DIR", tts_home / ".claude" / "projects"), \
         patch("claude_code_tts.cli.TTS_CONFIG_FILE", tts_home / ".claude-tts" / "config.json"), \
         patch("claude_code_tts.cli.TTS_SESSIONS_DIR", tts_home / ".claude-tts" / "sessions.d"), \
         patch("claude_code_tts.cli.get_session_id", return_value="test-session"):
        yield


class TestMuteUnmute:
    def test_mute_creates_session_file(self, tts_home, patched_env, capsys):
        main(["mute"])
        sf = tts_home / ".claude-tts" / "sessions.d" / "test-session.json"
        assert sf.exists()
        data = json.loads(sf.read_text())
        assert data["muted"] is True
        assert "muted" in capsys.readouterr().out.lower()

    def test_unmute_creates_session_file(self, tts_home, patched_env, capsys):
        main(["unmute"])
        sf = tts_home / ".claude-tts" / "sessions.d" / "test-session.json"
        assert sf.exists()
        data = json.loads(sf.read_text())
        assert data["muted"] is False
        assert "unmuted" in capsys.readouterr().out.lower()

    def test_mute_then_unmute(self, tts_home, patched_env):
        main(["mute"])
        sf = tts_home / ".claude-tts" / "sessions.d" / "test-session.json"
        assert json.loads(sf.read_text())["muted"] is True

        main(["unmute"])
        assert json.loads(sf.read_text())["muted"] is False

    def test_mute_all(self, tts_home, patched_env, capsys):
        # Create some session files first
        sd = tts_home / ".claude-tts" / "sessions.d"
        (sd / "s1.json").write_text(json.dumps({"muted": False}))
        (sd / "s2.json").write_text(json.dumps({"muted": False}))

        main(["mute", "--all"])

        assert json.loads((sd / "s1.json").read_text())["muted"] is True
        assert json.loads((sd / "s2.json").read_text())["muted"] is True

        config = json.loads((tts_home / ".claude-tts" / "config.json").read_text())
        assert config["default_muted"] is True
        assert config["muted"] is True

    def test_unmute_all_clears_flags_and_the_default(self, tts_home, patched_env, capsys):
        """JMO, 2026-09-29: new sessions should speak, so he never starts one and forgets to unmute."""
        sd = tts_home / ".claude-tts" / "sessions.d"
        (sd / "s1.json").write_text(json.dumps({"muted": True, "persona": "claude-chill"}))
        (sd / "s2.json").write_text(json.dumps({"muted": True}))
        main(["mute", "--all"])
        main(["unmute", "--all"])
        assert json.loads((sd / "s1.json").read_text()) == {"persona": "claude-chill"}  # flag gone, persona kept
        assert "muted" not in json.loads((sd / "s2.json").read_text())
        config = json.loads((tts_home / ".claude-tts" / "config.json").read_text())
        assert config["default_muted"] is False and config["muted"] is False
        assert "new ones start speaking" in capsys.readouterr().out

    def test_unmute_room_unmutes_both_ids_of_one_room_and_no_other(self, tts_home, patched_env, capsys):
        """Haley 2026-10-13: unmuting one room took a hand edit of two files (its folder id and its room id)."""
        sd = tts_home / ".claude-tts" / "sessions.d"
        mine = ["alice--claude--widgets", "-Users-alice-code-acme-widgets"]
        others = ["alice--claude--gizmos", "-Users-alice-code-acme-gizmos", "-Users-alice-code-acme-blue-widgets-old"]
        for sid in mine + others:
            (sd / f"{sid}.json").write_text(json.dumps({"muted": True, "persona": "claude-chill"}))
        main(["unmute", "--room", "widgets"])
        for sid in mine:
            assert json.loads((sd / f"{sid}.json").read_text()) == {"muted": False, "persona": "claude-chill"}
        for sid in others:
            assert json.loads((sd / f"{sid}.json").read_text())["muted"] is True, sid
        out = capsys.readouterr().out
        assert "Unmuted: alice--claude--widgets" in out and "2 session(s) of room widgets unmuted" in out
        config = json.loads((tts_home / ".claude-tts" / "config.json").read_text())
        assert config["default_muted"] is False and config["muted"] is False, "the default is not touched"

    def test_mute_room_by_tag_and_by_full_id(self, tts_home, patched_env):
        sd = tts_home / ".claude-tts" / "sessions.d"
        (sd / "alice--claude--claude-code-tts.json").write_text("{}")
        (sd / "alice--claude--notes.json").write_text("{}")
        main(["mute", "--room", "tts"])
        assert json.loads((sd / "alice--claude--claude-code-tts.json").read_text())["muted"] is True
        assert "muted" not in json.loads((sd / "alice--claude--notes.json").read_text())
        main(["mute", "--room", "alice--claude--notes"])
        assert json.loads((sd / "alice--claude--notes.json").read_text())["muted"] is True

    def test_a_room_with_no_session_changes_nothing_and_exits_1(self, tts_home, patched_env, capsys):
        (tts_home / ".claude-tts" / "sessions.d" / "alice--claude--notes.json").write_text("{}")
        with pytest.raises(SystemExit) as e:
            main(["unmute", "--room", "nope"])
        assert e.value.code == 1
        assert "nothing changed" in capsys.readouterr().out
        assert json.loads((tts_home / ".claude-tts" / "sessions.d" / "alice--claude--notes.json").read_text()) == {}

    def test_room_and_all_are_one_or_the_other(self, tts_home, patched_env):
        with pytest.raises(SystemExit) as e:
            main(["unmute", "--all", "--room", "tts"])
        assert e.value.code == 2


class TestSpeed:
    def test_show_speed(self, tts_home, patched_env, capsys):
        main(["speed"])
        out = capsys.readouterr().out
        assert "2.0x" in out

    def test_set_speed(self, tts_home, patched_env):
        main(["speed", "1.5"])
        sf = tts_home / ".claude-tts" / "sessions.d" / "test-session.json"
        data = json.loads(sf.read_text())
        assert data["speed"] == 1.5

    def test_reset_speed(self, tts_home, patched_env, capsys):
        # Set speed first
        main(["speed", "1.5"])
        main(["speed", "reset"])
        sf = tts_home / ".claude-tts" / "sessions.d" / "test-session.json"
        data = json.loads(sf.read_text())
        assert "speed" not in data

    def test_invalid_speed(self, tts_home, patched_env):
        with pytest.raises(SystemExit):
            main(["speed", "abc"])

    def test_speed_out_of_range(self, tts_home, patched_env):
        with pytest.raises(SystemExit):
            main(["speed", "10.0"])


class TestPersona:
    def test_list_personas(self, tts_home, patched_env, capsys):
        main(["persona"])
        out = capsys.readouterr().out
        assert "claude-prime" in out
        assert "claude-chill" in out

    def test_set_persona(self, tts_home, patched_env, capsys):
        main(["persona", "claude-chill"])
        sf = tts_home / ".claude-tts" / "sessions.d" / "test-session.json"
        data = json.loads(sf.read_text())
        assert data["persona"] == "claude-chill"

    def test_set_invalid_persona(self, tts_home, patched_env):
        with pytest.raises(SystemExit):
            main(["persona", "nonexistent"])

    def test_reset_persona(self, tts_home, patched_env):
        main(["persona", "claude-chill"])
        main(["persona", "reset"])
        sf = tts_home / ".claude-tts" / "sessions.d" / "test-session.json"
        data = json.loads(sf.read_text())
        assert "persona" not in data

    def test_project_persona(self, tts_home, patched_env, capsys):
        main(["persona", "--project", "claude-chill"])
        config = json.loads((tts_home / ".claude-tts" / "config.json").read_text())
        assert config["project_personas"]["test-session"] == "claude-chill"


class TestPersonaAddRemove:
    """`claude-tts persona add|remove` writes the same shape the installer seeds."""

    def _config(self, tts_home):
        return json.loads((tts_home / ".claude-tts" / "config.json").read_text())

    def test_add_writes_full_persona_with_defaults(self, tts_home, patched_env, capsys):
        main(["persona", "add", "house-geordi", "--voice", "en_GB-alan-medium"])
        entry = self._config(tts_home)["personas"]["house-geordi"]
        assert entry["voice"] == "en_GB-alan-medium"
        assert entry["speed"] == 2.0
        assert entry["speed_method"] == "playback"
        assert entry["max_chars"] == 10000
        assert entry["ai_type"] == "claude"
        assert entry["description"]
        out = capsys.readouterr().out
        assert "Persona added: house-geordi" in out
        assert "not installed on this machine" in out
        assert "claude-tts-install --voice en_GB-alan-medium" in out

    def test_add_takes_every_field_and_sets_project_and_session(self, tts_home, patched_env):
        main(["persona", "add", "room-x", "--voice", "en_US-joe-medium", "--speed", "1.5",
              "--speed-method", "length_scale", "--description", "A room", "--max-chars", "5000",
              "--project", "--session"])
        config = self._config(tts_home)
        assert config["personas"]["room-x"] == {
            "description": "A room", "voice": "en_US-joe-medium", "speed": 1.5,
            "speed_method": "length_scale", "max_chars": 5000, "ai_type": "claude",
        }
        assert config["project_personas"]["test-session"] == "room-x"
        sf = tts_home / ".claude-tts" / "sessions.d" / "test-session.json"
        assert json.loads(sf.read_text())["persona"] == "room-x"

    def test_add_with_sherpa_and_kokoro(self, tts_home, patched_env):
        main(["persona", "add", "k", "--kokoro", "af_heart"])
        main(["persona", "add", "s", "--sherpa", "vctk-vits", "--speaker", "42"])
        personas = self._config(tts_home)["personas"]
        assert personas["k"]["voice_kokoro"] == "af_heart"
        assert personas["k"]["voice"] == "en_US-hfc_male-medium"
        assert personas["s"]["voice_sherpa"] == "vctk-vits"
        assert personas["s"]["speaker_sherpa"] == 42

    def test_add_speaker_without_sherpa_is_the_piper_speaker(self, tts_home, patched_env):
        main(["persona", "add", "w", "--voice", "en_GB-vctk-medium", "--speaker", "12"])
        entry = self._config(tts_home)["personas"]["w"]
        assert (entry["voice"], entry["speaker_piper"]) == ("en_GB-vctk-medium", 12)
        assert "speaker_sherpa" not in entry

    def test_add_refuses_a_speaker_the_installed_model_lacks(self, tts_home, patched_env, capsys, monkeypatch, tmp_path):
        voices = tmp_path / "voices"
        voices.mkdir()
        (voices / "en_GB-vctk-medium.onnx.json").write_text(json.dumps({"num_speakers": 109}))
        monkeypatch.setattr("claude_code_tts.cli.VOICES_DIR", voices)
        with pytest.raises(SystemExit) as exc:
            main(["persona", "add", "w", "--voice", "en_GB-vctk-medium", "--speaker", "109"])
        assert exc.value.code == 2
        assert "109 speaker(s), ids 0-108" in capsys.readouterr().out
        assert "w" not in self._config(tts_home).get("personas", {})
        with pytest.raises(SystemExit) as exc:
            main(["persona", "add", "w", "--voice", "en_GB-vctk-medium", "--speaker", "-1"])
        assert exc.value.code == 2

    def test_add_refuses_to_overwrite_without_force(self, tts_home, patched_env, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["persona", "add", "claude-chill", "--voice", "en_US-joe-medium"])
        assert exc.value.code == 1
        assert "Persona exists: claude-chill" in capsys.readouterr().out
        main(["persona", "add", "claude-chill", "--voice", "en_GB-alan-medium", "--force"])
        assert self._config(tts_home)["personas"]["claude-chill"]["voice"] == "en_GB-alan-medium"

    @pytest.mark.parametrize("argv", [
        ["persona", "add"],
        ["persona", "add", "Bad Name", "--voice", "x"],
        ["persona", "add", "ok", "--speed", "9"],
        ["persona", "add", "ok", "--speaker", "3"],
    ])
    def test_add_rejects_bad_input(self, tts_home, patched_env, argv):
        with pytest.raises(SystemExit) as exc:
            main(argv)
        assert exc.value.code == 2
        assert "ok" not in self._config(tts_home)["personas"]

    def test_remove_drops_persona(self, tts_home, patched_env, capsys):
        main(["persona", "remove", "claude-chill"])
        assert "claude-chill" not in self._config(tts_home)["personas"]
        assert "Persona removed: claude-chill" in capsys.readouterr().out

    def test_remove_refuses_global_and_referenced_personas(self, tts_home, patched_env, capsys):
        with pytest.raises(SystemExit):
            main(["persona", "remove", "claude-prime"])
        assert "global persona" in capsys.readouterr().out
        main(["persona", "--project", "claude-chill"])
        with pytest.raises(SystemExit):
            main(["persona", "remove", "claude-chill"])
        assert "project persona for: test-session" in capsys.readouterr().out
        main(["persona", "remove", "claude-chill", "--force"])
        config = self._config(tts_home)
        assert "claude-chill" not in config["personas"]
        assert "project_personas" not in config

    def test_remove_unknown_persona_fails(self, tts_home, patched_env):
        with pytest.raises(SystemExit) as exc:
            main(["persona", "remove", "nope"])
        assert exc.value.code == 1


class TestPersonasGuide:
    """`claude-tts personas` — sibling-Claude voice picker guide."""

    def test_personas_lists_all_visible(self, tts_home, patched_env, capsys):
        main(["personas"])
        out = capsys.readouterr().out
        assert "claude-prime" in out
        assert "claude-chill" in out
        assert "PICK YOUR VOICE" in out
        assert "HOW TO AUDITION" in out
        assert "HOW TO COMMIT" in out

    def test_personas_includes_vibe_for_known_voice(self, tts_home, patched_env, capsys):
        main(["personas"])
        out = capsys.readouterr().out
        # claude-prime uses en_US-hfc_male-medium
        assert "American male" in out

    def test_personas_hides_random_by_default(self, tts_home, patched_env, capsys):
        cfg_path = tts_home / ".claude-tts" / "config.json"
        cfg = json.loads(cfg_path.read_text())
        cfg["personas"]["random-1234567890"] = {
            "description": "Random",
            "voice": "en_US-amy-medium",
            "speed": 1.5,
            "ai_type": "claude",
        }
        cfg_path.write_text(json.dumps(cfg))

        main(["personas"])
        out = capsys.readouterr().out
        assert "random-1234567890" not in out
        assert "1 random-* personas hidden" in out

    def test_personas_include_random_flag(self, tts_home, patched_env, capsys):
        cfg_path = tts_home / ".claude-tts" / "config.json"
        cfg = json.loads(cfg_path.read_text())
        cfg["personas"]["random-1234567890"] = {
            "description": "Random",
            "voice": "en_US-amy-medium",
            "speed": 1.5,
            "ai_type": "claude",
        }
        cfg_path.write_text(json.dumps(cfg))

        main(["personas", "--include-random"])
        out = capsys.readouterr().out
        assert "random-1234567890" in out
        assert "hidden" not in out

    def test_personas_marks_project_persona(self, tts_home, patched_env, capsys):
        main(["persona", "--project", "claude-chill"])
        capsys.readouterr()  # drain
        main(["personas"])
        out = capsys.readouterr().out
        # claude-chill should be tagged as the project persona
        chill_line_idx = out.index("claude-chill")
        # Find the line containing claude-chill and verify [project] marker is on it
        line_with_chill = out[chill_line_idx:].split("\n", 1)[0]
        assert "[project]" in line_with_chill


class TestIntermediate:
    def test_show_intermediate(self, tts_home, patched_env, capsys):
        main(["intermediate"])
        out = capsys.readouterr().out
        assert "ENABLED" in out

    def test_disable_intermediate(self, tts_home, patched_env):
        main(["intermediate", "off"])
        sf = tts_home / ".claude-tts" / "sessions.d" / "test-session.json"
        data = json.loads(sf.read_text())
        assert data["intermediate"] is False

    def test_enable_intermediate(self, tts_home, patched_env):
        main(["intermediate", "off"])
        main(["intermediate", "on"])
        sf = tts_home / ".claude-tts" / "sessions.d" / "test-session.json"
        data = json.loads(sf.read_text())
        assert data["intermediate"] is True


class TestStatus:
    def test_status_output(self, tts_home, patched_env, capsys):
        main(["status"])
        out = capsys.readouterr().out
        assert "test-session" in out
        assert "claude-prime" in out
        assert "direct" in out


class TestMode:
    def test_show_mode(self, tts_home, patched_env, capsys):
        main(["mode"])
        out = capsys.readouterr().out
        assert "direct" in out

    def test_set_mode(self, tts_home, patched_env, capsys):
        main(["mode", "queue"])
        config = json.loads((tts_home / ".claude-tts" / "config.json").read_text())
        assert config["mode"] == "queue"


class TestVersion:
    def test_version(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["--version"])
        assert exc.value.code == 0

    def test_no_args_shows_help(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main([])
        assert exc.value.code == 0


class TestDefaultSpeed:
    """`speed --default` writes persona speed, the value every session inherits."""

    def _config(self, tts_home):
        return json.loads((tts_home / ".claude-tts" / "config.json").read_text())

    def test_default_sets_active_persona_only(self, tts_home, patched_env, capsys):
        before = self._config(tts_home)
        active = before["active_persona"]
        other = next(n for n in before["personas"] if n != active)
        main(["speed", "--default", "1.0"])
        after = self._config(tts_home)
        assert after["personas"][active]["speed"] == 1.0
        assert after["personas"][other]["speed"] == before["personas"][other]["speed"]
        assert "Default speed set to 1.0x" in capsys.readouterr().out
        # No session override was written
        sf = tts_home / ".claude-tts" / "sessions.d" / "test-session.json"
        assert not sf.exists() or "speed" not in json.loads(sf.read_text())

    def test_default_all_sets_every_persona(self, tts_home, patched_env):
        main(["speed", "--default", "--all", "1.0"])
        after = self._config(tts_home)
        assert {p["speed"] for p in after["personas"].values()} == {1.0}

    def test_default_follows_session_persona(self, tts_home, patched_env):
        main(["persona", "claude-chill"])
        main(["speed", "--default", "1.2"])
        after = self._config(tts_home)
        assert after["personas"]["claude-chill"]["speed"] == 1.2
        assert after["personas"]["claude-prime"]["speed"] != 1.2

    def test_default_respects_range(self, tts_home, patched_env):
        with pytest.raises(SystemExit):
            main(["speed", "--default", "9"])


class TestSpeechFailureExplained:
    """Direct `claude-tts speak` says why synthesis failed (issue #1)."""

    def test_piper_missing_gives_install_hint(self):
        from claude_code_tts import audio, cli
        with patch.object(audio, "_LAST_ERROR", "piper not on PATH (/usr/bin:/bin)"):
            msg = cli._explain_speech_failure()
        assert msg.startswith("Failed to generate speech: piper not on PATH")
        assert "/usr/bin" not in msg.splitlines()[0]
        assert "uv tool install piper-tts" in msg

    def test_missing_voice_names_the_download(self):
        from claude_code_tts import audio, cli
        with patch.object(audio, "_LAST_ERROR", "voice model missing: /x/piper-voices/en_GB-alan-medium.onnx"):
            msg = cli._explain_speech_failure()
        assert "claude-tts-install --voice en_GB-alan-medium" in msg

    def test_unknown_reason_still_points_at_the_log(self):
        from claude_code_tts import audio, cli
        with patch.object(audio, "_LAST_ERROR", ""):
            msg = cli._explain_speech_failure()
        assert "no backend produced audio" in msg and "debug.log" in msg


class TestVoices:
    """`claude-tts voices`: one answer to "what voices can I choose, from which providers?"."""

    def test_lists_every_provider_with_state_and_users(self, tts_home, patched_env, capsys, tmp_path, monkeypatch):
        import claude_code_tts.config as cfg_mod
        voices_dir = tmp_path / "piper-voices"
        voices_dir.mkdir()
        (voices_dir / "en_US-hfc_male-medium.onnx").write_bytes(b"x")
        (voices_dir / "en_US-libritts_r-medium.onnx").write_bytes(b"x")
        (voices_dir / "en_US-libritts_r-medium.onnx.json").write_text(json.dumps({"num_speakers": 904}))
        monkeypatch.setattr(cfg_mod, "VOICES_DIR", voices_dir)
        monkeypatch.setattr(cfg_mod, "SHERPA_MODELS_DIR", tmp_path / "sherpa-models")
        # cli imports these names at module level, so patch where they are read
        monkeypatch.setattr("claude_code_tts.cli.VOICES_DIR", voices_dir)
        monkeypatch.setattr("claude_code_tts.cli.SHERPA_MODELS_DIR", tmp_path / "sherpa-models")
        monkeypatch.setattr(cfg_mod, "MLX_VENV_DIR", tmp_path / "venvs" / "mlx")
        with patch("shutil.which", return_value=None), \
             patch("claude_code_tts.cli._hf_model_cached", return_value=False):
            main(["voices"])
        out = capsys.readouterr().out
        assert "Piper" in out and "en_US-hfc_male-medium" in out
        assert "<- persona claude-prime" in out                       # who uses it
        assert "904 speakers" in out and "--speakers 20" in out        # multi-speaker hint
        assert "swift-kokoro: not installed" in out
        assert "sherpa-onnx (not enabled" in out
        assert "mlx-audio (not enabled" in out and "not fetched: claude-tts mlx pull kokoro" in out
        assert "claude-tts audition --mlx kokoro" in out               # how to hear one
        assert "docs/voice-notes.md" in out

    def test_sessions_section_names_the_clash(self, tts_home, patched_env, capsys, tmp_path, monkeypatch):
        """JMO 2026-09-30: six brothers, three unique voices; the rest fell through to the default."""
        import claude_code_tts.config as cfg_mod
        sd = tts_home / ".claude-tts" / "sessions.d"
        (sd / "-Users-x-repos-a.json").write_text(json.dumps({"muted": False}))                 # default persona
        (sd / "-Users-x-repos-b.json").write_text(json.dumps({"muted": False}))                 # default persona
        (sd / "-Users-x-repos-c.json").write_text(json.dumps({"persona": "claude-prime"}))      # chosen explicitly, same voice
        cfg_path = tts_home / ".claude-tts" / "config.json"
        cfg = json.loads(cfg_path.read_text())
        cfg["personas"]["solo"] = {"voice": "en_GB-alan-medium", "speed": 2.0}
        cfg["project_personas"] = {"-Users-x-repos-d": "solo"}
        cfg_path.write_text(json.dumps(cfg))
        monkeypatch.setattr(cfg_mod, "VOICES_DIR", tmp_path / "none")
        monkeypatch.setattr(cfg_mod, "SHERPA_MODELS_DIR", tmp_path / "none")
        monkeypatch.setattr(cfg_mod, "MLX_VENV_DIR", tmp_path / "none")
        with patch("shutil.which", return_value=None), patch("claude_code_tts.cli._hf_model_cached", return_value=False):
            main(["voices"])
        out = capsys.readouterr().out
        assert "claude-prime (default) -> piper en_US-hfc_male-medium  SAME VOICE as 2 other sessions" in out
        assert "claude-prime (session) -> piper en_US-hfc_male-medium  SAME VOICE as 2 other sessions" in out
        assert "solo (project) -> piper en_GB-alan-medium\n" in out    # unique: no clash tag
        assert "CLAUDE_TTS_SESSION=<session id above> claude-tts persona <name> --project" in out

    def test_slash_command_is_installed(self):
        from claude_code_tts.install import MANIFEST
        assert "tts-voices.md" in MANIFEST["commands"]
        assert (Path(__file__).parent.parent / "commands" / "tts-voices.md").is_file()


class TestVoiceClashes:
    """`claude-tts voices --clashes`: two personas that would sound the same, found from config alone.

    Voices built from a description can come out too alike, and that takes a listener to judge.
    Same engine and same speaker is the case that needs no audio to call.
    """

    @staticmethod
    def _run(tts_home, monkeypatch, personas, cards=None, swift_kokoro=False):
        cfg_path = tts_home / ".claude-tts" / "config.json"
        cfg = json.loads(cfg_path.read_text())
        cfg["personas"] = personas
        cfg_path.write_text(json.dumps(cfg))
        cards_dir = tts_home / ".claude-tts" / "voice.d"
        cards_dir.mkdir()
        for sid, persona in (cards or {}).items():
            (cards_dir / f"{sid}.json").write_text(json.dumps({"schema": 1, "session": sid, "persona": persona}))
        monkeypatch.setattr(config_mod, "VOICE_CARDS_DIR", cards_dir)
        with patch("shutil.which", return_value="/usr/bin/swift-kokoro" if swift_kokoro else None):
            with pytest.raises(SystemExit) as exc:
                main(["voices", "--clashes"])
        return exc.value.code

    def test_same_kokoro_preset_is_one_voice_whatever_the_speed(self, tts_home, patched_env, capsys, monkeypatch):
        kokoro = "mlx-community/Kokoro-82M-bf16"
        code = self._run(tts_home, monkeypatch, {
            "older": {"voice_mlx": kokoro, "speaker_mlx": "bm_george", "lang_mlx": "b", "speed": 1.8},
            "newer": {"voice_mlx": kokoro, "speaker_mlx": "bm_george", "lang_mlx": "b", "speed": 2.0},
            "other": {"voice_mlx": kokoro, "speaker_mlx": "am_michael", "speed": 1.8},
        }, cards={"alice--claude--forge": "newer"})
        out = capsys.readouterr().out
        assert code == 1
        assert "  kokoro bm_george\n    newer: alice--claude--forge\n    older: no session speaks as it\n" in out
        assert "other" not in out

    def test_a_preset_clashes_across_swift_kokoro_and_mlx(self, tts_home, patched_env, capsys, monkeypatch):
        personas = {
            "mlx-emma": {"voice_mlx": "mlx-community/Kokoro-82M-bf16", "speaker_mlx": "bf_emma"},
            "emma": {"voice_kokoro": "bf_emma"},
        }
        assert self._run(tts_home, monkeypatch, personas, swift_kokoro=True) == 1
        assert "  kokoro bf_emma\n    emma: no session speaks as it\n    mlx-emma:" in capsys.readouterr().out

    def test_without_swift_kokoro_a_persona_sounds_like_what_plays(self, tts_home, patched_env, capsys, monkeypatch):
        """tts_speak skips voice_kokoro when swift-kokoro is absent: emma falls to Piper, mlx-emma to mlx."""
        personas = {
            "mlx-emma": {"voice_kokoro": "bf_emma", "voice_mlx": "mlx-community/Kokoro-82M-bf16", "speaker_mlx": "bf_emma"},
            "emma": {"voice_kokoro": "bf_emma", "voice": "en_GB-alan-medium"},
        }
        assert self._run(tts_home, monkeypatch, personas) == 0
        assert "No two personas share a voice." in capsys.readouterr().out

    def test_a_speaker_or_a_pitch_filter_makes_another_voice(self, tts_home, patched_env, capsys, monkeypatch):
        lib = "en_US-libritts_r-medium"
        code = self._run(tts_home, monkeypatch, {
            "s1": {"voice": lib, "speaker_piper": 1},
            "s2": {"voice": lib, "speaker_piper": 2},
            "s2-low": {"voice": lib, "speaker_piper": 2, "pitch_filter": "asetrate=19000"},
            "s2-again": {"voice": lib, "speaker_piper": 2, "speed": 3.0},
        })
        out = capsys.readouterr().out
        assert code == 1
        assert f"  piper {lib} speaker 2\n    s2: no session speaks as it\n    s2-again:" in out
        assert "s1" not in out and "s2-low" not in out

    def test_plain_voices_points_at_the_clashes(self, tts_home, patched_env, capsys, tmp_path, monkeypatch):
        cfg_path = tts_home / ".claude-tts" / "config.json"
        cfg = json.loads(cfg_path.read_text())
        cfg["personas"]["prime-twin"] = dict(cfg["personas"]["claude-prime"], speed=3.0)
        cfg_path.write_text(json.dumps(cfg))
        for name in ("VOICES_DIR", "SHERPA_MODELS_DIR", "MLX_VENV_DIR"):
            monkeypatch.setattr(config_mod, name, tmp_path / "none")
        with patch("shutil.which", return_value=None), patch("claude_code_tts.cli._hf_model_cached", return_value=False):
            main(["voices"])
        assert "2 personas sound the same as another persona: claude-tts voices --clashes" in capsys.readouterr().out


class TestVoicesRooms:
    """`claude-tts voices --rooms`: the TSV a page reads to show each room's voice; read-only."""

    def test_one_row_per_session_with_stable_columns(self, tts_home, patched_env, capsys, monkeypatch):
        kokoro = "mlx-community/Kokoro-82M-bf16"
        tts = tts_home / ".claude-tts"
        cfg = json.loads((tts / "config.json").read_text())
        cfg["personas"]["canary-sky"] = {"voice_mlx": kokoro, "speaker_mlx": "af_sky", "placeholder": True}
        cfg["personas"]["fox"] = {"voice_mlx": kokoro, "speaker_mlx": "am_michael"}
        cfg["project_personas"] = {"acme--claude--canary": "canary-sky", "acme--gpt--gone": "retired"}
        (tts / "config.json").write_text(json.dumps(cfg))
        (tts / "sessions.d" / "acme--claude--fox.json").write_text(json.dumps({"persona": "fox", "name": "Fox\tMcCloud"}))
        cards = tts / "voice.d"
        cards.mkdir()
        (cards / "acme--gpt--quiet.json").write_text("{}")
        (cards / "-Users-dev-old.json").write_text("{}")
        monkeypatch.setattr(config_mod, "VOICE_CARDS_DIR", cards)
        before = sorted(p.name for p in tts.rglob("*"))
        with patch("shutil.which", return_value=None), pytest.raises(SystemExit) as exc:
            main(["voices", "--rooms"])
        assert exc.value.code == 0
        lines = capsys.readouterr().out.splitlines()
        assert lines[0] == "session\tkind\tname\tpersona\tfrom\tvoice\tplaceholder\tshares"
        assert lines[1:] == [
            "-Users-dev-old\tdirectory\t-\tclaude-prime\tdefault\tpiper en_US-hfc_male-medium\tfalse\tacme--gpt--quiet",
            "acme--claude--canary\troom\t-\tcanary-sky\tproject\tkokoro af_sky\ttrue\t-",
            "acme--claude--fox\troom\tFox McCloud\tfox\tsession\tkokoro am_michael\tfalse\t-",
            "acme--gpt--gone\troom\t-\tretired\tproject\t? (no such persona)\tfalse\t-",
            "acme--gpt--quiet\troom\t-\tclaude-prime\tdefault\tpiper en_US-hfc_male-medium\tfalse\t-Users-dev-old",
        ]
        assert sorted(p.name for p in tts.rglob("*")) == before


class TestInheritSessionSettings:
    """A session id from CLAUDE_TTS_SESSION inherits its directory's settings once (JMO 2026-09-30:
    'oh no your voice isn't right' after every rebuilt room came up as claude-prime)."""

    TRANSCRIPT = "/home/x/.claude/projects/-Users-dev-repo/uuid.jsonl"

    def _sd(self, tts_home):
        return tts_home / ".claude-tts" / "sessions.d"

    def test_copies_the_directory_session_file(self, tts_home, patched_env):
        from claude_code_tts.cli import _inherit_session_settings
        sd = self._sd(tts_home)
        (sd / "-Users-dev-repo.json").write_text(json.dumps({"muted": False, "persona": "claude-chill", "speed": 1.8}))
        assert _inherit_session_settings("room-a", self.TRANSCRIPT) is True
        assert json.loads((sd / "room-a.json").read_text()) == {"muted": False, "persona": "claude-chill", "speed": 1.8}

    def test_project_persona_pin_becomes_the_session_persona(self, tts_home, patched_env):
        from claude_code_tts.cli import _inherit_session_settings
        cfg_path = tts_home / ".claude-tts" / "config.json"
        config = json.loads(cfg_path.read_text())
        config["project_personas"] = {"-Users-dev-repo": "claude-chill"}
        cfg_path.write_text(json.dumps(config))
        assert _inherit_session_settings("room-a", self.TRANSCRIPT) is True
        assert json.loads((self._sd(tts_home) / "room-a.json").read_text()) == {"persona": "claude-chill"}

    def test_existing_session_file_is_never_touched(self, tts_home, patched_env):
        from claude_code_tts.cli import _inherit_session_settings
        sd = self._sd(tts_home)
        (sd / "-Users-dev-repo.json").write_text(json.dumps({"persona": "claude-chill"}))
        (sd / "room-a.json").write_text(json.dumps({"persona": "claude-prime"}))
        assert _inherit_session_settings("room-a", self.TRANSCRIPT) is False
        assert json.loads((sd / "room-a.json").read_text()) == {"persona": "claude-prime"}

    def test_nothing_to_inherit_writes_nothing(self, tts_home, patched_env):
        from claude_code_tts.cli import _inherit_session_settings
        assert _inherit_session_settings("room-a", self.TRANSCRIPT) is False
        assert not (self._sd(tts_home) / "room-a.json").exists()

    def test_same_id_from_env_and_path_is_a_no_op(self, tts_home, patched_env):
        from claude_code_tts.cli import _inherit_session_settings
        assert _inherit_session_settings("-Users-dev-repo", self.TRANSCRIPT) is False


class TestConfigStoredVsDefault:
    """JMO 2026-09-30: tooling that can detect stored vs defaults that we can ask."""

    def _set(self, tts_home, **kv):
        cfg_path = tts_home / ".claude-tts" / "config.json"
        config = json.loads(cfg_path.read_text())
        config.update(kv)
        cfg_path.write_text(json.dumps(config))

    def test_marks_a_stale_default_muted_as_changed(self, tts_home, patched_env, capsys):
        self._set(tts_home, default_muted=True, muted=True)
        main(["config"])
        out = capsys.readouterr().out
        lines = {row.split()[0]: row for row in out.splitlines() if row and row[0] not in " SDEK"}
        assert "CHANGED" in lines["default_muted"] and "true" in lines["default_muted"]
        assert "CHANGED" in lines["muted"]
        assert "CHANGED" not in lines["active_persona"]
        assert "not stored" in lines["queue.prefetch_next"]

    def test_changed_hides_matching_keys_and_json_is_parseable(self, tts_home, patched_env, capsys):
        self._set(tts_home, default_muted=True, mic_aware_pause=True)
        main(["config", "--changed", "--json"])
        data = json.loads(capsys.readouterr().out)
        by_key = {r["key"]: r for r in data["rows"]}
        assert by_key["default_muted"]["note"] == "CHANGED"
        assert by_key["mic_aware_pause"]["note"].startswith("stored only")
        assert "active_persona" not in by_key

    def test_rows_compare_nested_leaves_and_keep_personas_whole(self):
        from claude_code_tts.cli import config_rows
        rows = {r["key"]: r for r in config_rows(
            {"queue": {"max_depth": 5}, "personas": {"a": {}, "b": {}}},
            {"queue": {"max_depth": 20, "idle_poll_ms": 100}, "personas": {"a": {}}},
        )}
        assert rows["queue.max_depth"]["note"] == "CHANGED"
        assert rows["queue.idle_poll_ms"]["note"].startswith("not stored")
        assert rows["personas"]["stored"] == "<2 entries>" and rows["personas"]["note"] == "CHANGED"
