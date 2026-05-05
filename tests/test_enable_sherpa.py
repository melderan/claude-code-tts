"""Tests for `claude-tts-install --enable-sherpa`.

Covers the prompt-before-download discipline JMO asked for in
feedback_installer_interactive.md:
  - --yes short-circuits the prompt
  - "n" to the prompt aborts cleanly without running uv
  - Already-bootstrapped venv is detected and short-circuits without re-installing
  - Missing `uv` on PATH bails with a clear error
  - --dry-run prints the plan but never invokes subprocess
"""

from __future__ import annotations

import io
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from claude_code_tts import install


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Redirect HOME so the installer touches a clean tmp path."""
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def have_uv(monkeypatch):
    """Pretend `uv` is on PATH."""
    real_which = install.shutil.which

    def fake_which(name):
        if name == "uv":
            return "/usr/local/bin/uv"
        return real_which(name)

    monkeypatch.setattr(install.shutil, "which", fake_which)


def _make_fake_run(touch_venv_python: bool = True, sherpa_version: str = "1.13.0"):
    """Build a fake subprocess.run that mirrors what real uv/python would do.

    When `uv venv <dir>` is called, the venv's bin/python is materialized so
    later `_verify_sherpa_import` finds it. When `python -c 'import sherpa_onnx'`
    is called, returns the version string the verify step expects.
    """
    calls: list[list] = []

    def fake_run(cmd, **kwargs):
        cmd_list = list(cmd)
        calls.append(cmd_list)

        # `uv venv <dir> --python 3.12` — create the bin/python so verify finds it
        if touch_venv_python and len(cmd_list) >= 3 and cmd_list[:2] == ["uv", "venv"]:
            venv_dir = Path(cmd_list[2])
            (venv_dir / "bin").mkdir(parents=True, exist_ok=True)
            py = venv_dir / "bin" / "python"
            py.write_text("#!/bin/sh\nexit 0\n")
            py.chmod(0o755)
            return MagicMock(returncode=0, stdout="", stderr="")

        # `<venv-py> -c "import sherpa_onnx; print(...)"` — verify probe
        if len(cmd_list) >= 3 and cmd_list[1] == "-c" and "sherpa_onnx" in cmd_list[2]:
            return MagicMock(returncode=0, stdout=f"{sherpa_version}\n", stderr="")

        # `<venv-py> -c "import onnxruntime; print(...)"` — provider probe
        if len(cmd_list) >= 3 and cmd_list[1] == "-c" and "onnxruntime" in cmd_list[2]:
            return MagicMock(
                returncode=0,
                stdout="CoreMLExecutionProvider\nCPUExecutionProvider\n",
                stderr="",
            )

        return MagicMock(returncode=0, stdout="", stderr="")

    fake_run.calls = calls
    return fake_run


class TestPreflight:
    def test_no_uv_on_path_bails(self, fake_home, monkeypatch, capsys):
        monkeypatch.setattr(install.shutil, "which", lambda _: None)
        rc = install.do_enable_sherpa(assume_yes=True)
        assert rc == 2
        captured = capsys.readouterr()
        assert "uv" in captured.out.lower()

    def test_dry_run_does_not_invoke_subprocess(self, fake_home, have_uv):
        with patch.object(install.subprocess, "run") as mock_run:
            rc = install.do_enable_sherpa(assume_yes=True, dry_run=True)
        assert rc == 0
        # _verify_sherpa_import in the idempotency check would call run, but
        # only if the venv python exists. Fresh fake_home means no venv → no
        # idempotency probe → no subprocess at all in dry-run.
        mock_run.assert_not_called()


class TestPromptDiscipline:
    def test_assume_yes_skips_prompt_and_runs_bootstrap(self, fake_home, have_uv):
        fake_run = _make_fake_run()
        with patch.object(install.subprocess, "run", side_effect=fake_run), \
             patch("builtins.input") as mock_input:
            rc = install.do_enable_sherpa(assume_yes=True)

        assert rc == 0
        mock_input.assert_not_called()  # --yes => no prompts
        cmd_strings = [" ".join(c) for c in fake_run.calls]
        assert any("uv venv" in s for s in cmd_strings)
        assert any("uv pip install" in s and "sherpa-onnx" in s for s in cmd_strings)

    def test_user_says_no_aborts_cleanly(self, fake_home, have_uv):
        with patch.object(install.subprocess, "run") as mock_run, \
             patch("builtins.input", return_value="n"):
            rc = install.do_enable_sherpa(assume_yes=False)
        assert rc == 0  # clean abort, not an error
        mock_run.assert_not_called()  # nothing installed

    def test_user_says_yes_runs_bootstrap(self, fake_home, have_uv):
        fake_run = _make_fake_run()
        with patch.object(install.subprocess, "run", side_effect=fake_run), \
             patch("builtins.input", return_value="y"):
            rc = install.do_enable_sherpa(assume_yes=False)
        assert rc == 0
        assert any("uv venv" in " ".join(c) for c in fake_run.calls)


class TestIdempotency:
    def test_already_bootstrapped_short_circuits(self, fake_home, have_uv, capsys):
        # Pre-create a fake venv that "looks bootstrapped"
        venv = fake_home / ".claude-tts" / "venvs" / "sherpa"
        (venv / "bin").mkdir(parents=True)
        py = venv / "bin" / "python"
        py.write_text("#!/bin/sh\nexit 0\n")
        py.chmod(0o755)

        with patch.object(install.subprocess, "run") as mock_run, \
             patch("builtins.input") as mock_input:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="1.13.0\n", stderr=""
            )
            rc = install.do_enable_sherpa(assume_yes=True)

        assert rc == 0
        mock_input.assert_not_called()
        # Subprocess was called for verify (and provider probe), but NOT for
        # `uv venv` or `uv pip install` — idempotency held.
        commands_run = [list(c.args[0]) for c in mock_run.mock_calls if c.args]
        cmd_strings = [" ".join(c) for c in commands_run]
        assert not any("uv venv" in s for s in cmd_strings), \
            f"uv venv ran on already-bootstrapped install: {cmd_strings}"
        assert not any("pip install" in s for s in cmd_strings), \
            f"pip install ran on already-bootstrapped install: {cmd_strings}"
        captured = capsys.readouterr()
        assert "already installed" in captured.out


class TestVerification:
    def test_verify_failure_returns_nonzero(self, fake_home, have_uv):
        def fake_run(cmd, **kwargs):
            # Both venv create and pip install succeed
            if "uv" in cmd[0] or cmd[0] == "/usr/local/bin/uv":
                return MagicMock(returncode=0, stdout="", stderr="")
            # ...but the post-install verify fails
            return MagicMock(
                returncode=1, stdout="", stderr="ImportError: no sherpa_onnx",
            )

        with patch.object(install.subprocess, "run", side_effect=fake_run), \
             patch("builtins.input", return_value="y"):
            rc = install.do_enable_sherpa(assume_yes=True)
        assert rc == 6  # verify-failed exit code


class TestPaths:
    def test_paths_under_claude_tts_home(self, fake_home):
        venv, py, models = install._sherpa_paths()
        # All three should be under HOME/.claude-tts/
        assert str(venv).startswith(str(fake_home / ".claude-tts"))
        assert py == venv / "bin" / "python"
        assert models == fake_home / ".claude-tts" / "sherpa-models"
