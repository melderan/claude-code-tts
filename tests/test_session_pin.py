"""Tests for the session pinning mechanism.

The pin mechanism is the canonical bridge between hook-side session
detection (which uses transcript_path) and CLI-side resolution (which has
no transcript). The hook writes a file keyed by the parent `claude` PID;
the CLI walks its process tree to find the same PID and reads the file.
"""

import os
from unittest.mock import patch

import pytest


@pytest.fixture
def fake_active_dir(tmp_path):
    """Redirect ACTIVE_DIR to a temp directory."""
    active = tmp_path / "active"
    with patch("claude_code_tts.session.ACTIVE_DIR", active):
        yield active


class TestFindClaudeAncestor:
    """Process-tree walking finds the nearest `claude` ancestor."""

    def test_direct_parent_is_claude(self):
        # ppid 1000 → claude
        with patch("claude_code_tts.session._ps_query") as mock_ps, \
             patch("os.getppid", return_value=1000):
            mock_ps.return_value = (500, "claude")
            from claude_code_tts.session import find_claude_ancestor_pid
            assert find_claude_ancestor_pid() == 1000

    def test_grandparent_is_claude(self):
        # ppid 1000 (bash) → 2000 (claude)
        responses = {
            1000: (2000, "bash"),
            2000: (3000, "claude"),
        }
        with patch("claude_code_tts.session._ps_query", side_effect=lambda p: responses.get(p)), \
             patch("os.getppid", return_value=1000):
            from claude_code_tts.session import find_claude_ancestor_pid
            assert find_claude_ancestor_pid() == 2000

    def test_no_claude_ancestor(self):
        responses = {
            1000: (2000, "bash"),
            2000: (3000, "tmux"),
            3000: (1, "login"),
        }
        with patch("claude_code_tts.session._ps_query", side_effect=lambda p: responses.get(p)), \
             patch("os.getppid", return_value=1000):
            from claude_code_tts.session import find_claude_ancestor_pid
            assert find_claude_ancestor_pid() is None

    def test_terminates_at_init(self):
        with patch("claude_code_tts.session._ps_query", return_value=(1, "init")), \
             patch("os.getppid", return_value=1000):
            from claude_code_tts.session import find_claude_ancestor_pid
            # First hop returns (1, init), then pid becomes 1, loop exits
            assert find_claude_ancestor_pid() is None

    def test_terminates_when_process_gone(self):
        with patch("claude_code_tts.session._ps_query", return_value=None), \
             patch("os.getppid", return_value=99999):
            from claude_code_tts.session import find_claude_ancestor_pid
            assert find_claude_ancestor_pid() is None

    def test_walk_bounded(self):
        # Pathological cycle — claim every process has parent 999
        with patch("claude_code_tts.session._ps_query", return_value=(999, "fake")), \
             patch("os.getppid", return_value=1000):
            from claude_code_tts.session import find_claude_ancestor_pid
            # Should give up after 20 hops, never finding claude
            assert find_claude_ancestor_pid() is None

    def test_full_path_comm_matches(self):
        """ps -o comm= may return /path/to/claude on some systems."""
        with patch("claude_code_tts.session._ps_query",
                   return_value=(500, "/opt/homebrew/bin/claude")), \
             patch("os.getppid", return_value=1000):
            from claude_code_tts.session import find_claude_ancestor_pid
            assert find_claude_ancestor_pid() == 1000


class TestPinAndRead:
    """pin_session writes, read_pinned_session reads."""

    def test_pin_then_read_roundtrip(self, fake_active_dir):
        with patch("claude_code_tts.session.find_claude_ancestor_pid", return_value=12345):
            from claude_code_tts.session import pin_session, read_pinned_session
            pin_session("-Users-foo-bar-project")
            assert (fake_active_dir / "12345.session").is_file()
            assert read_pinned_session() == "-Users-foo-bar-project"

    def test_pin_overwrites(self, fake_active_dir):
        with patch("claude_code_tts.session.find_claude_ancestor_pid", return_value=12345):
            from claude_code_tts.session import pin_session, read_pinned_session
            pin_session("first")
            pin_session("second")
            assert read_pinned_session() == "second"

    def test_pin_no_claude_ancestor_is_noop(self, fake_active_dir):
        with patch("claude_code_tts.session.find_claude_ancestor_pid", return_value=None):
            from claude_code_tts.session import pin_session
            pin_session("anything")
            # Directory may not even exist
            assert not fake_active_dir.exists() or not any(fake_active_dir.iterdir())

    def test_read_no_claude_ancestor_returns_none(self, fake_active_dir):
        with patch("claude_code_tts.session.find_claude_ancestor_pid", return_value=None):
            from claude_code_tts.session import read_pinned_session
            assert read_pinned_session() is None

    def test_read_no_pin_file_returns_none(self, fake_active_dir):
        with patch("claude_code_tts.session.find_claude_ancestor_pid", return_value=12345):
            from claude_code_tts.session import read_pinned_session
            assert read_pinned_session() is None

    def test_atomic_write_uses_tmpfile(self, fake_active_dir):
        """Pin writes to .tmp then renames — no partial reads possible."""
        with patch("claude_code_tts.session.find_claude_ancestor_pid", return_value=12345):
            from claude_code_tts.session import pin_session
            pin_session("session-id")
            # No leftover .tmp file after a successful write
            tmp_files = list(fake_active_dir.glob("*.tmp"))
            assert tmp_files == []


class TestGetSessionIdPriority:
    """Pinned session takes priority over PROJECT_ROOT, override beats both."""

    def test_pinned_beats_project_root(self, fake_active_dir):
        with patch("claude_code_tts.session.find_claude_ancestor_pid", return_value=12345):
            from claude_code_tts.session import pin_session, get_session_id
            pin_session("-canonical-from-hook")

            env = {"PROJECT_ROOT": "/Users/foo/different"}
            old_session = os.environ.pop("CLAUDE_TTS_SESSION", None)
            try:
                with patch.dict(os.environ, env, clear=False):
                    os.environ.pop("CLAUDE_TTS_SESSION", None)
                    assert get_session_id() == "-canonical-from-hook"
            finally:
                if old_session is not None:
                    os.environ["CLAUDE_TTS_SESSION"] = old_session

    def test_override_beats_pinned(self, fake_active_dir):
        with patch("claude_code_tts.session.find_claude_ancestor_pid", return_value=12345):
            from claude_code_tts.session import pin_session, get_session_id
            pin_session("-canonical-from-hook")
            with patch.dict(os.environ, {"CLAUDE_TTS_SESSION": "override-wins"}, clear=False):
                assert get_session_id() == "override-wins"

    def test_no_pin_falls_through_to_project_root(self, fake_active_dir, tmp_path):
        """When nothing is pinned, the existing PROJECT_ROOT path still works."""
        projects_dir = tmp_path / ".claude" / "projects"
        projects_dir.mkdir(parents=True)
        (projects_dir / "-Users-foo-project").mkdir()

        with patch("claude_code_tts.session.find_claude_ancestor_pid", return_value=None), \
             patch("claude_code_tts.session.Path.home", return_value=tmp_path), \
             patch.dict(os.environ, {"PROJECT_ROOT": "/Users/foo/project"}, clear=False):
            from claude_code_tts.session import get_session_id
            os.environ.pop("CLAUDE_TTS_SESSION", None)
            assert get_session_id() == "-Users-foo-project"


class TestCleanup:
    """cleanup_stale_pins removes files for dead PIDs."""

    def test_removes_dead_pid_files(self, fake_active_dir):
        fake_active_dir.mkdir(parents=True)
        (fake_active_dir / "11111.session").write_text("alive")
        (fake_active_dir / "22222.session").write_text("dead")

        def fake_ps(pid):
            return (1, "claude") if pid == 11111 else None

        with patch("claude_code_tts.session._ps_query", side_effect=fake_ps):
            from claude_code_tts.session import cleanup_stale_pins
            removed = cleanup_stale_pins()
            assert removed == 1
            assert (fake_active_dir / "11111.session").is_file()
            assert not (fake_active_dir / "22222.session").is_file()

    def test_ignores_non_pid_filenames(self, fake_active_dir):
        fake_active_dir.mkdir(parents=True)
        (fake_active_dir / "garbage.session").write_text("x")
        with patch("claude_code_tts.session._ps_query", return_value=None):
            from claude_code_tts.session import cleanup_stale_pins
            removed = cleanup_stale_pins()
            assert removed == 0
            # Non-numeric filename is left alone, not an error
            assert (fake_active_dir / "garbage.session").is_file()

    def test_no_active_dir_is_safe(self, fake_active_dir):
        from claude_code_tts.session import cleanup_stale_pins
        # fake_active_dir doesn't exist yet
        assert cleanup_stale_pins() == 0
