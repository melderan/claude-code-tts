"""scripts/host-python.sh picks the interpreter host-side recipes run on.

macOS ships python3 3.9 and the package needs 3.10. The daemon's own interpreter (behind the
installed claude-tts tool) wins, so recipes and daemon share one Python; before the tool exists
the newest 3.10+ on the PATH is used; python3 is the last resort.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "scripts" / "host-python.sh"


def run(path_dir: Path) -> str:
    """Run the resolver with a PATH of the fake bin plus only the tools the script itself needs,
    so the machine's own interpreters (this room has a /usr/bin/python3.14) cannot leak in."""
    tools = path_dir.parent / "tools"
    tools.mkdir(exist_ok=True)
    for name in ("sh", "sed"):
        real = shutil.which(name)
        assert real, name
        link = tools / name
        if not link.exists():
            link.symlink_to(real)
    env = dict(os.environ, PATH=f"{path_dir}:{tools}")
    return subprocess.run([str(SCRIPT)], env=env, capture_output=True, text=True, check=True).stdout.strip()


def fake_exe(path: Path, body: str = "#!/bin/sh\n") -> Path:
    path.write_text(body)
    path.chmod(0o755)
    return path


def test_installed_tool_interpreter_wins(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (tmp_path / "toolenv").mkdir()
    py = fake_exe(tmp_path / "toolenv" / "python3")
    fake_exe(bin_dir / "claude-tts", f"#!{py}\n")
    fake_exe(bin_dir / "python3.14")
    assert run(bin_dir) == str(py)


def test_without_the_tool_the_newest_supported_python_is_picked(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_exe(bin_dir / "python3.10")
    fake_exe(bin_dir / "python3.12")
    fake_exe(bin_dir / "python3.9")
    assert run(bin_dir) == str(bin_dir / "python3.12")


def test_a_tool_whose_shebang_is_not_python_is_ignored(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_exe(bin_dir / "claude-tts", "#!/bin/sh\n")
    fake_exe(bin_dir / "python3.11")
    assert run(bin_dir) == str(bin_dir / "python3.11")


def test_python3_is_the_last_resort(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_exe(bin_dir / "python3")
    assert run(bin_dir) == str(bin_dir / "python3")
