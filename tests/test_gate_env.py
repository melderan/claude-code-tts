"""The gate and the test suite never see git's hook variables.

2026-10-02: a pre-commit run from a linked worktree exported GIT_DIR and GIT_INDEX_FILE to the
gate; test_release.py and test_private_check.py ran `git init` and `git config` in tmp_path and
git applied them to the committing repository instead (core.bare=true, commit.gpgsign=false in the
main checkout, an emptied index). These tests pin the scrub on both paths.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "gate.py"
spec = importlib.util.spec_from_file_location("gate_under_test", SCRIPT)
assert spec is not None and spec.loader is not None
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def test_hook_safe_env_drops_every_git_hook_variable():
    env = dict.fromkeys(gate.GIT_HOOK_ENV, "/elsewhere")
    env["PATH"] = "/usr/bin"
    env["GIT_AUTHOR_NAME"] = "kept"
    out = gate.hook_safe_env(env)
    assert not any(name in out for name in gate.GIT_HOOK_ENV)
    assert out["PATH"] == "/usr/bin"
    assert out["GIT_AUTHOR_NAME"] == "kept"
    assert env["GIT_DIR"] == "/elsewhere", "the caller's dict is not changed"


def test_gate_runs_steps_without_git_hook_variables(monkeypatch):
    """main() passes the scrubbed environment to every step, with GIT_DIR set as a hook sets it."""
    monkeypatch.setenv("GIT_DIR", "/some/repo/.git/worktrees/x")
    monkeypatch.setenv("GIT_INDEX_FILE", "/some/repo/.git/worktrees/x/index")
    seen: list[dict[str, str] | None] = []

    def fake_run(cmd, cwd, env=None):
        seen.append(env)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(gate.subprocess, "run", fake_run)
    monkeypatch.setattr(gate, "FAST", [("one", ["true"]), ("two", ["true"])])
    monkeypatch.setattr(gate, "FULL", [])
    assert gate.main([]) == 0
    assert len(seen) == 2
    for env in seen:
        assert env is not None
        assert "GIT_DIR" not in env and "GIT_INDEX_FILE" not in env


def test_suite_process_has_no_git_hook_variables():
    """conftest.py dropped them at import, so a test's `git init` in tmp_path stays in tmp_path."""
    assert not any(name in os.environ for name in gate.GIT_HOOK_ENV)


def test_git_init_in_tmp_path_touches_only_tmp_path(tmp_path):
    """The failure as it happened: with the scrub, git init lands where cwd says."""
    subprocess.run(["git", "init", "-q", str(tmp_path / "r")], check=True)
    assert (tmp_path / "r" / ".git" / "config").exists()
