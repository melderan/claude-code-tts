#!/usr/bin/env python3
"""gate.py - the local quality gate (`just gate`, git pre-commit and pre-push hooks).

Runs the same checks CI runs, in order, and fails on the first real failure with the
tool's own output. Nothing here is piped through `tail`, so an exit code can never be
lost; that is the whole reason this file exists (2026-09-22, a flaky test slipped into a
signed commit behind `pytest | tail -1`).

    gate.py            lint, type checks (mypy and ty), version check, tests
    gate.py --full     the same, then the wheel build and cold-install proof
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Variables git exports to a hook that name the committing repository. A test that runs
# `git init` or `git config` in its tmp_path would act on that repository instead: on
# 2026-10-02 a pre-commit run from a linked worktree set core.bare=true and
# commit.gpgsign=false in the main checkout's config and emptied the worktree's index.
# Every step runs without them; tests/conftest.py drops the same set for a bare pytest.
GIT_HOOK_ENV = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_PREFIX",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_QUARANTINE_PATH",
)


def hook_safe_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """The environment for a gate step: the caller's, minus git's hook variables."""
    out = dict(os.environ if env is None else env)
    for name in GIT_HOOK_ENV:
        out.pop(name, None)
    return out

FAST: list[tuple[str, list[str]]] = [
    # First, and cheapest: nothing private (names, paths, hosts, internal tools) leaves this
    # public repo, in files, staged changes, unpushed commit messages or tag notes.
    ("private", ["scripts/private-check.py"]),
    ("lint", ["uv", "tool", "run", "ruff", "check", "src", "tests", "scripts"]),
    ("mypy", ["uv", "tool", "run", "mypy", "src"]),
    ("ty", ["uv", "tool", "run", "ty", "check", "src"]),
    ("version", ["scripts/check-version.sh"]),
    ("tests", ["uv", "run", "--python", "3.12", "--with", "pytest", "pytest", "-q",
               "--ignore=tests/docker", "-p", "no:cacheprovider"]),
]
FULL: list[tuple[str, list[str]]] = [("build", ["scripts/build-check.sh"])]


def main(argv: list[str]) -> int:
    steps = FAST + (FULL if "--full" in argv else [])
    env = hook_safe_env()
    for name, cmd in steps:
        started = time.monotonic()
        proc = subprocess.run(cmd, cwd=REPO, env=env)
        took = time.monotonic() - started
        if proc.returncode != 0:
            print(f"gate: {name} FAILED (exit {proc.returncode}) after {took:.1f}s", file=sys.stderr)
            return proc.returncode or 1
        print(f"gate: {name} ok ({took:.1f}s)")
    print("gate: all clear")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
