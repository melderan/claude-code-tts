"""Tests for session ID resolution (get_session_id in tts-lib.sh).

Verifies that get_session_id() correctly resolves PROJECT_ROOT to the actual
Claude Code project folder name, handling Claude Code's path encoding which
strips underscores, dots, and other non-alphanumeric characters.
"""

import os
import subprocess
from pathlib import Path

import pytest

# Path to tts-lib.sh relative to repo root
REPO_DIR = Path(__file__).parent.parent
TTS_LIB = REPO_DIR / "scripts" / "tts-lib.sh"

# Wrapper script that sources tts-lib.sh and calls get_session_id
WRAPPER_SCRIPT = """\
#!/bin/bash
set -euo pipefail
source "{tts_lib}"
get_session_id
"""


def run_get_session_id(
    *,
    project_root: str | None = None,
    pwd: str | None = None,
    claude_tts_session: str | None = None,
    home: str | None = None,
) -> str:
    """Run get_session_id() via subprocess with controlled environment.

    Args:
        project_root: Value for PROJECT_ROOT env var (None to unset)
        pwd: Working directory (None for /tmp)
        claude_tts_session: Value for CLAUDE_TTS_SESSION override (None to unset)
        home: Value for HOME (controls where ~/.claude/projects/ is looked up)

    Returns:
        The session ID printed to stdout (stripped of whitespace)
    """
    env = os.environ.copy()

    # Clear vars that might leak from the test runner's environment
    env.pop("CLAUDE_TTS_SESSION", None)
    env.pop("PROJECT_ROOT", None)

    if project_root is not None:
        env["PROJECT_ROOT"] = project_root
    if claude_tts_session is not None:
        env["CLAUDE_TTS_SESSION"] = claude_tts_session
    if home is not None:
        env["HOME"] = home

    wrapper = WRAPPER_SCRIPT.format(tts_lib=TTS_LIB)
    result = subprocess.run(
        ["bash", "-c", wrapper],
        capture_output=True,
        text=True,
        env=env,
        cwd=pwd or "/tmp",
        timeout=5,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"get_session_id failed (exit {result.returncode}): {result.stderr}"
        )
    return result.stdout.strip()


@pytest.fixture
def fake_home(tmp_path):
    """Create a fake HOME with ~/.claude/projects/ directory."""
    projects_dir = tmp_path / ".claude" / "projects"
    projects_dir.mkdir(parents=True)
    return tmp_path, projects_dir


class TestSimplePaths:
    """Paths with no special characters — basic slash-to-dash encoding."""

    def test_simple_project_path(self, fake_home):
        home, projects = fake_home
        folder = "-Users-foo-project"
        (projects / folder).mkdir()

        result = run_get_session_id(
            project_root="/Users/foo/project",
            home=str(home),
        )
        assert result == folder

    def test_deep_nested_path(self, fake_home):
        home, projects = fake_home
        folder = "-Users-foo-code-repos-org-myrepo"
        (projects / folder).mkdir()

        result = run_get_session_id(
            project_root="/Users/foo/code/repos/org/myrepo",
            home=str(home),
        )
        assert result == folder

    def test_path_with_hyphens(self, fake_home):
        """Hyphens in directory names are preserved."""
        home, projects = fake_home
        folder = "-Users-foo-my-cool-project"
        (projects / folder).mkdir()

        result = run_get_session_id(
            project_root="/Users/foo/my-cool-project",
            home=str(home),
        )
        assert result == folder


class TestUnderscoreEncoding:
    """Claude Code strips underscores from project folder names."""

    def test_underscore_prefix(self, fake_home):
        """_worktrees → --worktrees (underscore stripped, slash becomes dash)."""
        home, projects = fake_home
        folder = "-Users-foo--worktrees-bar"
        (projects / folder).mkdir()

        result = run_get_session_id(
            project_root="/Users/foo/_worktrees/bar",
            home=str(home),
        )
        assert result == folder

    def test_underscore_in_middle(self, fake_home):
        """my_project → my-project (underscore stripped)."""
        home, projects = fake_home
        folder = "-Users-foo-myproject"
        (projects / folder).mkdir()

        result = run_get_session_id(
            project_root="/Users/foo/my_project",
            home=str(home),
        )
        assert result == folder

    def test_multiple_underscores(self, fake_home):
        home, projects = fake_home
        folder = "-Users-foo-abc-def"
        (projects / folder).mkdir()

        result = run_get_session_id(
            project_root="/Users/foo/_a_b_c/d_e_f",
            home=str(home),
        )
        assert result == folder

    def test_real_worktree_pattern(self, fake_home):
        """Matches the actual pattern that triggered this bug."""
        home, projects = fake_home
        folder = "-Users-jwmoore-vault-code-repos-furiousengineering--worktrees-dbgorilla-dbgorilla-packages-shared-lib"
        (projects / folder).mkdir()

        result = run_get_session_id(
            project_root="/Users/jwmoore/vault/code/repos/furiousengineering/_worktrees/dbgorilla/dbgorilla/packages/shared-lib",
            home=str(home),
        )
        assert result == folder


class TestDotEncoding:
    """Claude Code strips dots from project folder names."""

    def test_hidden_directory(self, fake_home):
        """.hidden → -hidden (dot stripped)."""
        home, projects = fake_home
        folder = "-Users-foo-hidden-bar"
        (projects / folder).mkdir()

        result = run_get_session_id(
            project_root="/Users/foo/.hidden/bar",
            home=str(home),
        )
        assert result == folder

    def test_dot_in_directory_name(self, fake_home):
        home, projects = fake_home
        folder = "-Users-foo-myv2project"
        (projects / folder).mkdir()

        result = run_get_session_id(
            project_root="/Users/foo/my.v2.project",
            home=str(home),
        )
        assert result == folder


class TestMultipleSpecialChars:
    """Paths with multiple types of special characters."""

    def test_underscore_and_dot(self, fake_home):
        home, projects = fake_home
        folder = "-Users-foo-abc-d"
        (projects / folder).mkdir()

        result = run_get_session_id(
            project_root="/Users/foo/_a.b_c/d",
            home=str(home),
        )
        assert result == folder

    def test_dot_worktrees_pattern(self, fake_home):
        """In case someone uses .worktrees instead of _worktrees."""
        home, projects = fake_home
        folder = "-Users-foo--worktrees-bar"
        (projects / folder).mkdir()

        result = run_get_session_id(
            project_root="/Users/foo/.worktrees/bar",
            home=str(home),
        )
        assert result == folder


class TestOverride:
    """CLAUDE_TTS_SESSION env var takes precedence over everything."""

    def test_override_ignores_project_root(self, fake_home):
        home, projects = fake_home
        (projects / "-Users-foo-project").mkdir()

        result = run_get_session_id(
            project_root="/Users/foo/project",
            claude_tts_session="my-custom-session",
            home=str(home),
        )
        assert result == "my-custom-session"

    def test_override_ignores_missing_project_root(self, fake_home):
        home, _ = fake_home

        result = run_get_session_id(
            claude_tts_session="override-value",
            home=str(home),
        )
        assert result == "override-value"


class TestFallbacks:
    """Fallback behavior when lookup can't find a match."""

    def test_no_projects_dir(self, tmp_path):
        """When ~/.claude/projects/ doesn't exist, falls back to tr '/' '-'."""
        result = run_get_session_id(
            project_root="/Users/foo/project",
            home=str(tmp_path),  # No .claude/projects/ created
        )
        assert result == "-Users-foo-project"

    def test_no_project_root(self, fake_home):
        """When PROJECT_ROOT is unset, uses PWD."""
        home, _ = fake_home
        # Use a known path that won't get symlink-resolved (unlike /var on macOS)
        pwd = str(home / "workdir")
        Path(pwd).mkdir()
        result = run_get_session_id(
            project_root=None,
            pwd=pwd,
            home=str(home),
        )
        expected = pwd.replace("/", "-")
        assert result == expected

    def test_no_matching_folder(self, fake_home):
        """When no folder matches, falls back to tr '/' '-'."""
        home, projects = fake_home
        # Create a folder that doesn't match
        (projects / "-Users-bar-other").mkdir()

        result = run_get_session_id(
            project_root="/Users/foo/new-project",
            home=str(home),
        )
        assert result == "-Users-foo-new-project"

    def test_empty_projects_dir(self, fake_home):
        """Empty projects directory falls back to tr '/' '-'."""
        home, _ = fake_home

        result = run_get_session_id(
            project_root="/Users/foo/project",
            home=str(home),
        )
        assert result == "-Users-foo-project"


class TestAmbiguity:
    """Ensure we don't match the wrong folder when paths are similar."""

    def test_underscore_vs_no_underscore(self, fake_home):
        """Paths that differ only by underscore could collide alphanumerically.

        /Users/foo/a_b → alphanumeric 'Usersfooab'
        /Users/foo/ab  → alphanumeric 'Usersfooab'

        Both produce the same alphanumeric fingerprint. If both folders exist,
        we should still match one (the first match is fine since Claude Code
        would only create one folder per actual path).
        """
        home, projects = fake_home
        # Both /Users/foo/a_b and /Users/foo/ab would produce the same
        # folder name: -Users-foo-ab (underscore stripped). Claude Code
        # creates one folder per actual path, so collision is theoretical.
        (projects / "-Users-foo-ab").mkdir()

        result = run_get_session_id(
            project_root="/Users/foo/a_b",
            home=str(home),
        )
        assert result == "-Users-foo-ab"

    def test_similar_but_different_paths(self, fake_home):
        """Paths that produce genuinely different alphanumeric content."""
        home, projects = fake_home
        (projects / "-Users-foo-abc").mkdir()
        (projects / "-Users-foo-xyz").mkdir()

        result = run_get_session_id(
            project_root="/Users/foo/abc",
            home=str(home),
        )
        assert result == "-Users-foo-abc"

    def test_substring_path_not_confused(self, fake_home):
        """A shorter path shouldn't match a longer folder."""
        home, projects = fake_home
        (projects / "-Users-foo-project-extra").mkdir()
        # Don't create -Users-foo-project

        result = run_get_session_id(
            project_root="/Users/foo/project",
            home=str(home),
        )
        # Should NOT match the longer folder — fallback to tr
        assert result == "-Users-foo-project"
