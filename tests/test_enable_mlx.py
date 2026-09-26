"""Tests for `claude-tts-install --enable-mlx`, the mlx-audio venv bootstrap.

Same discipline as --enable-sherpa: the platform is checked first (MLX is
Apple silicon only), nothing downloads before the prompt, --yes skips the
prompt, --dry-run prints the plan and runs nothing, and a working venv
short-circuits without reinstalling.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from claude_code_tts import install


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def apple_silicon(monkeypatch):
    monkeypatch.setattr(install.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(install.platform, "machine", lambda: "arm64")


@pytest.fixture
def have_uv(monkeypatch):
    real_which = install.shutil.which
    monkeypatch.setattr(install.shutil, "which", lambda name: "/usr/local/bin/uv" if name == "uv" else real_which(name))


def _fake_run(version="0.5.6"):
    calls: list[list[str]] = []

    def run(cmd, **kwargs):
        cmd_list = list(cmd)
        calls.append(cmd_list)
        if cmd_list[:2] == ["uv", "venv"]:
            venv_dir = Path(cmd_list[2])
            (venv_dir / "bin").mkdir(parents=True, exist_ok=True)
            (venv_dir / "bin" / "python").write_text("#!/bin/sh\nexit 0\n")
            return MagicMock(returncode=0, stdout="", stderr="")
        if len(cmd_list) >= 3 and cmd_list[1] == "-c" and "mlx_audio" in cmd_list[2]:
            return MagicMock(returncode=0, stdout=f"{version}\n", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    run.calls = calls  # type: ignore[attr-defined]
    return run


class TestPlatformGate:
    @pytest.mark.parametrize("system,machine", [("Linux", "x86_64"), ("Darwin", "x86_64"), ("Linux", "aarch64")])
    def test_refuses_off_apple_silicon(self, fake_home, monkeypatch, capsys, system, machine):
        monkeypatch.setattr(install.platform, "system", lambda: system)
        monkeypatch.setattr(install.platform, "machine", lambda: machine)
        with patch.object(install.subprocess, "run") as mock_run:
            assert install.do_enable_mlx(assume_yes=True) == 7
        mock_run.assert_not_called()
        out = capsys.readouterr().out
        assert "Apple silicon" in out and "--enable-sherpa" in out


class TestPreflight:
    def test_no_uv_bails(self, fake_home, apple_silicon, monkeypatch, capsys):
        monkeypatch.setattr(install.shutil, "which", lambda _: None)
        assert install.do_enable_mlx(assume_yes=True) == 2
        assert "uv" in capsys.readouterr().out.lower()

    def test_dry_run_runs_nothing(self, fake_home, apple_silicon, have_uv, capsys):
        with patch.object(install.subprocess, "run") as mock_run:
            assert install.do_enable_mlx(assume_yes=True, dry_run=True) == 0
        mock_run.assert_not_called()
        out = capsys.readouterr().out
        assert "uv venv" in out and "mlx-audio[tts]" in out and "misaki[en]" in out


class TestPromptDiscipline:
    def test_yes_skips_prompt_and_installs(self, fake_home, apple_silicon, have_uv, capsys):
        run = _fake_run()
        with patch.object(install.subprocess, "run", side_effect=run), patch("builtins.input") as mock_input:
            assert install.do_enable_mlx(assume_yes=True) == 0
        mock_input.assert_not_called()
        joined = [" ".join(c) for c in run.calls]
        assert any(s.startswith("uv venv") and s.endswith("--python 3.12") for s in joined)
        assert any("uv pip install" in s and "mlx-audio[tts]" in s and "misaki[en]" in s for s in joined)
        out = capsys.readouterr().out
        assert "mlx-audio 0.5.6 installed and verified" in out
        assert "claude-tts mlx pull kokoro" in out

    def test_no_aborts_cleanly(self, fake_home, apple_silicon, have_uv):
        with patch.object(install.subprocess, "run") as mock_run, patch("builtins.input", return_value="n"):
            assert install.do_enable_mlx(assume_yes=False) == 0
        mock_run.assert_not_called()

    def test_pip_failure_is_reported(self, fake_home, apple_silicon, have_uv, capsys):
        import subprocess

        def run(cmd, **kwargs):
            cmd_list = list(cmd)
            if cmd_list[:2] == ["uv", "venv"]:
                venv_dir = Path(cmd_list[2])
                (venv_dir / "bin").mkdir(parents=True, exist_ok=True)
                (venv_dir / "bin" / "python").write_text("")
                return MagicMock(returncode=0)
            if cmd_list[:3] == ["uv", "pip", "install"]:
                raise subprocess.CalledProcessError(1, cmd_list, stderr="no wheel for mlx on this platform")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch.object(install.subprocess, "run", side_effect=run):
            assert install.do_enable_mlx(assume_yes=True) == 5
        assert "no wheel for mlx" in capsys.readouterr().out


class TestIdempotency:
    def test_working_venv_short_circuits(self, fake_home, apple_silicon, have_uv, capsys):
        venv_dir, venv_py = install._mlx_paths()
        venv_py.parent.mkdir(parents=True)
        venv_py.write_text("")
        run = _fake_run(version="0.5.6")
        with patch.object(install.subprocess, "run", side_effect=run):
            assert install.do_enable_mlx(assume_yes=True) == 0
        assert not any(c[:2] == ["uv", "venv"] for c in run.calls)
        assert "already installed" in capsys.readouterr().out

    def test_broken_venv_offers_reinstall(self, fake_home, apple_silicon, have_uv):
        venv_dir, venv_py = install._mlx_paths()
        venv_py.parent.mkdir(parents=True)
        venv_py.write_text("")

        def run(cmd, **kwargs):
            cmd_list = list(cmd)
            if len(cmd_list) >= 3 and cmd_list[1] == "-c" and "mlx_audio" in cmd_list[2]:
                return MagicMock(returncode=1, stdout="", stderr="ModuleNotFoundError: mlx_audio")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch.object(install.subprocess, "run", side_effect=run), patch("builtins.input", return_value="n"):
            assert install.do_enable_mlx(assume_yes=False) == 1
