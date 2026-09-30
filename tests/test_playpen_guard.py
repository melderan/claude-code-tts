"""Importing the package under pytest with a real HOME must refuse: ~/.claude-tts is live state.

On 2026-09-30 a reproduction script outside tests/ ran with the real HOME, called the pause
command, and held a real daemon's queue for half an hour. tests/conftest.py already points HOME
at a throwaway directory; this guard makes any other pytest import fail loudly instead.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).parent.parent / "src"
PROBE = "import pytest, claude_code_tts.config as c; print('imported', c.PLAYPEN_HOME_PREFIX)"


def _run(home: Path, **env_extra: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_TTS_ALLOW_REAL_HOME"}
    env.update(HOME=str(home), PYTHONPATH=str(SRC), **env_extra)
    return subprocess.run([sys.executable, "-c", PROBE], env=env, capture_output=True, text=True)


def test_real_home_under_pytest_is_refused(tmp_path):
    r = _run(tmp_path / "looks-like-a-real-home")
    assert r.returncode != 0
    assert "playpen" in r.stderr and "RuntimeError" in r.stderr


def test_playpen_home_is_accepted(tmp_path):
    home = tmp_path / "claude-tts-test-home-abc"
    home.mkdir()
    r = _run(home)
    assert r.returncode == 0, r.stderr
    assert "imported" in r.stdout


def test_explicit_opt_out_is_accepted(tmp_path):
    r = _run(tmp_path / "real", CLAUDE_TTS_ALLOW_REAL_HOME="1")
    assert r.returncode == 0, r.stderr


def test_without_pytest_the_real_home_is_fine(tmp_path):
    env = dict(os.environ, HOME=str(tmp_path), PYTHONPATH=str(SRC))
    r = subprocess.run([sys.executable, "-c", "import claude_code_tts.config; print('ok')"],
                       env=env, capture_output=True, text=True)
    assert r.returncode == 0 and "ok" in r.stdout
