"""The release command: preflight, notes from the commit, and the plan it prints."""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

import claude_code_tts.release as rel
from claude_code_tts.release import (
    build_notes,
    head_version,
    latest_tag_version,
    notes_from_commit,
    preflight,
    repo_slug,
)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def set_version(repo: Path, version: str) -> None:
    (repo / "src" / "claude_code_tts" / "__init__.py").write_text(
        f'"""pkg"""\n\n__version__ = "{version}"\n'
    )


@pytest.fixture
def repo(tmp_path):
    """A git repo shaped like this one: main, a version file, one released tag."""
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.name", "Test")
    git(tmp_path, "config", "user.email", "t@example.invalid")
    git(tmp_path, "config", "commit.gpgsign", "false")
    git(tmp_path, "config", "tag.gpgsign", "false")
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    (tmp_path / "src" / "claude_code_tts").mkdir(parents=True)
    set_version(tmp_path, "1.2.0")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "feat: first")
    git(tmp_path, "tag", "-a", "v1.2.0", "-m", "v1.2.0 - first")
    return tmp_path


class TestNotes:
    def test_commit_prefix_and_trailers_dropped(self):
        summary, body = notes_from_commit(
            "fix: a pause holds the queue",
            "Paused time is not age.\n\nSecond paragraph.\n\nCo-Authored-By: Someone <x@y.z>\n",
        )
        assert summary == "a pause holds the queue"
        assert body == "Paused time is not age.\n\nSecond paragraph."

    def test_scope_and_breaking_marker_dropped(self):
        assert notes_from_commit("feat(daemon)!: stream", "")[0] == "stream"

    def test_build_from_commit(self):
        subject, body = build_notes("9.15.1", None, "fix: hold", "why\n\nCo-Authored-By: a <b@c>")
        assert subject == "v9.15.1 - hold"
        assert body == "why"

    def test_build_from_notes_argument(self):
        subject, body = build_notes("9.16.0", "the summary\nline one\nline two", "fix: x", "y")
        assert subject == "v9.16.0 - the summary"
        assert body == "line one\nline two"

    def test_prose_with_a_leading_label_is_kept(self):
        body_in = "Paused time is not age.\n\nWhy: this matters to the listener.\nFound: today."
        _, body = notes_from_commit("fix: x", body_in)
        assert body == body_in

    def test_only_a_final_hyphenated_trailer_block_is_dropped(self):
        body_in = "Why: prose.\n\nCo-Authored-By: a <b@c>\nSigned-off-by: d <e@f>"
        _, body = notes_from_commit("fix: x", body_in)
        assert body == "Why: prose."


class TestPreflight:
    def test_released_head_is_refused(self, repo):
        plan = preflight(repo)
        assert head_version(repo) == "1.2.0"
        assert latest_tag_version(repo) == "1.2.0"
        assert any("already exists" in p for p in plan.problems)

    def test_clean_bumped_head_with_key_has_no_problems(self, repo):
        set_version(repo, "1.3.0")
        git(repo, "commit", "-q", "-am", "feat: streaming\n\nLong story.\n\nCo-Authored-By: a <b@c>")
        git(repo, "config", "user.signingkey", "DEADBEEF")
        plan = preflight(repo)
        assert plan.problems == []
        assert plan.tag == "v1.3.0"
        assert plan.subject == "v1.3.0 - streaming"
        assert plan.body == "Long story."
        assert plan.branch == "main"

    def test_missing_signing_key_is_a_problem(self, repo):
        set_version(repo, "1.3.0")
        git(repo, "commit", "-q", "-am", "feat: x")
        plan = preflight(repo)
        assert any("signingkey" in p for p in plan.problems)

    def test_dirty_tree_and_branch_are_problems(self, repo):
        git(repo, "checkout", "-q", "-b", "topic")
        set_version(repo, "1.3.0")
        plan = preflight(repo)
        assert any("not clean" in p for p in plan.problems)
        assert any("branch 'topic'" in p for p in plan.problems)

    def test_notes_argument_overrides_commit(self, repo):
        set_version(repo, "1.3.0")
        git(repo, "commit", "-q", "-am", "feat: x")
        git(repo, "config", "user.signingkey", "DEADBEEF")
        plan = preflight(repo, notes="hand written\nbody here")
        assert plan.subject == "v1.3.0 - hand written"
        assert plan.body == "body here"


class TestRepoSlug:
    @pytest.mark.parametrize(
        "url",
        ["https://github.com/melderan/claude-code-tts.git", "git@github.com:melderan/claude-code-tts.git",
         "https://github.com/melderan/claude-code-tts"],
    )
    def test_github_urls(self, repo, url):
        git(repo, "remote", "add", "origin", url)
        assert repo_slug(repo) == "melderan/claude-code-tts"

    def test_non_github_is_none(self, repo):
        git(repo, "remote", "add", "origin", "https://example.invalid/x/y.git")
        assert repo_slug(repo) is None


class TestMain:
    def test_check_prints_plan_and_stops_before_tagging(self, repo, capsys):
        set_version(repo, "1.3.0")
        git(repo, "commit", "-q", "-am", "feat: streaming")
        git(repo, "config", "user.signingkey", "DEADBEEF")
        with patch.object(rel, "_repo_dir", return_value=repo), \
             patch.object(rel, "run_gate", return_value=True) as gate:
            rel.main(["--check"])
        out = capsys.readouterr().out
        assert "tag:      v1.3.0" in out
        assert "check ok: would tag v1.3.0" in out
        gate.assert_called_once()
        assert git(repo, "tag", "-l", "v1.3.0") == ""

    def test_problems_exit_before_the_gate(self, repo):
        with patch.object(rel, "_repo_dir", return_value=repo), \
             patch.object(rel, "run_gate") as gate, pytest.raises(SystemExit) as e:
            rel.main(["--check"])
        assert e.value.code == 1
        gate.assert_not_called()

    def test_gate_failure_tags_nothing(self, repo):
        set_version(repo, "1.3.0")
        git(repo, "commit", "-q", "-am", "feat: streaming")
        git(repo, "config", "user.signingkey", "DEADBEEF")
        with patch.object(rel, "_repo_dir", return_value=repo), \
             patch.object(rel, "run_gate", return_value=False), pytest.raises(SystemExit) as e:
            rel.main([])
        assert e.value.code == 1
        assert git(repo, "tag", "-l", "v1.3.0") == ""
