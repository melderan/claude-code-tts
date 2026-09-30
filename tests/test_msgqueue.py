"""msgqueue.py owns the queue directory; daemon, audio, bridge and cli go through it.

The second cut of docs/redesign-10.md. Same trap as state.py: a path constant imported into
another module is a copy, so sixteen test sites that patched TTS_QUEUE_DIR on daemon, audio
or bridge would have redirected nothing once the functions moved. QUEUE_DIR lives in one
module and nowhere else carries a copy, so a patch aimed at the old home fails loudly.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

import claude_code_tts.audio as audio_mod
import claude_code_tts.bridge as bridge_mod
import claude_code_tts.cli as cli_mod
import claude_code_tts.config as config_mod
import claude_code_tts.daemon as daemon_mod
import claude_code_tts.msgqueue as mq

SRC = Path(mq.__file__).parent


@pytest.mark.parametrize("mod", [daemon_mod, audio_mod, bridge_mod, cli_mod, config_mod])
def test_no_other_module_carries_the_queue_directory(mod) -> None:
    for name in ("QUEUE_DIR", "TTS_QUEUE_DIR"):
        assert not hasattr(mod, name), f"{mod.__name__}.{name} would be a copy a patch cannot reach"
    assert mq.QUEUE_DIR.parent == mq.TTS_CONFIG_DIR


@pytest.mark.parametrize("name", ["PauseLedger", "play_order", "next_speakable"])
def test_daemon_reexports_the_same_object(name: str) -> None:
    assert getattr(daemon_mod, name) is getattr(mq, name)


def test_daemon_wrappers_bind_the_daemon_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The queue module has no logger; the daemon's wrappers hand it theirs."""
    monkeypatch.setattr(mq, "QUEUE_DIR", tmp_path / "queue")
    monkeypatch.setattr(daemon_mod, "LOG_FILE", tmp_path / "daemon.log")
    monkeypatch.setattr(daemon_mod, "_daemon_mode", True)
    path = daemon_mod.write_control_message("hello", post_action="reload_config")
    assert path.parent == tmp_path / "queue"
    assert daemon_mod.get_queue_messages()[0]["_file"] == path
    (tmp_path / "queue" / "0.000001_old.json").write_text('{"timestamp": 1.0, "text": "stale"}')
    assert daemon_mod.cleanup_old_messages(60) == 1
    log = (tmp_path / "daemon.log").read_text()
    assert "Control message written" in log and "Removed stale message" in log


def test_scan_deletes_what_it_cannot_parse_and_says_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mq, "QUEUE_DIR", tmp_path)
    bad = tmp_path / "1.0_bad.json"
    bad.write_text("{not json")
    (tmp_path / "2.0_ok.json").write_text('{"timestamp": 2.0, "text": "fine"}')
    said: list[str] = []
    msgs = mq.scan(log=lambda m, level: said.append(level))
    assert [m["text"] for m in msgs] == ["fine"]
    assert not bad.exists() and said == ["WARN"]


def test_remove_source_returns_the_messages_it_removed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mq, "QUEUE_DIR", tmp_path)
    (tmp_path / "1.0_a.json").write_text('{"id": "a", "source": "page", "text": "x"}')
    (tmp_path / "2.0_b.json").write_text('{"id": "b", "source": "other", "text": "y"}')
    removed = mq.remove_source("page")
    assert [m["id"] for m in removed] == ["a"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["2.0_b.json"]


def test_depth_counts_json_files_only_and_survives_a_missing_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mq, "QUEUE_DIR", tmp_path / "nowhere")
    assert mq.depth() == 0
    monkeypatch.setattr(mq, "QUEUE_DIR", tmp_path)
    (tmp_path / f"{time.time():.6f}_a.json").write_text("{}")
    (tmp_path / "half.tmp").write_text("{")
    assert mq.depth() == 1


def test_msgqueue_imports_no_engine_or_daemon_module(tmp_path: Path) -> None:
    """The hook writer loads msgqueue; it must stay as cheap as config."""
    home = tmp_path / "claude-tts-test-home-probe"
    home.mkdir()
    probe = (
        "import sys, claude_code_tts.msgqueue; "
        "print(sorted(m for m in sys.modules if m.startswith('claude_code_tts')))"
    )
    r = subprocess.run(
        [sys.executable, "-c", probe],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin", "PYTHONPATH": str(SRC.parent)},
        capture_output=True,
        text=True,
        check=True,
    )
    loaded = set(eval(r.stdout))  # noqa: S307  (our own repr of a list of module names)
    heavy = {m for m in loaded if m.split(".")[-1] in ("daemon", "audio", "bridge", "handy", "install", "cli")}
    assert heavy == set(), f"msgqueue pulled in {sorted(heavy)}"


def test_no_package_module_shadows_the_standard_library() -> None:
    """queue.py would have shadowed stdlib queue for any script run from the package directory."""
    clashes = sorted(p.stem for p in SRC.glob("*.py") if p.stem in sys.stdlib_module_names)
    assert clashes == [], clashes
