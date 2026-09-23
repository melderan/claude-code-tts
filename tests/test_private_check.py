"""scripts/private-check.py: private words never leave the repo, in files or in messages."""

import importlib.util
import re
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "private-check.py"
spec = importlib.util.spec_from_file_location("private_check", SCRIPT)
assert spec and spec.loader
pc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pc)


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.name", "t")
    git(tmp_path, "config", "user.email", "t@example.invalid")
    git(tmp_path, "config", "commit.gpgsign", "false")
    git(tmp_path, "config", "tag.gpgsign", "false")
    (tmp_path / "README.md").write_text("A public file.\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "docs: start")
    words = tmp_path / "words.txt"
    words.write_text("# comment\n\nsecret-host\n/Users/someone\n\\bacme-internal\\b\n")
    return tmp_path, words


class TestPatterns:
    def test_comments_and_blanks_ignored(self, repo):
        _, words = repo
        assert [p.pattern for p in pc.load_patterns(words)] == [
            "secret-host", "/Users/someone", "\\bacme-internal\\b"
        ]

    def test_scan_text_reports_line_and_pattern_once_per_line(self):
        pats = [re.compile("a"), re.compile("b")]
        hits = pc.scan_text("ab\nc\nb\n", pats, "f.txt")
        assert hits == ["f.txt:1: matches /a/", "f.txt:3: matches /b/"]


class TestFiles:
    def test_clean_repo_passes(self, repo, capsys):
        path, words = repo
        assert pc.main(["--repo", str(path), "--words", str(words)]) == 0
        assert "clean (3 patterns)" in capsys.readouterr().out

    def test_tracked_file_hit_fails(self, repo, capsys):
        path, words = repo
        (path / "README.md").write_text("see secret-host for details\n")
        git(path, "commit", "-q", "-am", "docs: oops")
        assert pc.main(["--repo", str(path), "--words", str(words)]) == 1
        assert "README.md:1: matches /secret-host/" in capsys.readouterr().err

    def test_staged_but_uncommitted_file_is_scanned(self, repo):
        path, words = repo
        (path / "new.md").write_text("/Users/someone/private\n")
        git(path, "add", "new.md")
        assert pc.main(["--repo", str(path), "--words", str(words)]) == 1

    def test_case_insensitive(self, repo):
        path, words = repo
        (path / "README.md").write_text("ACME-INTERNAL tooling\n")
        git(path, "commit", "-q", "-am", "docs: caps")
        assert pc.main(["--repo", str(path), "--words", str(words)]) == 1

    def test_word_list_itself_is_never_scanned(self, repo):
        path, words = repo
        (path / ".private-words").write_text("secret-host\n")
        git(path, "add", "-f", ".private-words")
        assert pc.main(["--repo", str(path), "--words", str(path / ".private-words")]) == 0

    def test_missing_list_skips_loudly(self, repo, capsys):
        path, _ = repo
        assert pc.main(["--repo", str(path)]) == 0
        assert "SKIPPED" in capsys.readouterr().out


class TestMessages:
    def test_unpushed_commit_message_hit_fails(self, repo, capsys):
        path, words = repo
        (path / "x.txt").write_text("fine\n")
        git(path, "add", "x.txt")
        git(path, "commit", "-q", "-m", "feat: x\n\nTested on secret-host before pushing.")
        assert pc.main(["--repo", str(path), "--words", str(words)]) == 1
        assert "commit " in capsys.readouterr().err

    def test_tag_note_on_unpushed_commit_is_scanned(self, repo, capsys):
        path, words = repo
        git(path, "tag", "-a", "v0.1.0", "-m", "v0.1.0 - first\n\nBuilt on secret-host.")
        assert pc.main(["--repo", str(path), "--words", str(words)]) == 1
        assert "tag v0.1.0" in capsys.readouterr().err

    def test_pushed_commits_are_not_rescanned(self, repo):
        path, words = repo
        git(path, "commit", "-q", "--allow-empty", "-m", "chore: mentions secret-host")
        # A remote-tracking ref makes the commit "already on a remote".
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True).stdout.strip()
        git(path, "update-ref", "refs/remotes/origin/main", sha)
        assert pc.main(["--repo", str(path), "--words", str(words)]) == 0
