"""Tests for cli.py — command handler integration tests."""

import json
import os
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
