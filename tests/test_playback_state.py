"""playback.json is written by the play loop, the mic thread, the bridge and the CLI.

Through 9.33.1 the reader took the first 10 000 bytes only, so a long interrupted message
made the file unreadable and the next write started from the defaults: a pause in the middle
of a long response never resumed. The writer used one fixed temp name for every writer, so
two threads writing at once corrupted the file (375 failures in 1200 concurrent writes).
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import claude_code_tts.daemon as d
from claude_code_tts.config import atomic_write_json


def _home() -> Path:
    home = Path.home() / ".claude-tts"
    home.mkdir(parents=True, exist_ok=True)
    return home


def test_long_current_message_survives_a_following_write():
    _home()
    d.write_playback_state(paused=False, current_message={"text": "x" * 12000, "session_id": "s"})
    d.write_playback_state(paused=True)
    state = d.read_playback_state()
    assert state["paused"] is True
    assert state["current_message"]["text"] == "x" * 12000


def test_concurrent_writers_never_fail_and_leave_valid_json():
    _home()
    errors: list[str] = []

    def writer(k: int) -> None:
        for i in range(150):
            try:
                d.write_playback_state(audio_pid=k * 1000 + i)
            except Exception as e:  # noqa: BLE001
                errors.append(type(e).__name__)

    threads = [threading.Thread(target=writer, args=(k,)) for k in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    json.loads(d.PLAYBACK_STATE_FILE.read_text())
    leftovers = [p for p in d.PLAYBACK_STATE_FILE.parent.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


def test_atomic_write_json_uses_its_own_temp_name(tmp_path):
    target = tmp_path / "state.json"
    seen: set[str] = set()
    original_replace = Path.replace

    def spy(self, dst):
        seen.add(self.name)
        return original_replace(self, dst)

    Path.replace = spy  # type: ignore[method-assign]
    try:
        atomic_write_json(target, {"a": 1})
        atomic_write_json(target, {"a": 2})
    finally:
        Path.replace = original_replace  # type: ignore[method-assign]
    assert len(seen) == 2, "two writes must not share one temp file"
    assert json.loads(target.read_text()) == {"a": 2}
    assert list(tmp_path.iterdir()) == [target]


def test_unreadable_state_file_falls_back_to_defaults():
    _home()
    d.PLAYBACK_STATE_FILE.write_text("{not json")
    assert d.read_playback_state() == {"paused": False, "audio_pid": None, "current_message": None}
