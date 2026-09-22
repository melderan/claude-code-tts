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

import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

FAST: list[tuple[str, list[str]]] = [
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
    for name, cmd in steps:
        started = time.monotonic()
        proc = subprocess.run(cmd, cwd=REPO)
        took = time.monotonic() - started
        if proc.returncode != 0:
            print(f"gate: {name} FAILED (exit {proc.returncode}) after {took:.1f}s", file=sys.stderr)
            return proc.returncode or 1
        print(f"gate: {name} ok ({took:.1f}s)")
    print("gate: all clear")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
