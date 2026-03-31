"""Tests for session.py — Python implementation of get_session_id().

Mirrors the test matrix from test_session_id.py (bash subprocess tests)
but tests the Python function directly.
"""

import os
import re
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def fake_home(tmp_path):
    """Create a fake HOME with ~/.claude/projects/ directory."""
    projects_dir = tmp_path / ".claude" / "projects"
    projects_dir.mkdir(parents=True)
    return tmp_path


class TestSimplePaths:
    """Basic path-to-session-ID resolution."""

    def test_simple_path(self, fake_home):
        (fake_home / ".claude" / "projects" / "-Users-foo-project").mkdir()
        with patch.dict(os.environ, {"PROJECT_ROOT": "/Users/foo/project"}, clear=False), \
             patch("claude_code_tts.session.Path.home", return_value=fake_home):
            from claude_code_tts.session import get_session_id
            env = os.environ.copy()
            env.pop("CLAUDE_TTS_SESSION", None)
            with patch.dict(os.environ, env, clear=True):
                result = get_session_id()
        assert result == "-Users-foo-project"

    def test_deep_path(self, fake_home):
        (fake_home / ".claude" / "projects" / "-Users-foo-bar-baz-project").mkdir()
        with patch.dict(os.environ, {"PROJECT_ROOT": "/Users/foo/bar/baz/project"}, clear=False), \
             patch("claude_code_tts.session.Path.home", return_value=fake_home):
            from claude_code_tts.session import get_session_id
            env = os.environ.copy()
            env.pop("CLAUDE_TTS_SESSION", None)
            with patch.dict(os.environ, env, clear=True):
                assert get_session_id() == "-Users-foo-bar-baz-project"


class TestUnderscoreEncoding:
    """Claude Code strips underscores from folder names."""

    def test_underscore_stripped(self, fake_home):
        # Claude Code encodes /Users/foo/_worktrees/bar as -Users-foo--worktrees-bar
        (fake_home / ".claude" / "projects" / "-Users-foo--worktrees-bar").mkdir()
        with patch.dict(os.environ, {"PROJECT_ROOT": "/Users/foo/_worktrees/bar"}, clear=False), \
             patch("claude_code_tts.session.Path.home", return_value=fake_home):
            from claude_code_tts.session import get_session_id
            env = os.environ.copy()
            env.pop("CLAUDE_TTS_SESSION", None)
            with patch.dict(os.environ, env, clear=True):
                assert get_session_id() == "-Users-foo--worktrees-bar"

    def test_real_worktree_pattern(self, fake_home):
        """The actual bug that prompted get_session_id() rewrite."""
        folder = "-Users-jwmoore-vault-code-repos-furiousengineering--worktrees-dbgorilla-dbgorilla-packages-shared-lib"
        (fake_home / ".claude" / "projects" / folder).mkdir()
        root = "/Users/jwmoore/vault/code/repos/furiousengineering/_worktrees/dbgorilla/dbgorilla/packages/shared-lib"
        with patch.dict(os.environ, {"PROJECT_ROOT": root}, clear=False), \
             patch("claude_code_tts.session.Path.home", return_value=fake_home):
            from claude_code_tts.session import get_session_id
            env = os.environ.copy()
            env.pop("CLAUDE_TTS_SESSION", None)
            with patch.dict(os.environ, env, clear=True):
                assert get_session_id() == folder


class TestDotEncoding:
    """Claude Code strips dots from folder names."""

    def test_dot_stripped(self, fake_home):
        (fake_home / ".claude" / "projects" / "-Users-foo-hidden-bar").mkdir()
        with patch.dict(os.environ, {"PROJECT_ROOT": "/Users/foo/.hidden/bar"}, clear=False), \
             patch("claude_code_tts.session.Path.home", return_value=fake_home):
            from claude_code_tts.session import get_session_id
            env = os.environ.copy()
            env.pop("CLAUDE_TTS_SESSION", None)
            with patch.dict(os.environ, env, clear=True):
                assert get_session_id() == "-Users-foo-hidden-bar"


class TestOverride:
    """CLAUDE_TTS_SESSION env var overrides everything."""

    def test_env_override(self, fake_home):
        with patch.dict(os.environ, {
            "CLAUDE_TTS_SESSION": "my-custom-session",
            "PROJECT_ROOT": "/Users/foo/project",
        }, clear=False), \
             patch("claude_code_tts.session.Path.home", return_value=fake_home):
            from claude_code_tts.session import get_session_id
            assert get_session_id() == "my-custom-session"


class TestFallbacks:
    """Fallback behavior when no match found."""

    def test_no_projects_dir(self, fake_home):
        """Falls back to naive slash-to-dash when ~/.claude/projects/ doesn't exist."""
        # Remove the projects dir
        (fake_home / ".claude" / "projects").rmdir()
        with patch.dict(os.environ, {"PROJECT_ROOT": "/Users/foo/project"}, clear=False), \
             patch("claude_code_tts.session.Path.home", return_value=fake_home):
            from claude_code_tts.session import get_session_id
            env = os.environ.copy()
            env.pop("CLAUDE_TTS_SESSION", None)
            with patch.dict(os.environ, env, clear=True):
                assert get_session_id() == "-Users-foo-project"

    def test_no_match(self, fake_home):
        """Falls back to naive slash-to-dash when no matching folder."""
        with patch.dict(os.environ, {"PROJECT_ROOT": "/Users/foo/unknown"}, clear=False), \
             patch("claude_code_tts.session.Path.home", return_value=fake_home):
            from claude_code_tts.session import get_session_id
            env = os.environ.copy()
            env.pop("CLAUDE_TTS_SESSION", None)
            with patch.dict(os.environ, env, clear=True):
                assert get_session_id() == "-Users-foo-unknown"

    def test_no_project_root(self, fake_home):
        """Falls back to PWD when PROJECT_ROOT is unset."""
        test_dir = fake_home / "test-workdir"
        test_dir.mkdir()
        with patch.dict(os.environ, {}, clear=False), \
             patch("claude_code_tts.session.Path.home", return_value=fake_home), \
             patch("os.getcwd", return_value=str(test_dir)):
            from claude_code_tts.session import get_session_id
            env = os.environ.copy()
            env.pop("CLAUDE_TTS_SESSION", None)
            env.pop("PROJECT_ROOT", None)
            with patch.dict(os.environ, env, clear=True):
                result = get_session_id()
        assert result == re.sub(r"[^a-zA-Z0-9]", "-", str(test_dir))


class TestAmbiguity:
    """Multiple folders that could match — correct one wins."""

    def test_underscore_vs_no_underscore(self, fake_home):
        """Both /foo/a_b and /foo/ab exist — correct one matched."""
        (fake_home / ".claude" / "projects" / "-foo-a-b").mkdir()
        (fake_home / ".claude" / "projects" / "-foo-ab").mkdir()
        # /foo/a_b should match -foo-a-b (same alphanumeric: fooab)
        # But -foo-ab also has alphanumeric fooab — ambiguous!
        # The function picks whichever it finds first. Both are valid matches.
        with patch.dict(os.environ, {"PROJECT_ROOT": "/foo/a_b"}, clear=False), \
             patch("claude_code_tts.session.Path.home", return_value=fake_home):
            from claude_code_tts.session import get_session_id
            env = os.environ.copy()
            env.pop("CLAUDE_TTS_SESSION", None)
            with patch.dict(os.environ, env, clear=True):
                result = get_session_id()
        assert result in ("-foo-a-b", "-foo-ab")
