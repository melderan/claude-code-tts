"""One queue writer: msgqueue.write_message, with an additive "v" and every old field kept.

Step 2 of docs/redesign-10.md, second half. Three writers (the hook, the bridge, the daemon's
control path) each built their own dict and did their own tmp-then-rename. They now build the
dict and hand it to msgqueue.write_message. The on-disk shape is the contract: a sandbox's
hooks and the Mac's daemon can be different versions in either direction, permanently, so
the golden key sets below are what 9.36.x wrote, and the only change allowed is "v".
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pytest

import claude_code_tts.audio as audio_mod
import claude_code_tts.bridge as bridge_mod
import claude_code_tts.daemon as daemon_mod
import claude_code_tts.msgqueue as mq
from claude_code_tts.config import TTSConfig

# What the one writer adds on top of the 9.36.x shapes, and nothing else.
ADDED: frozenset[str] = frozenset({"v"})

HOOK_KEYS = frozenset(
    {
        "id", "timestamp", "session_id", "project", "text", "persona", "speed", "speed_method",
        "voice_kokoro", "voice_kokoro_blend", "voice_sherpa", "speaker_sherpa",
        "voice_mlx", "speaker_mlx", "lang_mlx", "pitch_filter",
    }
)
BRIDGE_KEYS = frozenset(
    {
        "id", "timestamp", "session_id", "project", "text", "persona", "speed", "speed_method",
        "source", "want_marks",
    }
)
CONTROL_KEYS = frozenset({"id", "timestamp", "type", "session_id", "text"})

NAME_6DP = re.compile(r"^(\d+\.\d{6})_([0-9a-f]{16})\.json$")


@pytest.fixture
def queue(tmp_path, monkeypatch) -> Path:
    q = tmp_path / "queue"
    monkeypatch.setattr(mq, "QUEUE_DIR", q)
    bridge_mod.JOBS._jobs.clear()
    bridge_mod.JOBS._cancel.clear()
    return q


@pytest.fixture
def renames(monkeypatch) -> list[tuple[str, str]]:
    """Every Path.rename, so a test can see the write went .tmp -> .json."""
    seen: list[tuple[str, str]] = []
    real = Path.rename

    def spy(self, target):
        seen.append((Path(self).name, Path(target).name))
        return real(self, target)

    monkeypatch.setattr(Path, "rename", spy)
    return seen


def _cfg() -> TTSConfig:
    return TTSConfig(
        session_id="-Users-x-proj", project_name="proj", active_persona="claude-connery",
        speed=1.8, speed_method="", voice_kokoro="af_bella", voice_kokoro_blend="a:0.5,b:0.5",
        voice_sherpa="vctk", speaker_sherpa=7, voice_mlx="m", speaker_mlx="af_heart",
        lang_mlx="a", pitch_filter="asetrate=1",
    )


def _read(path: Path) -> dict:
    return json.loads(path.read_text())


def _check_file(path: Path, msg: dict, renames: list[tuple[str, str]]) -> None:
    m = NAME_6DP.match(path.name)
    assert m, path.name
    assert m.group(1) == f"{msg['timestamp']:.6f}"
    assert m.group(2) == msg["id"]
    assert isinstance(msg["timestamp"], float)
    assert (path.with_suffix(".tmp").name, path.name) in renames
    assert not list(path.parent.glob("*.tmp"))


# --- Golden: the exact key set and values each writer emits ---


class TestHookWriterGolden:
    def test_without_engine(self, queue, renames):
        path = audio_mod.write_queue_message("hello", _cfg())
        msg = _read(path)
        assert set(msg) == HOOK_KEYS | ADDED
        _check_file(path, msg, renames)
        assert {k: msg[k] for k in HOOK_KEYS - {"id", "timestamp"}} == {
            "session_id": "-Users-x-proj", "project": "proj", "text": "hello",
            "persona": "claude-connery", "speed": 1.8, "speed_method": "playback",
            "voice_kokoro": "af_bella", "voice_kokoro_blend": "a:0.5,b:0.5",
            "voice_sherpa": "vctk", "speaker_sherpa": 7, "voice_mlx": "m",
            "speaker_mlx": "af_heart", "lang_mlx": "a", "pitch_filter": "asetrate=1",
        }

    def test_with_engine(self, queue, renames):
        path = audio_mod.write_queue_message("hello", _cfg(), engine="mlx")
        msg = _read(path)
        assert set(msg) == HOOK_KEYS | {"engine"} | ADDED
        assert msg["engine"] == "mlx"
        _check_file(path, msg, renames)


class TestBridgeWriterGolden:
    def test_without_lane(self, queue, renames):
        ret = bridge_mod.write_bridge_message(
            "hi", persona="p", persona_config={"speed": 1.5}, source="artifact", label="doc"
        )
        files = list(queue.glob("*.json"))
        assert len(files) == 1
        msg = _read(files[0])
        assert set(msg) == BRIDGE_KEYS | ADDED
        assert ret == msg  # the caller gets what is on disk
        _check_file(files[0], msg, renames)
        assert {k: msg[k] for k in BRIDGE_KEYS - {"id", "timestamp"}} == {
            "session_id": "browser", "project": "artifact:doc", "text": "hi", "persona": "p",
            "speed": 1.5, "speed_method": "playback", "source": "artifact", "want_marks": False,
        }
        job = bridge_mod.JOBS.get(msg["id"])
        assert job is not None and job["state"] == "queued" and job["project"] == "artifact:doc"

    def test_with_lane(self, queue, renames):
        ret = bridge_mod.write_bridge_message(
            "hi", persona="p", persona_config={}, source="artifact", want_marks=True,
            lane="background",
        )
        msg = _read(next(queue.glob("*.json")))
        assert set(msg) == BRIDGE_KEYS | {"lane"} | ADDED
        assert ret == msg
        assert msg["lane"] == "background" and msg["want_marks"] is True
        assert msg["project"] == "artifact" and msg["speed"] == 2.0
        assert bridge_mod.JOBS.get(msg["id"])["lane"] == "background"  # type: ignore[index]


class TestControlWriterGolden:
    def test_without_actions(self, queue, renames):
        path = mq.write_control_message("hello")
        msg = _read(path)
        assert set(msg) == CONTROL_KEYS | ADDED
        assert msg["type"] == "control" and msg["session_id"] == "system" and msg["text"] == "hello"
        # 9.36.x named control files with the raw float; now 6 decimals like the rest.
        _check_file(path, msg, renames)

    def test_with_actions(self, queue, renames):
        logged: list[tuple[str, str]] = []
        path = mq.write_control_message(
            "", pre_action="drain", post_action="restart", log=lambda m, lv: logged.append((m, lv))
        )
        msg = _read(path)
        assert set(msg) == CONTROL_KEYS | {"pre_action", "post_action"} | ADDED
        assert msg["pre_action"] == "drain" and msg["post_action"] == "restart"
        assert logged == [(f"Control message written: {path.name}", "INFO")]
        _check_file(path, msg, renames)

    def test_daemon_wrapper_is_the_same_writer(self, queue, monkeypatch):
        monkeypatch.setattr(daemon_mod, "log", lambda *a, **k: None)
        msg = _read(daemon_mod.write_control_message(post_action="stop"))
        assert set(msg) == CONTROL_KEYS | {"post_action"} | ADDED


# --- Fixtures: 9.36.x shapes (no "v") and a future shape read exactly as before ---

HOOK_9_36 = {
    "id": "0123456789abcdef", "timestamp": 1759200000.123456, "session_id": "-Users-x-proj",
    "project": "proj", "text": "Hello there.", "persona": "claude-connery", "speed": 1.8,
    "speed_method": "playback", "voice_kokoro": "", "voice_kokoro_blend": "",
    "voice_sherpa": "", "speaker_sherpa": -1, "voice_mlx": "", "speaker_mlx": "", "lang_mlx": "",
    "pitch_filter": "",
}
BRIDGE_9_36 = {
    "id": "fedcba9876543210", "timestamp": 1759200001.5, "session_id": "browser",
    "project": "artifact:doc", "text": "A block.", "persona": "claude-connery", "speed": 1.5,
    "speed_method": "length_scale", "source": "artifact", "want_marks": True, "lane": "background",
}
CONTROL_9_36 = {
    "id": "00000000ffffffff", "timestamp": 1759200002.25, "type": "control",
    "session_id": "system", "text": "Restarting.", "pre_action": "drain",
    "post_action": "reload_config",
}


def _shapes(base: dict) -> list[dict]:
    """The 9.36.x message, the same with "v": 1, and a later writer's "v": 2 with a field we do not know."""
    return [dict(base), {**base, "v": 1}, {**base, "v": 2, "voice": {"engine": "piper"}}]


def _prepared(msg: dict, monkeypatch) -> tuple:
    monkeypatch.setattr(daemon_mod, "get_persona_config", lambda _n: {"voice": "en_US-lessac-medium"})
    p = daemon_mod.prepare_message({**msg, "_file": Path("/nowhere/x.json")}, {})
    return (p.text, p.persona, p.speed, p.speed_method, p.job_id, p.want_marks, p.audio_file.name)


class TestOldAndNewShapesReadTheSame:
    def test_hook_message(self, monkeypatch):
        want = (
            "Hello there.", "claude-connery", 1.8, "playback", None, False,
            "tts_queue_-Users-x-proj_0123456789ab.wav",
        )
        for msg in _shapes(HOOK_9_36):
            assert _prepared(msg, monkeypatch) == want, msg

    def test_bridge_message(self, monkeypatch):
        want = (
            "A block.", "claude-connery", 1.5, "length_scale", "fedcba9876543210", True,
            "tts_queue_browser_fedcba987654.wav",
        )
        for msg in _shapes(BRIDGE_9_36):
            assert _prepared(msg, monkeypatch) == want, msg

    def test_next_speakable_skips_current_and_stops_at_control(self):
        for hook, bridge, control in zip(_shapes(HOOK_9_36), _shapes(BRIDGE_9_36), _shapes(CONTROL_9_36), strict=True):
            cur, nxt, ctl = (
                dict(m, _file=Path(f"/q/{i}.json")) for i, m in enumerate((hook, bridge, control))
            )
            assert mq.next_speakable([cur, nxt], cur["_file"]) is nxt
            assert mq.next_speakable([cur, ctl, nxt], cur["_file"]) is None
            assert daemon_mod.next_speakable([cur, nxt], cur["_file"]) is nxt

    def test_control_message(self, monkeypatch):
        spoken: list[tuple[str, str]] = []
        logged: list[str] = []
        monkeypatch.setattr(
            daemon_mod, "speak_announcement", lambda t, p="claude-prime": spoken.append((t, p))
        )
        monkeypatch.setattr(daemon_mod, "log", lambda m, *a, **k: logged.append(m))
        for msg in _shapes(CONTROL_9_36):
            spoken.clear()
            logged.clear()
            daemon_mod.handle_control_message(msg)
            assert spoken == [("Restarting.", "claude-prime")]
            assert logged == [
                "Control message: pre=drain, post=reload_config, text='Restarting.'",
                "Control: drain (no-op in serial mode)",
                "Control: reloading config",
            ]

    def test_scan_keeps_a_future_message(self, queue):
        queue.mkdir(parents=True)
        for i, msg in enumerate(_shapes(HOOK_9_36)):
            (queue / f"{1759200000 + i}.000000_{i:016x}.json").write_text(json.dumps(msg))
        found = sorted(mq.scan(), key=lambda m: m.get("v", 0))  # same timestamp: glob order
        assert [m.get("v") for m in found] == [None, 1, 2]
        assert len(list(queue.glob("*.json"))) == 3
        assert found[2]["voice"] == {"engine": "piper"}


def test_written_messages_read_like_their_9_36_shapes(queue, monkeypatch):
    """What the hook writer emits resolves exactly as the hand-written 9.36.x fixture does."""
    cfg = TTSConfig(
        session_id="-Users-x-proj", project_name="proj", active_persona="claude-connery", speed=1.8
    )
    written = _read(audio_mod.write_queue_message("Hello there.", cfg))
    old = {**HOOK_9_36, "id": written["id"]}
    assert _prepared(written, monkeypatch) == _prepared(old, monkeypatch)
    assert {k: v for k, v in written.items() if k not in ADDED | {"timestamp"}} == {
        k: v for k, v in old.items() if k != "timestamp"
    }
    assert time.time() - written["timestamp"] < 60
