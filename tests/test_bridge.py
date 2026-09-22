"""Tests for the loopback HTTP bridge: auth, CORS, queue writes, jobs, stop, marks."""

from __future__ import annotations

import json
import stat
import urllib.error
import urllib.request
import wave
from pathlib import Path

import pytest

import claude_code_tts.bridge as bridge_mod
from claude_code_tts.bridge import (
    JOBS,
    Bridge,
    build_marks,
    concat_wavs,
    ensure_token,
    estimated_marks,
    flush_source,
    split_sentences,
    synthesize_with_marks,
    to_playback_ms,
    write_bridge_message,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tts_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every path the bridge touches at a fresh directory."""
    cfg_dir = tmp_path / ".claude-tts"
    cfg_dir.mkdir()
    queue = cfg_dir / "queue"
    monkeypatch.setattr(bridge_mod, "TTS_CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(bridge_mod, "TTS_QUEUE_DIR", queue)
    monkeypatch.setattr(bridge_mod, "TOKEN_FILE", cfg_dir / "http-token")
    config = {
        "active_persona": "claude-connery",
        "personas": {
            "claude-connery": {"description": "warm", "speed": 1.8, "speed_method": "playback",
                               "max_chars": 60},
        },
    }
    monkeypatch.setattr(bridge_mod, "load_raw_config", lambda: config)
    # Fresh registry per test
    JOBS._jobs.clear()
    JOBS._cancel.clear()
    return cfg_dir


class FakeState:
    def __init__(self) -> None:
        self.state: dict = {"paused": False, "audio_pid": None, "current_message": None}
        self.cleared = 0

    def read(self) -> dict:
        return dict(self.state)

    def clear(self) -> None:
        self.cleared += 1
        self.state["current_message"] = None


@pytest.fixture
def server(tts_home: Path):
    fake = FakeState()
    logs: list[str] = []
    b = Bridge(log_fn=logs.append, read_playback_state=fake.read,
               clear_current_message=fake.clear)
    assert b.start({"bind": "127.0.0.1", "port": 0,
                    "allowed_origins": ["https://ok.example"]})
    b.fake = fake  # type: ignore[attr-defined]
    b.logs = logs  # type: ignore[attr-defined]
    yield b
    b.stop()


def call(server: Bridge, method: str, path: str, body: dict | None = None,
         token: str | None = "auto", origin: str | None = None) -> tuple[int, dict, dict]:
    url = f"http://127.0.0.1:{server.port}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if token == "auto":
        token = ensure_token()
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if origin:
        req.add_header("Origin", origin)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {}), dict(resp.headers)
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, (json.loads(raw) if raw else {}), dict(e.headers)


def make_wav(path: Path, seconds: float, rate: int = 22050) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(seconds * rate))
    return path


# ---------------------------------------------------------------------------
# Token
# ---------------------------------------------------------------------------


def test_token_created_once_with_mode_600(tts_home: Path) -> None:
    t1 = ensure_token()
    t2 = ensure_token()
    assert t1 == t2 and len(t1) > 20
    mode = stat.S_IMODE((tts_home / "http-token").stat().st_mode)
    assert mode == 0o600


# ---------------------------------------------------------------------------
# Auth and CORS
# ---------------------------------------------------------------------------


def test_health_requires_token(server: Bridge) -> None:
    status, body, _ = call(server, "GET", "/health", token=None)
    assert status == 401
    status, body, _ = call(server, "GET", "/health", token="wrong")
    assert status == 401
    status, body, _ = call(server, "GET", "/health")
    assert status == 200 and body["ok"] is True


def test_unknown_origin_is_refused_even_with_token(server: Bridge) -> None:
    status, _, _ = call(server, "GET", "/health", origin="https://evil.example")
    assert status == 403


def test_allowed_origin_is_echoed(server: Bridge) -> None:
    status, _, headers = call(server, "GET", "/health", origin="https://ok.example")
    assert status == 200
    assert headers["Access-Control-Allow-Origin"] == "https://ok.example"


def test_preflight(server: Bridge) -> None:
    url = f"http://127.0.0.1:{server.port}/speak"
    req = urllib.request.Request(url, method="OPTIONS")
    req.add_header("Origin", "https://ok.example")
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 204
        assert "Authorization" in resp.headers["Access-Control-Allow-Headers"]
    req = urllib.request.Request(url, method="OPTIONS")
    req.add_header("Origin", "https://evil.example")
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=5)
    assert exc.value.code == 403


# ---------------------------------------------------------------------------
# Speak, voices, jobs
# ---------------------------------------------------------------------------


def test_voices_lists_personas(server: Bridge) -> None:
    status, body, _ = call(server, "GET", "/voices")
    assert status == 200
    assert body["active"] == "claude-connery"
    assert body["personas"]["claude-connery"]["speed"] == 1.8


def test_speak_writes_queue_file_and_job(server: Bridge, tts_home: Path) -> None:
    status, body, _ = call(server, "POST", "/speak",
                           {"text": "Hello there.", "source": "artifact",
                            "label": "cascade", "want_marks": True})
    assert status == 202
    job_id = body["id"]
    files = list((tts_home / "queue").glob("*.json"))
    assert len(files) == 1
    msg = json.loads(files[0].read_text())
    assert msg["id"] == job_id
    assert msg["session_id"] == "browser"
    assert msg["project"] == "artifact:cascade"
    assert msg["source"] == "artifact"
    assert msg["want_marks"] is True
    assert msg["persona"] == "claude-connery"
    assert msg["speed"] == 1.8

    status, job, _ = call(server, "GET", f"/jobs/{job_id}")
    assert status == 200 and job["state"] == "queued"
    assert "updated_at" not in job


def test_speak_validation(server: Bridge) -> None:
    assert call(server, "POST", "/speak", {"text": ""})[0] == 400
    assert call(server, "POST", "/speak", {"text": "x", "persona": "nope"})[0] == 400
    status, body, _ = call(server, "POST", "/speak", {"text": "y" * 61})
    assert status == 413 and body["max_chars"] == 60
    assert call(server, "GET", "/jobs/nothere")[0] == 404
    assert call(server, "GET", "/nowhere")[0] == 404


def test_job_state_follows_daemon_updates(server: Bridge) -> None:
    _, body, _ = call(server, "POST", "/speak", {"text": "Hi.", "source": "artifact"})
    JOBS.update(body["id"], state="playing", started_at=1.0, offset_ms=0, duration_ms=500)
    _, job, _ = call(server, "GET", f"/jobs/{body['id']}")
    assert job["state"] == "playing" and job["duration_ms"] == 500


# ---------------------------------------------------------------------------
# Stop
# ---------------------------------------------------------------------------


def test_stop_flushes_only_its_source(server: Bridge, tts_home: Path) -> None:
    _, a, _ = call(server, "POST", "/speak", {"text": "one", "source": "artifact"})
    _, b, _ = call(server, "POST", "/speak", {"text": "two", "source": "artifact"})
    # A room's message, written like a hook does, has no source.
    room = tts_home / "queue" / "9.0_room.json"
    room.write_text(json.dumps({"id": "room", "timestamp": 9.0, "text": "room",
                                "session_id": "s", "project": "p"}))
    status, body, _ = call(server, "POST", "/stop", {"source": "artifact"})
    assert status == 200
    assert body == {"flushed": 2, "stopped_current": False}
    remaining = [p.name for p in (tts_home / "queue").glob("*.json")]
    assert remaining == ["9.0_room.json"]
    assert JOBS.state(a["id"]) == "cancelled"
    assert JOBS.state(b["id"]) == "cancelled"


def test_stop_cancels_playing_message_of_same_source(server: Bridge) -> None:
    fake = server.fake  # type: ignore[attr-defined]
    fake.state["audio_pid"] = 4242
    fake.state["current_message"] = {"id": "cur", "source": "artifact", "text": "x"}
    JOBS.create("cur", source="artifact")
    _, body, _ = call(server, "POST", "/stop", {"source": "artifact"})
    assert body["stopped_current"] is True
    # The play loop consumes the cancel request; it is not applied here.
    assert JOBS.take_cancel("cur") is True
    assert fake.cleared == 0


def test_stop_clears_paused_message_of_same_source(server: Bridge) -> None:
    fake = server.fake  # type: ignore[attr-defined]
    fake.state["audio_pid"] = None
    fake.state["current_message"] = {"id": "cur", "source": "artifact", "text": "x"}
    JOBS.create("cur", source="artifact")
    _, body, _ = call(server, "POST", "/stop", {"source": "artifact"})
    assert body["stopped_current"] is True
    assert fake.cleared == 1
    assert JOBS.state("cur") == "cancelled"


def test_stop_leaves_a_room_message_alone(server: Bridge) -> None:
    fake = server.fake  # type: ignore[attr-defined]
    fake.state["audio_pid"] = 4242
    fake.state["current_message"] = {"session_id": "room", "text": "x"}
    _, body, _ = call(server, "POST", "/stop", {"source": "artifact"})
    assert body["stopped_current"] is False
    assert fake.cleared == 0


def test_flush_source_direct(tts_home: Path) -> None:
    write_bridge_message("a", persona="p", persona_config={}, source="x")
    write_bridge_message("b", persona="p", persona_config={}, source="y")
    assert flush_source("x") == 1
    assert len(list((tts_home / "queue").glob("*.json"))) == 1


# ---------------------------------------------------------------------------
# Marks
# ---------------------------------------------------------------------------


def test_split_sentences() -> None:
    assert split_sentences("One. Two!  Three? Four") == ["One.", "Two!", "Three?", "Four"]
    assert split_sentences('He said "stop." Then left.') == ['He said "stop."', "Then left."]
    assert split_sentences("no punctuation at all") == ["no punctuation at all"]
    assert split_sentences("v9.11.2 is out. Yes.") == ["v9.11.2 is out.", "Yes."]


def test_build_marks_words_share_sentence_by_length() -> None:
    marks = build_marks(["ab cd.", "e"], [1.0, 0.5])
    assert marks["sentence_timing"] == "exact"
    assert marks["word_timing"] == "estimated"
    assert [s["start_ms"] for s in marks["sentences"]] == [0, 1000]
    assert marks["sentences"][1]["end_ms"] == 1500
    words = marks["words"]
    assert [w["text"] for w in words] == ["ab", "cd.", "e"]
    assert [w["c"] for w in words] == [0, 3, 7]
    assert [s["c"] for s in marks["sentences"]] == [0, 7]
    assert words[0]["start_ms"] == 0 and words[0]["end_ms"] == 400
    assert words[1]["start_ms"] == 400 and words[1]["end_ms"] == 1000
    assert words[2]["s"] == 1 and words[2]["start_ms"] == 1000


def test_concat_wavs_and_mismatch(tmp_path: Path) -> None:
    a = make_wav(tmp_path / "a.wav", 0.5)
    b = make_wav(tmp_path / "b.wav", 0.25)
    out = tmp_path / "out.wav"
    assert concat_wavs([a, b], out)
    assert abs(bridge_mod.wav_duration_seconds(out) - 0.75) < 0.001
    c = make_wav(tmp_path / "c.wav", 0.1, rate=16000)
    assert concat_wavs([a, c], tmp_path / "bad.wav") is False


def test_synthesize_with_marks_exact_sentences(tmp_path: Path) -> None:
    durations = {"First one.": 1.0, "Second.": 0.5}

    def gen(chunk: str, path: Path) -> bool:
        make_wav(path, durations[chunk])
        return True

    out = tmp_path / "speech.wav"
    marks = synthesize_with_marks("First one. Second.", gen, out, playback_speed=2.0)
    assert marks is not None
    assert out.exists()
    assert abs(bridge_mod.wav_duration_seconds(out) - 1.5) < 0.001
    # Playback at 2x halves the listening time.
    assert [s["end_ms"] for s in marks["sentences"]] == [500, 750]
    assert not list(tmp_path.glob("speech_part*.wav"))


def test_synthesize_with_marks_single_sentence(tmp_path: Path) -> None:
    def gen(chunk: str, path: Path) -> bool:
        make_wav(path, 0.3)
        return True

    out = tmp_path / "speech.wav"
    marks = synthesize_with_marks("Just one", gen, out)
    assert marks is not None and out.exists()
    assert marks["sentences"][0]["end_ms"] == 300


def test_synthesize_with_marks_failure_returns_none(tmp_path: Path) -> None:
    calls: list[str] = []

    def gen(chunk: str, path: Path) -> bool:
        calls.append(chunk)
        if chunk.startswith("Bad"):
            return False
        make_wav(path, 0.2)
        return True

    out = tmp_path / "speech.wav"
    assert synthesize_with_marks("Good. Bad.", gen, out) is None
    assert not list(tmp_path.glob("*.wav"))


def test_estimated_marks_and_playback_ms() -> None:
    marks = estimated_marks("A single run.", 3.0, playback_speed=1.5)
    assert marks["sentence_timing"] == "estimated"
    assert marks["sentences"][0]["end_ms"] == 2000
    assert to_playback_ms(3.0, 1.5, "playback") == 2000
    assert to_playback_ms(3.0, 1.5, "length_scale") == 3000


# ---------------------------------------------------------------------------
# Registry housekeeping
# ---------------------------------------------------------------------------


def test_evict_finished_only_after_ttl() -> None:
    JOBS._jobs.clear()
    JOBS.create("old", source="x")
    JOBS.update("old", state="done")
    JOBS.create("live", source="x")
    JOBS.update("live", state="playing")
    JOBS._jobs["old"]["updated_at"] -= 1000
    JOBS._jobs["live"]["updated_at"] -= 1000
    assert JOBS.evict_finished(ttl=300) == 1
    assert JOBS.get("old") is None
    assert JOBS.get("live") is not None
    JOBS.update("missing", state="done")  # unknown ids are ignored
    assert JOBS.state(None) is None


def test_marks_offsets_follow_the_submitted_text() -> None:
    text = "See #339.   Then  PR #1223 lands."
    sentences = split_sentences(text)
    marks = build_marks(sentences, [1.0, 1.0], text=text)
    # Offsets index the original text, whitespace runs included.
    for w in marks["words"]:
        assert text[w["c"] : w["c"] + len(w["text"])] == w["text"]
    for s in marks["sentences"]:
        assert text[s["c"] : s["c"] + len(s["text"])] == s["text"]
    assert marks["sentences"][1]["c"] == text.index("Then")
