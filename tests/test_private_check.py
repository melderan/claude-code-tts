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
            "secret-host",
            "/Users/someone",
            "\\bacme-internal\\b",
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
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True
        ).stdout.strip()
        git(path, "update-ref", "refs/remotes/origin/main", sha)
        assert pc.main(["--repo", str(path), "--words", str(words)]) == 0


def test_synced_block_age_none_warn_fail_thresholds() -> None:
    today = __import__("datetime").date(2026, 9, 30)
    header = pc.SYNC_HEADER
    assert pc.synced_block_age("\\bfoo\\b\n", today) is None
    assert pc.synced_block_age(f"{header}2026-09-30 ---\n", today) == 0
    assert pc.synced_block_age(f"{header}2026-09-01 ---\n", today) == 29
    assert pc.synced_block_age(f"{header}not-a-date ---\n", today) is None
    assert pc.STALE_WARN_DAYS < pc.STALE_FAIL_DAYS


def test_stale_synced_block_fails_and_fresh_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", repo], check=True)
    (repo / "ok.txt").write_text("nothing private\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    old = (
        __import__("datetime").date.today()
        - __import__("datetime").timedelta(days=pc.STALE_FAIL_DAYS + 1)
    ).isoformat()
    words = repo / ".private-words"
    words.write_text(f"{pc.SYNC_HEADER}{old} ---\n\\bsecret-9\\b\n# --- end managed block ---\n")
    assert pc.main(["--repo", str(repo)]) == 1
    assert "days old" in capsys.readouterr().err
    words.write_text(
        f"{pc.SYNC_HEADER}{__import__('datetime').date.today().isoformat()} ---\n\\bsecret-9\\b\n# --- end managed block ---\n"
    )
    assert pc.main(["--repo", str(repo)]) == 0


class TestApprovedLapses:
    """`.private-allow`: a known, accepted hit is printed by name and reason, and does not fail the gate."""

    def _hit(self, repo):
        path, words = repo
        (path / "kit.sh").write_text("docker run secret-host\n")
        git(path, "add", "kit.sh")  # not -A: the fixture's word list sits untracked beside it
        git(path, "commit", "-q", "-m", "chore: kit")
        return path, words

    def test_an_approved_hit_passes_and_is_printed_with_its_reason(self, repo, capsys):
        path, words = self._hit(repo)
        allow = path / "allow.tsv"
        allow.write_text("kit.sh\tsecret-host\tJMO 2026-10-05: the kit talks to that host by name\n")
        assert pc.main(["--repo", str(path), "--words", str(words), "--allow", str(allow)]) == 0
        out = capsys.readouterr()
        assert "approved kit.sh:1 /secret-host/ (JMO 2026-10-05: the kit talks to that host by name)" in out.out
        assert "clean (3 patterns, 1 approved lapse)" in out.out

    def test_an_approval_for_another_pattern_or_place_does_not_cover_the_hit(self, repo, capsys):
        path, words = self._hit(repo)
        allow = path / "allow.tsv"
        allow.write_text("kit.sh\t/Users/someone\tJMO: wrong pattern\nREADME.md\tsecret-host\tJMO: wrong place\n")
        assert pc.main(["--repo", str(path), "--words", str(words), "--allow", str(allow)]) == 1
        err = capsys.readouterr().err
        assert "kit.sh:1: matches /secret-host/" in err
        assert "allow line unused" in err

    def test_a_glob_covers_a_directory_and_a_commit(self, repo, capsys):
        path, words = self._hit(repo)
        git(path, "commit", "-q", "--allow-empty", "-m", "fix: see /Users/someone/notes")
        allow = path / "allow.tsv"
        allow.write_text("kit.sh\t*\tJMO: every pattern in that file\ncommit *\t/Users/someone\tJMO: history stays\n")
        assert pc.main(["--repo", str(path), "--words", str(words), "--allow", str(allow)]) == 0
        assert "2 approved lapses" in capsys.readouterr().out

    def test_an_approval_without_a_reason_fails_the_gate(self, repo, capsys):
        path, words = self._hit(repo)
        allow = path / "allow.tsv"
        allow.write_text("kit.sh\tsecret-host\n")
        assert pc.main(["--repo", str(path), "--words", str(words), "--allow", str(allow)]) == 1
        assert "allow line 1 needs where, pattern and a reason" in capsys.readouterr().err

    def test_an_unused_approval_warns_but_passes(self, repo, capsys):
        path, words = repo
        allow = path / "allow.tsv"
        allow.write_text("gone.sh\tsecret-host\tJMO: approved long ago\n")
        assert pc.main(["--repo", str(path), "--words", str(words), "--allow", str(allow)]) == 0
        out = capsys.readouterr()
        assert "allow line unused: gone.sh" in out.err
        assert "clean (3 patterns)" in out.out

    def test_the_allow_file_itself_is_never_scanned(self, repo):
        path, words = repo
        allow = path / ".private-allow"
        allow.write_text("kit.sh\tsecret-host\tJMO: the pattern text sits in this file\n")
        git(path, "add", "-f", ".private-allow")
        assert pc.main(["--repo", str(path), "--words", str(words), "--allow", str(allow)]) == 0
