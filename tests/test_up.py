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


def test_daemon_version_reads_the_release_marker_not_the_protocol_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(up, "TTS_DIR", tmp_path)
    (tmp_path / "daemon.version").write_text("control-v1")
    assert up.daemon_version() == "", (
        "no release marker yet: never report the protocol tag as a version"
    )
    (tmp_path / "daemon.release").write_text("9.32.1\n")
    assert up.daemon_version() == "9.32.1"


def test_daemon_writes_a_release_marker_the_script_can_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import claude_code_tts.daemon as d
    from claude_code_tts import __version__

    monkeypatch.setattr(d, "VERSION_FILE", tmp_path / "daemon.version")
    written = d.write_release_marker()
    assert written == tmp_path / "daemon.release" and written.read_text() == __version__
    monkeypatch.setattr(up, "TTS_DIR", tmp_path)
    assert up.daemon_version() == __version__


def test_install_asks_for_the_preferred_python_then_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """The first install command pins PREFERRED_PYTHON; the alternative has no pin."""
    import inspect

    src = inspect.getsource(up.main)
    assert '"--python", PREFERRED_PYTHON' in src
    after_pin = src.split('"--python", PREFERRED_PYTHON', 1)[1]
    assert '["uv", "tool", "install", ".", "--force", "--build"],' in after_pin
    assert up.PREFERRED_PYTHON == "3.14"


def test_tool_python_reads_the_shebang(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_py = tmp_path / "python3"
    fake_py.write_text("#!/bin/sh\necho 'Python 3.14.4'\n")
    fake_py.chmod(0o755)
    tool = tmp_path / "claude-tts"
    tool.write_text(f"#!{fake_py}\nprint('hi')\n")
    monkeypatch.setattr(up.shutil, "which", lambda name: str(tool) if name == "claude-tts" else None)
    assert up.tool_python() == str(fake_py)
    assert up.tool_python_version() == "3.14.4"


def test_tool_python_is_empty_without_the_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(up.shutil, "which", lambda name: None)
    assert up.tool_python() == ""
    assert up.tool_python_version() == "-"
