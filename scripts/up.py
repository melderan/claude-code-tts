#!/usr/bin/env python3
"""up.py - the operator step, run on the machine that owns the daemon (`just up`).

Rebuild the CLI from this checkout, deploy hooks and commands, restart the daemon,
verify the heartbeat, and record the run:

    .logs/just/up-<utc time>-<git ref>.log   full output of every step
    .logs/just/timeline.log                  one line per run, newest last (`just timeline`)

.logs/ is gitignored and lives in the checkout, so a sandbox sharing the working
tree can read what the host is running without asking. Standard library only.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LOGDIR = REPO / ".logs" / "just"
HOME = Path.home()
TTS_DIR = HOME / ".claude-tts"
HEARTBEAT_MAX_AGE = 10.0


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def git(*args: str) -> str:
    try:
        return subprocess.run(
            ["git", *args], cwd=REPO, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (subprocess.CalledProcessError, OSError):
        return "unknown"


def version() -> str:
    text = (REPO / "src" / "claude_code_tts" / "__init__.py").read_text()
    m = re.search(r'^__version__ = "([^"]+)"', text, re.M)
    return m.group(1) if m else "unknown"


def config_flag(path: list[str]) -> str:
    try:
        node: object = json.loads((TTS_DIR / "config.json").read_text())
        for key in path:
            node = node[key]  # type: ignore[index]
        return "on" if node else "off"
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return "off"


def heartbeat_fresh() -> bool:
    try:
        return time.time() - (TTS_DIR / "daemon.heartbeat").stat().st_mtime < HEARTBEAT_MAX_AGE
    except OSError:
        return False


def main() -> int:
    LOGDIR.mkdir(parents=True, exist_ok=True)
    ref = git("describe", "--always", "--dirty", "--tags")
    branch = git("branch", "--show-current") or "detached"
    ver = version()
    started = time.time()
    run = LOGDIR / f"up-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{ref}.log"
    timeline = LOGDIR / "timeline.log"
    ident = f"up v{ver} {ref} {branch}"

    def record(line: str) -> None:
        with timeline.open("a") as f:
            f.write(line + "\n")
        with run.open("a") as f:
            f.write(line + "\n")
        print(line)

    def step(name: str, *cmds: list[str]) -> None:
        """Run commands in order; the first that succeeds ends the step, all failing fails it."""
        with run.open("a") as f:
            f.write(f"== {stamp()} {name}\n")
            f.flush()
            for cmd in cmds:
                f.write(f"$ {' '.join(cmd)}\n")
                f.flush()
                proc = subprocess.run(cmd, cwd=REPO, stdout=f, stderr=subprocess.STDOUT)
                if proc.returncode == 0:
                    return
                f.write(f"(exit {proc.returncode})\n")
        record(f"{stamp()} {ident} FAILED at {name} run={run.name}")
        print(f"details: {run}")
        sys.exit(1)

    print(f"up: v{ver} {ref} ({branch}) -> {run}")
    # --build: some uv configs refuse to build source distributions; harmless elsewhere.
    step("install", ["uv", "tool", "install", ".", "--force", "--build"])
    # The installer would restart the daemon itself; we do the one restart below instead,
    # so the daemon finishes its current sentence once, not twice.
    step("hooks", ["claude-tts-install", "--upgrade", "--no-daemon-restart"])
    step("restart", ["claude-tts", "daemon", "restart"], ["claude-tts", "daemon", "start"])
    time.sleep(2)

    with run.open("a") as f:
        f.write(f"== {stamp()} verify\n")
        f.flush()
        subprocess.run(["claude-tts", "daemon", "status"], stdout=f, stderr=subprocess.STDOUT)

    daemon = "running" if heartbeat_fresh() else "DOWN"
    try:
        pid = (TTS_DIR / "daemon.pid").read_text().strip() or "-"
    except OSError:
        pid = "-"
    record(
        f"{stamp()} {ident} daemon={daemon} pid={pid} "
        f"bridge={config_flag(['http', 'enabled'])} mic={config_flag(['mic_aware_pause'])} "
        f"took={int(time.time() - started)}s run={run.name}"
    )
    return 0 if daemon == "running" else 1


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    sys.exit(main())
