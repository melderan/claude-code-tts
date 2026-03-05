"""Tests for sessions.d conf.d-style session config.

Tests the session helper functions (tts_session_set, tts_session_del),
migration from legacy config.json .sessions, auto-cleanup, and the
installer's batch migration function.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_DIR = Path(__file__).parent.parent
TTS_LIB = REPO_DIR / "scripts" / "tts-lib.sh"


def run_bash(script: str, *, home: str, env_extra: dict | None = None) -> str:
    """Run a bash script with controlled HOME."""
    env = os.environ.copy()
    env["HOME"] = home
    env.pop("CLAUDE_TTS_SESSION", None)
    env.pop("PROJECT_ROOT", None)
    if env_extra:
        env.update(env_extra)

    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(f"bash failed (exit {result.returncode}): {result.stderr}")
    return result.stdout.strip()


@pytest.fixture
def tts_home(tmp_path):
    """Create a fake HOME with .claude-tts/ and .claude/projects/ dirs."""
    tts_dir = tmp_path / ".claude-tts"
    tts_dir.mkdir()
    sessions_dir = tts_dir / "sessions.d"
    sessions_dir.mkdir()
    projects_dir = tmp_path / ".claude" / "projects"
    projects_dir.mkdir(parents=True)
    return tmp_path


class TestSessionSet:
    """Tests for tts_session_set helper."""

    def test_creates_session_file(self, tts_home):
        run_bash(
            f'source "{TTS_LIB}"; tts_session_set "test-session" "muted" "false" "bool"',
            home=str(tts_home),
        )
        sf = tts_home / ".claude-tts" / "sessions.d" / "test-session.json"
        assert sf.exists()
        data = json.loads(sf.read_text())
        assert data["muted"] is False

    def test_set_string_value(self, tts_home):
        run_bash(
            f'source "{TTS_LIB}"; tts_session_set "test-session" "persona" "claude-connery"',
            home=str(tts_home),
        )
        sf = tts_home / ".claude-tts" / "sessions.d" / "test-session.json"
        data = json.loads(sf.read_text())
        assert data["persona"] == "claude-connery"

    def test_set_number_value(self, tts_home):
        run_bash(
            f'source "{TTS_LIB}"; tts_session_set "test-session" "speed" "1.5" "number"',
            home=str(tts_home),
        )
        sf = tts_home / ".claude-tts" / "sessions.d" / "test-session.json"
        data = json.loads(sf.read_text())
        assert data["speed"] == 1.5

    def test_multiple_keys(self, tts_home):
        run_bash(
            f"""source "{TTS_LIB}"
            tts_session_set "test-session" "muted" "false" "bool"
            tts_session_set "test-session" "persona" "claude-prime"
            tts_session_set "test-session" "speed" "2.0" "number"
            """,
            home=str(tts_home),
        )
        sf = tts_home / ".claude-tts" / "sessions.d" / "test-session.json"
        data = json.loads(sf.read_text())
        assert data == {"muted": False, "persona": "claude-prime", "speed": 2.0}

    def test_creates_sessions_dir_if_missing(self, tts_home):
        # Remove the sessions.d dir
        sessions_dir = tts_home / ".claude-tts" / "sessions.d"
        sessions_dir.rmdir()
        assert not sessions_dir.exists()

        run_bash(
            f'source "{TTS_LIB}"; tts_session_set "test-session" "muted" "true" "bool"',
            home=str(tts_home),
        )
        assert sessions_dir.exists()
        sf = sessions_dir / "test-session.json"
        assert sf.exists()


class TestSessionDel:
    """Tests for tts_session_del helper."""

    def test_delete_key(self, tts_home):
        sf = tts_home / ".claude-tts" / "sessions.d" / "test-session.json"
        sf.write_text(json.dumps({"muted": False, "speed": 1.5}))

        run_bash(
            f'source "{TTS_LIB}"; tts_session_del "test-session" "speed"',
            home=str(tts_home),
        )
        data = json.loads(sf.read_text())
        assert "speed" not in data
        assert data["muted"] is False

    def test_delete_from_nonexistent_file(self, tts_home):
        """Should not error when session file doesn't exist."""
        run_bash(
            f'source "{TTS_LIB}"; tts_session_del "nonexistent" "speed"',
            home=str(tts_home),
        )
        # Should not create the file
        sf = tts_home / ".claude-tts" / "sessions.d" / "nonexistent.json"
        assert not sf.exists()


class TestMigration:
    """Tests for _tts_migrate_session (lazy migration from config.json)."""

    def test_migrates_legacy_session(self, tts_home):
        config = {
            "version": 1,
            "mode": "queue",
            "sessions": {
                "-Users-foo-project": {"muted": False, "persona": "claude-connery"}
            },
        }
        config_file = tts_home / ".claude-tts" / "config.json"
        config_file.write_text(json.dumps(config))

        run_bash(
            f"""source "{TTS_LIB}"
            TTS_SESSION="-Users-foo-project"
            _tts_migrate_session "$TTS_SESSION"
            """,
            home=str(tts_home),
        )

        # Session file should exist
        sf = tts_home / ".claude-tts" / "sessions.d" / "-Users-foo-project.json"
        assert sf.exists()
        data = json.loads(sf.read_text())
        assert data == {"muted": False, "persona": "claude-connery"}

        # Config.json should no longer have this session
        config_after = json.loads(config_file.read_text())
        assert "-Users-foo-project" not in config_after.get("sessions", {})

    def test_skips_if_already_migrated(self, tts_home):
        """If session file already exists, don't overwrite it."""
        config = {
            "version": 1,
            "sessions": {
                "-Users-foo-project": {"muted": True}
            },
        }
        config_file = tts_home / ".claude-tts" / "config.json"
        config_file.write_text(json.dumps(config))

        # Pre-create session file with different data
        sf = tts_home / ".claude-tts" / "sessions.d" / "-Users-foo-project.json"
        sf.write_text(json.dumps({"muted": False, "persona": "custom"}))

        run_bash(
            f"""source "{TTS_LIB}"
            TTS_SESSION="-Users-foo-project"
            _tts_migrate_session "$TTS_SESSION" || true
            """,
            home=str(tts_home),
        )

        # Session file should NOT be overwritten
        data = json.loads(sf.read_text())
        assert data == {"muted": False, "persona": "custom"}

    def test_no_legacy_data(self, tts_home):
        """No crash when config.json has no .sessions."""
        config = {"version": 1, "mode": "queue"}
        config_file = tts_home / ".claude-tts" / "config.json"
        config_file.write_text(json.dumps(config))

        run_bash(
            f"""source "{TTS_LIB}"
            TTS_SESSION="-Users-foo-project"
            _tts_migrate_session "$TTS_SESSION" || true
            """,
            home=str(tts_home),
        )

        sf = tts_home / ".claude-tts" / "sessions.d" / "-Users-foo-project.json"
        assert not sf.exists()


class TestAutoCleanup:
    """Tests for _tts_maybe_cleanup (auto-cleanup of stale sessions)."""

    def test_removes_stale_session_files(self, tts_home):
        # Create a session file for a project that doesn't exist
        sf = tts_home / ".claude-tts" / "sessions.d" / "-Users-foo-dead.json"
        sf.write_text(json.dumps({"muted": False}))

        # Create a session file for a project that DOES exist
        (tts_home / ".claude" / "projects" / "-Users-foo-alive").mkdir()
        sf_alive = tts_home / ".claude-tts" / "sessions.d" / "-Users-foo-alive.json"
        sf_alive.write_text(json.dumps({"muted": True}))

        run_bash(
            f"""source "{TTS_LIB}"
            _tts_maybe_cleanup
            """,
            home=str(tts_home),
        )

        assert not sf.exists(), "Stale session file should be removed"
        assert sf_alive.exists(), "Active session file should be preserved"

    def test_throttled_by_marker(self, tts_home):
        """Cleanup should be throttled — won't run twice in quick succession."""
        # Create a stale session
        sf = tts_home / ".claude-tts" / "sessions.d" / "-Users-foo-stale.json"
        sf.write_text(json.dumps({"muted": False}))

        # Set the marker to "just ran"
        marker = tts_home / ".claude-tts" / "sessions.d" / ".last_cleanup"
        import time
        marker.write_text(str(int(time.time())))

        run_bash(
            f"""source "{TTS_LIB}"
            _tts_maybe_cleanup
            """,
            home=str(tts_home),
        )

        # Stale file should still exist (throttled)
        assert sf.exists(), "Cleanup should be throttled and not remove files"

    def test_logs_restorable_content(self, tts_home):
        """Debug log should contain the full JSON for restoration."""
        sf = tts_home / ".claude-tts" / "sessions.d" / "-Users-foo-dead.json"
        sf.write_text(json.dumps({"muted": False, "persona": "claude-prime"}))

        output = run_bash(
            f"""source "{TTS_LIB}"
            _tts_maybe_cleanup
            cat /tmp/claude_tts_debug.log | grep "Auto-cleanup.*-Users-foo-dead" | tail -1
            """,
            home=str(tts_home),
        )

        assert "restore:" in output
        assert '"muted":false' in output.replace(" ", "") or '"muted": false' in output


class TestInstallerMigration:
    """Tests for the installer's _migrate_sessions_to_confd function."""

    def test_batch_migration(self, tts_home):
        from claude_code_tts.install import _migrate_sessions_to_confd
        import claude_code_tts.install as install_mod

        config = {
            "version": 1,
            "mode": "queue",
            "sessions": {
                "-Users-foo-project1": {"muted": False},
                "-Users-foo-project2": {"muted": True, "persona": "claude-connery"},
            },
        }
        config_file = tts_home / ".claude-tts" / "config.json"
        config_file.write_text(json.dumps(config))

        # Temporarily patch the paths
        orig_config_file = install_mod.TTS_CONFIG_FILE
        orig_sessions_dir = install_mod.TTS_SESSIONS_DIR
        try:
            install_mod.TTS_CONFIG_FILE = config_file
            install_mod.TTS_SESSIONS_DIR = tts_home / ".claude-tts" / "sessions.d"
            _migrate_sessions_to_confd()
        finally:
            install_mod.TTS_CONFIG_FILE = orig_config_file
            install_mod.TTS_SESSIONS_DIR = orig_sessions_dir

        # Verify session files created
        sf1 = tts_home / ".claude-tts" / "sessions.d" / "-Users-foo-project1.json"
        sf2 = tts_home / ".claude-tts" / "sessions.d" / "-Users-foo-project2.json"
        assert sf1.exists()
        assert sf2.exists()
        assert json.loads(sf1.read_text()) == {"muted": False}
        assert json.loads(sf2.read_text()) == {"muted": True, "persona": "claude-connery"}

        # Verify .sessions removed from config.json
        config_after = json.loads(config_file.read_text())
        assert "sessions" not in config_after
