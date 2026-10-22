"""scripts/ship.sh: commit, push and release in one call, stopping at the first failure.

The scripts run from copies inside a throwaway repo with a bare remote. bump-my-version and
ci-wait are fakes on the PATH, and the repo's own claude_code_tts.cli is a fake that records
its arguments, so nothing here touches this checkout, GitHub or a real release.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_commit_feature import FAKE_BUMP, git

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"

FAKE_CLI = """import os, sys
from pathlib import Path
Path(os.environ["SHIP_TEST_RECORD"]).write_text(" ".join(sys.argv[1:]))
sys.exit(int(os.environ.get("SHIP_TEST_RELEASE_RC", "0")))
"""


@pytest.fixture
def repo(tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    root = tmp_path / "repo"
    pkg = root / "src" / "claude_code_tts"
    pkg.mkdir(parents=True)
    (root / "scripts").mkdir()
    for name in ("ship.sh", "commit-feature.sh", "host-python.sh"):
        shutil.copy2(SCRIPTS / name, root / "scripts" / name)
    (pkg / "__init__.py").write_text('__version__ = "1.2.3"\n')
    (pkg / "cli.py").write_text(FAKE_CLI)
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "t")
    git(root, "config", "user.email", "t@example.invalid")
    git(root, "config", "commit.gpgsign", "false")
    git(root, "remote", "add", "origin", str(remote))
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "chore: start")
    git(root, "push", "-q", "-u", "origin", "main")
    (root / "change.txt").write_text("a change\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("bump-my-version", FAKE_BUMP), ("ci-wait", "#!/bin/sh\nexit 0\n")):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    return root, remote, bin_dir, tmp_path / "release-args"


def path_without_ci_wait(bin_dir: Path) -> str:
    keep = [d for d in os.environ["PATH"].split(os.pathsep) if not (Path(d) / "ci-wait").exists()]
    return os.pathsep.join([str(bin_dir), *keep])


def run(repo, *args: str, release_rc: int = 0, ci_wait: bool = True) -> subprocess.CompletedProcess[str]:
    root, _, bin_dir, record = repo
    if not ci_wait:
        (bin_dir / "ci-wait").unlink()
    env = dict(
        os.environ,
        PATH=path_without_ci_wait(bin_dir),
        SHIP_TEST_RECORD=str(record),
        SHIP_TEST_RELEASE_RC=str(release_rc),
    )
    return subprocess.run(
        [str(root / "scripts" / "ship.sh"), *args], cwd=root, env=env, input="", capture_output=True, text=True
    )


def test_commits_pushes_and_releases_with_wait_ci(repo):
    root, remote, _, record = repo
    r = run(repo, "fix", "handle the empty queue")
    assert r.returncode == 0, r.stdout + r.stderr
    assert git(root, "log", "-1", "--format=%s") == "fix: handle the empty queue\n"
    assert git(remote, "rev-parse", "main") == git(root, "rev-parse", "HEAD")
    assert record.read_text() == "release --wait-ci"
    assert "1.2.4 committed, pushed and released" in r.stdout


def test_without_ci_wait_it_refuses_before_committing(repo):
    root, _, _, record = repo
    r = run(repo, "fix", "something", ci_wait=False)
    assert r.returncode == 2
    assert "nothing committed" in r.stdout
    assert git(root, "log", "--format=%s") == "chore: start\n"
    assert '"1.2.3"' in (root / "src" / "claude_code_tts" / "__init__.py").read_text()
    assert not record.exists()


def test_a_failed_push_stops_before_release(repo):
    root, _, _, record = repo
    git(root, "remote", "set-url", "origin", str(root.parent / "missing.git"))
    r = run(repo, "fix", "something")
    assert r.returncode != 0
    assert "committed locally, not pushed" in r.stdout
    assert git(root, "log", "-1", "--format=%s") == "fix: something\n"
    assert not record.exists()


def test_a_failed_release_keeps_its_exit_code(repo):
    root, remote, _, _ = repo
    r = run(repo, "fix", "something", release_rc=1)
    assert r.returncode == 1
    assert "pushed, not released" in r.stdout
    assert git(remote, "rev-parse", "main") == git(root, "rev-parse", "HEAD")


def test_a_refused_commit_stops_everything(repo):
    _, remote, _, record = repo
    before = git(remote, "rev-parse", "main")
    r = run(repo, "style", "something")
    assert r.returncode == 2
    assert git(remote, "rev-parse", "main") == before
    assert not record.exists()
