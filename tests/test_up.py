"""up.py: --if-changed skips only when CLI, daemon and checkout agree and the daemon is alive."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "up.py"
spec = importlib.util.spec_from_file_location("up", SCRIPT)
assert spec and spec.loader
up = importlib.util.module_from_spec(spec)
spec.loader.exec_module(up)


@pytest.mark.parametrize(
    ("installed", "daemon", "alive", "expect_skip"),
    [
        ("9.30.0", "9.30.0", True, True),
        ("9.29.0", "9.30.0", True, False),
        ("9.30.0", "9.29.0", True, False),
        ("9.30.0", "9.30.0", False, False),
        ("", "", True, False),
    ],
)
def test_unchanged(
    monkeypatch: pytest.MonkeyPatch, installed: str, daemon: str, alive: bool, expect_skip: bool
) -> None:
    monkeypatch.setattr(up, "installed_version", lambda: installed)
    monkeypatch.setattr(up, "daemon_version", lambda: daemon)
    monkeypatch.setattr(up, "heartbeat_fresh", lambda: alive)
    assert (up.unchanged("9.30.0") is not None) is expect_skip


def test_installed_version_parses_the_cli_line(monkeypatch: pytest.MonkeyPatch) -> None:
    class R:
        stdout = "claude-tts 9.30.0\n"

    monkeypatch.setattr(up.subprocess, "run", lambda *a, **k: R())
    assert up.installed_version() == "9.30.0"


def test_unknown_argument_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    assert up.main(["--bogus"]) == 2
    assert "--if-changed" in capsys.readouterr().err
