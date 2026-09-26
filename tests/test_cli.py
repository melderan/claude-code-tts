"""Tests for cli.py — command handler integration tests."""

import json
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
