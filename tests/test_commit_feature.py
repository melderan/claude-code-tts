"""scripts/commit-feature.sh: a change and its version bump land as one commit, with no tag.

The script runs from a copy inside a throwaway repo; bump-my-version is a fake on the PATH
that rewrites __init__.py the way the real one does, so nothing here touches this checkout.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "commit-feature.sh"

FAKE_BUMP = """#!/bin/sh
f=src/claude_code_tts/__init__.py
v=$(sed -n 's/^__version__ = "\\(.*\\)"$/\\1/p' "$f")
major=${v%%.*}; rest=${v#*.}; minor=${rest%%.*}; patch=${rest#*.}
case "$2" in
  minor) new="$major.$((minor + 1)).0" ;;
  patch) new="$major.$minor.$((patch + 1))" ;;
  *) exit 9 ;;
esac
printf '__version__ = "%s"\\n' "$new" > "$f"
"""


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "src" / "claude_code_tts").mkdir(parents=True)
    shutil.copy2(SCRIPT, root / "scripts" / "commit-feature.sh")
    (root / "src" / "claude_code_tts" / "__init__.py").write_text('__version__ = "1.2.3"\n')
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "t")
    git(root, "config", "user.email", "t@example.invalid")
    git(root, "config", "commit.gpgsign", "false")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "chore: start")
    (root / "change.txt").write_text("a change\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "bump-my-version"
    fake.write_text(FAKE_BUMP)
    fake.chmod(0o755)
    return root, bin_dir


def run(repo, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    root, bin_dir = repo
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}")
    return subprocess.run(
        [str(root / "scripts" / "commit-feature.sh"), *args],
        cwd=root,
        env=env,
        input=stdin if stdin is not None else "",
        capture_output=True,
        text=True,
    )


def test_body_file_becomes_the_message_after_the_subject(repo, tmp_path):
    root, _ = repo
    body = tmp_path / "body.txt"
    body.write_text("Why it changed.\n\nCo-Authored-By: Someone <x@example.invalid>\n")
    r = run(repo, "-y", "-F", str(body), "fix", "handle the empty queue")
    assert r.returncode == 0, r.stdout + r.stderr
    assert git(root, "log", "-1", "--format=%B").strip() == (
        "fix: handle the empty queue\n\nWhy it changed.\n\nCo-Authored-By: Someone <x@example.invalid>"
    )
    assert '"1.2.4"' in (root / "src" / "claude_code_tts" / "__init__.py").read_text()
    assert git(root, "status", "--porcelain") == ""
    assert set(git(root, "show", "--name-only", "--format=", "HEAD").split()) == {
        "change.txt",
        "src/claude_code_tts/__init__.py",
    }


def test_feat_bumps_minor_and_makes_no_tag(repo):
    root, _ = repo
    r = run(repo, "-y", "feat", "a new thing")
    assert r.returncode == 0, r.stdout + r.stderr
    assert git(root, "log", "-1", "--format=%B").strip() == "feat: a new thing"
    assert '"1.3.0"' in (root / "src" / "claude_code_tts" / "__init__.py").read_text()
    assert git(root, "tag", "-l") == ""


def test_docs_keeps_its_prefix_and_bumps_patch(repo):
    root, _ = repo
    r = run(repo, "-y", "docs", "say it plainly")
    assert r.returncode == 0, r.stdout + r.stderr
    assert git(root, "log", "-1", "--format=%s") == "docs: say it plainly\n"
    assert '"1.2.4"' in (root / "src" / "claude_code_tts" / "__init__.py").read_text()


def test_without_y_and_no_terminal_it_refuses_and_changes_nothing(repo):
    root, _ = repo
    r = run(repo, "fix", "something", stdin="y")
    assert r.returncode == 2
    assert "pass -y" in r.stdout
    assert git(root, "log", "--format=%s") == "chore: start\n"
    assert '"1.2.3"' in (root / "src" / "claude_code_tts" / "__init__.py").read_text()


def test_missing_body_file_refuses_before_bumping(repo, tmp_path):
    root, _ = repo
    r = run(repo, "-y", "-F", str(tmp_path / "nope.txt"), "fix", "something")
    assert r.returncode == 2
    assert '"1.2.3"' in (root / "src" / "claude_code_tts" / "__init__.py").read_text()


def test_unknown_type_refuses(repo):
    r = run(repo, "-y", "style", "something")
    assert r.returncode == 2
    assert "Unknown type" in r.stdout
