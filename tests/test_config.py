"""Tests for config.py — Python config loading and session management.

Tests session_set/session_del, migration, cleanup, and load_config.
"""

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import claude_code_tts.config as config_mod


@pytest.fixture
def tts_home(tmp_path):
    """Create a fake HOME with TTS directory structure."""
    tts_dir = tmp_path / ".claude-tts"
    tts_dir.mkdir()
    sessions_dir = tts_dir / "sessions.d"
    sessions_dir.mkdir()
    projects_dir = tmp_path / ".claude" / "projects"
    projects_dir.mkdir(parents=True)
    return tmp_path


@pytest.fixture
def patched_paths(tts_home):
    """Patch config module paths to use fake home."""
    with patch.object(config_mod, "HOME", tts_home), \
         patch.object(config_mod, "TTS_CONFIG_DIR", tts_home / ".claude-tts"), \
         patch.object(config_mod, "TTS_CONFIG_FILE", tts_home / ".claude-tts" / "config.json"), \
         patch.object(config_mod, "TTS_SESSIONS_DIR", tts_home / ".claude-tts" / "sessions.d"), \
         patch.object(config_mod, "PROJECTS_DIR", tts_home / ".claude" / "projects"):
        yield


class TestSessionSet:
    """Tests for session_set helper."""

    def test_creates_session_file(self, tts_home, patched_paths):
        config_mod.session_set("test-session", "muted", False)
        sf = tts_home / ".claude-tts" / "sessions.d" / "test-session.json"
        assert sf.exists()
        data = json.loads(sf.read_text())
        assert data["muted"] is False

    def test_set_string_value(self, tts_home, patched_paths):
        config_mod.session_set("test-session", "persona", "claude-connery")
        sf = tts_home / ".claude-tts" / "sessions.d" / "test-session.json"
        data = json.loads(sf.read_text())
        assert data["persona"] == "claude-connery"

    def test_set_number_value(self, tts_home, patched_paths):
        config_mod.session_set("test-session", "speed", 1.5)
        sf = tts_home / ".claude-tts" / "sessions.d" / "test-session.json"
        data = json.loads(sf.read_text())
        assert data["speed"] == 1.5

    def test_multiple_keys(self, tts_home, patched_paths):
        config_mod.session_set("test-session", "muted", False)
        config_mod.session_set("test-session", "persona", "claude-prime")
        config_mod.session_set("test-session", "speed", 2.0)
        sf = tts_home / ".claude-tts" / "sessions.d" / "test-session.json"
        data = json.loads(sf.read_text())
        assert data == {"muted": False, "persona": "claude-prime", "speed": 2.0}

    def test_creates_sessions_dir_if_missing(self, tts_home, patched_paths):
        sessions_dir = tts_home / ".claude-tts" / "sessions.d"
        sessions_dir.rmdir()
        assert not sessions_dir.exists()

        config_mod.session_set("test-session", "muted", True)
        assert sessions_dir.exists()
        sf = sessions_dir / "test-session.json"
        assert sf.exists()


class TestSessionDel:
    """Tests for session_del helper."""

    def test_delete_key(self, tts_home, patched_paths):
        sf = tts_home / ".claude-tts" / "sessions.d" / "test-session.json"
        sf.write_text(json.dumps({"muted": False, "speed": 1.5}))

        config_mod.session_del("test-session", "speed")
        data = json.loads(sf.read_text())
        assert "speed" not in data
        assert data["muted"] is False

    def test_delete_from_nonexistent_file(self, tts_home, patched_paths):
        """Should not error when session file doesn't exist."""
        config_mod.session_del("nonexistent", "speed")
        sf = tts_home / ".claude-tts" / "sessions.d" / "nonexistent.json"
        assert not sf.exists()


class TestMigration:
    """Tests for migrate_session (lazy migration from config.json)."""

    def test_migrates_legacy_session(self, tts_home, patched_paths):
        config = {
            "version": 1,
            "mode": "queue",
            "sessions": {
                "-Users-foo-project": {"muted": False, "persona": "claude-connery"}
            },
        }
        config_file = tts_home / ".claude-tts" / "config.json"
        config_file.write_text(json.dumps(config))

        result = config_mod.migrate_session("-Users-foo-project")
        assert result is True

        sf = tts_home / ".claude-tts" / "sessions.d" / "-Users-foo-project.json"
        assert sf.exists()
        data = json.loads(sf.read_text())
        assert data == {"muted": False, "persona": "claude-connery"}

        config_after = json.loads(config_file.read_text())
        assert "-Users-foo-project" not in config_after.get("sessions", {})

    def test_skips_if_already_migrated(self, tts_home, patched_paths):
        config = {
            "version": 1,
            "sessions": {"-Users-foo-project": {"muted": True}},
        }
        config_file = tts_home / ".claude-tts" / "config.json"
        config_file.write_text(json.dumps(config))

        sf = tts_home / ".claude-tts" / "sessions.d" / "-Users-foo-project.json"
        sf.write_text(json.dumps({"muted": False, "persona": "custom"}))

        result = config_mod.migrate_session("-Users-foo-project")
        assert result is False

        data = json.loads(sf.read_text())
        assert data == {"muted": False, "persona": "custom"}

    def test_no_legacy_data(self, tts_home, patched_paths):
        config = {"version": 1, "mode": "queue"}
        config_file = tts_home / ".claude-tts" / "config.json"
        config_file.write_text(json.dumps(config))

        result = config_mod.migrate_session("-Users-foo-project")
        assert result is False

        sf = tts_home / ".claude-tts" / "sessions.d" / "-Users-foo-project.json"
        assert not sf.exists()


class TestAutoCleanup:
    """Tests for maybe_cleanup (auto-cleanup of stale sessions)."""

    def test_removes_stale_session_files(self, tts_home, patched_paths):
        sf = tts_home / ".claude-tts" / "sessions.d" / "-Users-foo-dead.json"
        sf.write_text(json.dumps({"muted": False}))

        (tts_home / ".claude" / "projects" / "-Users-foo-alive").mkdir()
        sf_alive = tts_home / ".claude-tts" / "sessions.d" / "-Users-foo-alive.json"
        sf_alive.write_text(json.dumps({"muted": True}))

        config_mod.maybe_cleanup()

        assert not sf.exists(), "Stale session file should be removed"
        assert sf_alive.exists(), "Active session file should be preserved"

    def test_throttled_by_marker(self, tts_home, patched_paths):
        sf = tts_home / ".claude-tts" / "sessions.d" / "-Users-foo-stale.json"
        sf.write_text(json.dumps({"muted": False}))

        marker = tts_home / ".claude-tts" / "sessions.d" / ".last_cleanup"
        marker.write_text(str(int(time.time())))

        config_mod.maybe_cleanup()

        assert sf.exists(), "Cleanup should be throttled and not remove files"


class TestLoadConfig:
    """Tests for load_config merged config loading."""

    def test_defaults_without_config_file(self, tts_home, patched_paths):
        cfg = config_mod.load_config("test-session")
        assert cfg.mode == "direct"
        assert cfg.muted is False  # No config file means no default_muted check
        assert cfg.speed == 2.0
        assert cfg.active_persona == "claude-prime"

    def test_loads_persona_settings(self, tts_home, patched_paths):
        config = {
            "version": 1,
            "mode": "queue",
            "default_muted": False,
            "muted": False,
            "active_persona": "claude-chill",
            "personas": {
                "claude-chill": {
                    "voice": "en_US-joe-medium",
                    "speed": 1.5,
                    "speed_method": "length_scale",
                    "max_chars": 5000,
                },
            },
        }
        (tts_home / ".claude-tts" / "config.json").write_text(json.dumps(config))

        cfg = config_mod.load_config("test-session")
        assert cfg.mode == "queue"
        assert cfg.active_persona == "claude-chill"
        assert cfg.speed == 1.5
        assert cfg.speed_method == "length_scale"
        assert cfg.voice == "en_US-joe-medium"
        assert cfg.max_chars == 5000

    def test_session_overrides_persona(self, tts_home, patched_paths):
        config = {
            "version": 1,
            "default_muted": False,
            "muted": False,
            "active_persona": "claude-prime",
            "personas": {
                "claude-prime": {"voice": "en_US-hfc_male-medium", "speed": 2.0},
                "claude-chill": {"voice": "en_US-joe-medium", "speed": 1.5},
            },
        }
        (tts_home / ".claude-tts" / "config.json").write_text(json.dumps(config))

        sf = tts_home / ".claude-tts" / "sessions.d" / "test-session.json"
        sf.write_text(json.dumps({"persona": "claude-chill", "speed": 1.8}))

        cfg = config_mod.load_config("test-session")
        assert cfg.active_persona == "claude-chill"
        assert cfg.speed == 1.8  # Session speed overrides persona speed
        assert cfg.voice == "en_US-joe-medium"  # From claude-chill persona

    def test_mute_priority(self, tts_home, patched_paths):
        """Session mute > global mute > default_muted."""
        config = {
            "version": 1,
            "default_muted": True,
            "muted": True,
            "active_persona": "claude-prime",
            "personas": {"claude-prime": {"voice": "en_US-hfc_male-medium", "speed": 2.0}},
        }
        (tts_home / ".claude-tts" / "config.json").write_text(json.dumps(config))

        # Session says unmuted — should override global mute
        sf = tts_home / ".claude-tts" / "sessions.d" / "test-session.json"
        sf.write_text(json.dumps({"muted": False}))

        cfg = config_mod.load_config("test-session")
        assert cfg.muted is False

    def test_default_muted_new_session(self, tts_home, patched_paths):
        """New sessions (no session file) should respect default_muted."""
        config = {
            "version": 1,
            "default_muted": True,
            "muted": False,
            "active_persona": "claude-prime",
            "personas": {"claude-prime": {"voice": "en_US-hfc_male-medium", "speed": 2.0}},
        }
        (tts_home / ".claude-tts" / "config.json").write_text(json.dumps(config))

        cfg = config_mod.load_config("new-session")
        assert cfg.muted is True

    def test_env_override(self, tts_home, patched_paths):
        """Environment variables override everything."""
        config = {
            "version": 1,
            "default_muted": False,
            "muted": False,
            "active_persona": "claude-prime",
            "personas": {"claude-prime": {"voice": "en_US-hfc_male-medium", "speed": 2.0}},
        }
        (tts_home / ".claude-tts" / "config.json").write_text(json.dumps(config))

        import os
        with patch.dict(os.environ, {"CLAUDE_TTS_SPEED": "3.0"}):
            cfg = config_mod.load_config("test-session")
        assert cfg.speed == 3.0
