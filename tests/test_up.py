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


@pytest.mark.parametrize(
    ("status", "tags", "expect"),
    [
        ("", "v9.30.0", None),
        ("", "v9.29.0\nv9.30.0", None),
        (" M src/x.py", "v9.30.0", "uncommitted"),
        ("?? new.py", "v9.30.0", "uncommitted"),
        ("", "", "not tagged"),
        ("", "v9.29.0", "not tagged"),
    ],
)
def test_not_released(monkeypatch: pytest.MonkeyPatch, status: str, tags: str, expect: str | None) -> None:
    """The sweep deploys a clean tree tagged v<version>; a bump mid-edit is held."""
    answers = {("status", "--porcelain"): status, ("tag", "--points-at", "HEAD"): tags}
    monkeypatch.setattr(up, "git", lambda *a: answers[a])
    hold = up.not_released("9.30.0")
    assert (hold is None) if expect is None else (expect in hold)


def test_if_changed_holds_an_unreleased_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A differing but untagged version writes one held line and installs nothing."""
    monkeypatch.setattr(up, "LOGDIR", tmp_path)
    monkeypatch.setattr(up, "unchanged", lambda ver: None)
    monkeypatch.setattr(up, "not_released", lambda ver: "HEAD is not tagged v9.30.0")
    monkeypatch.setattr(up, "git", lambda *a: "abc123")
    monkeypatch.setattr(up, "version", lambda: "9.30.0")
    monkeypatch.setattr(up.subprocess, "run", lambda *a, **k: pytest.fail("no command may run"))
    monkeypatch.setenv("UV_OFFLINE", "1")
    assert up.main(["--if-changed"]) == 0
    assert "holding" in capsys.readouterr().out
    assert "uv=offline held (HEAD is not tagged v9.30.0)" in (tmp_path / "timeline.log").read_text()
    assert list(tmp_path.glob("up-*.log")) == []


def _checkout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, status: str, tags: str = "") -> None:
    answers = {("status", "--porcelain"): status, ("tag", "--points-at", "HEAD"): tags}
    monkeypatch.setattr(up, "LOGDIR", tmp_path)
    monkeypatch.setattr(up, "git", lambda *a: answers.get(a, "abc123"))
    monkeypatch.setattr(up, "version", lambda: "9.30.0")


def test_a_plain_run_refuses_a_dirty_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """2026-10-15: a `just up` by hand installed a sandbox's half-done edit on the daemon."""
    _checkout(monkeypatch, tmp_path, " M src/x.py")
    monkeypatch.setattr(up.subprocess, "run", lambda *a, **k: pytest.fail("no command may run"))
    assert up.main([]) == 1
    err = capsys.readouterr().err
    assert "M src/x.py" in err and "--dirty" in err
    assert "refused (working tree has uncommitted changes: M src/x.py)" in (tmp_path / "timeline.log").read_text()


def test_dirty_installs_it_anyway_and_writes_no_ledger_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _checkout(monkeypatch, tmp_path, " M src/x.py")
    ran: list[list[str]] = []

    class Done:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(up.subprocess, "run", lambda cmd, **k: ran.append(cmd) or Done())
    monkeypatch.setattr(up.time, "sleep", lambda s: None)
    monkeypatch.setattr(up, "daemon_state", lambda ver: "running")
    monkeypatch.setattr(up, "announce", lambda ver: pytest.fail("an unreleased build announces nothing"))
    assert up.main(["--dirty"]) == 0
    assert ran and ran[0][:3] == ["uv", "tool", "install"]


def test_a_released_build_writes_the_ledger_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _checkout(monkeypatch, tmp_path, "", "v9.30.0")

    class Done:
        returncode = 0
        stdout = ""
        stderr = ""

    announced: list[str] = []
    monkeypatch.setattr(up.subprocess, "run", lambda cmd, **k: Done())
    monkeypatch.setattr(up.time, "sleep", lambda s: None)
    monkeypatch.setattr(up, "daemon_state", lambda ver: "running")
    monkeypatch.setattr(up, "announce", lambda ver: announced.append(ver) or None)
    assert up.main([]) == 0
    assert announced == ["9.30.0"]


@pytest.mark.parametrize(
    ("argv", "config", "expect"),
    [
        (["--dirty"], "{}", ""),
        (["--dirty", "--offline"], "{}", "1"),
        (["--dirty"], '{"up_offline": true}', "1"),
        (["--dirty"], '{"up_offline": false}', ""),
    ],
)
def test_offline_reaches_every_uv_command_and_the_timeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str], config: str, expect: str | None
) -> None:
    """A run that must add no code from a package index: uv sees UV_OFFLINE=1 on every step."""
    _checkout(monkeypatch, tmp_path, " M src/x.py")
    tts = tmp_path / "tts"
    tts.mkdir()
    (tts / "config.json").write_text(config)
    monkeypatch.setattr(up, "TTS_DIR", tts)
    monkeypatch.setenv("UV_OFFLINE", "")  # recorded, so teardown removes what main() sets
    seen: list[str | None] = []

    class Done:
        returncode = 0
        stdout = ""
        stderr = ""

    def run(cmd, **k):
        if cmd[0] == "uv":
            seen.append(up.os.environ.get("UV_OFFLINE"))
        return Done()

    monkeypatch.setattr(up.subprocess, "run", run)
    monkeypatch.setattr(up.time, "sleep", lambda s: None)
    monkeypatch.setattr(up, "daemon_state", lambda ver: "running")
    assert up.main(argv) == 0
    assert seen == [expect]
    mode = "offline" if expect else "online"
    assert f" uv={mode} " in (tmp_path / "timeline.log").read_text()


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
    import claude_code_tts.state as d
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


def test_ledger_row_pins_the_version_in_field_two(monkeypatch: pytest.MonkeyPatch) -> None:
    """Readers of the shared ledger (the house kit's update line) take field 2 as the version."""
    monkeypatch.setattr(up, "git", lambda *a: "v9.38.1 - the Stop hook speaks from its input")
    row = up.ledger_row("9.38.1", at="2026-10-01T19:40:00Z")
    fields = row.split("\t")
    assert len(fields) == 5
    assert fields[0] == "claude-tts"
    assert fields[1] == "9.38.1"
    assert fields[2] == "2026-10-01T19:40:00Z"
    assert fields[3] == "v9.38.1 - the Stop hook speaks from its input"
    assert fields[4] == "uv tool install --force --build git+https://github.com/melderan/claude-code-tts@v9.38.1"
    assert "\n" not in row


def test_announce_appends_to_the_ledger_the_pointer_names(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    ledger = tmp_path / "host" / "tool-versions.tsv"
    pointer = tmp_path / ".tool-versions-ledger"
    pointer.write_text(f"{ledger}\n")
    monkeypatch.setattr(up, "LEDGER_POINTER", pointer)
    monkeypatch.setattr(up, "git", lambda *a: "v9.38.2 - the landed-response match is exact")
    assert up.announce("9.38.2") == str(ledger)
    assert up.announce("9.38.2") == str(ledger)
    rows = ledger.read_text().splitlines()
    assert len(rows) == 2, "append-only, one row per deploy"
    assert all(r.split("\t")[1] == "9.38.2" for r in rows)


def test_announce_is_silent_without_a_pointer(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(up, "LEDGER_POINTER", tmp_path / "missing")
    assert up.announce("9.38.2") is None

