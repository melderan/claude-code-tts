"""Loopback HTTP bridge: lets a browser page hand text to the queue daemon.

The daemon has always been fed by files: hooks write JSON into ~/.claude-tts/queue
and the daemon loop plays them in order. A page running in a browser cannot write
files, so this module gives it a small HTTP surface on 127.0.0.1 that does the
same thing a hook does (writes a queue file) and reads back a job registry the
daemon loop keeps as each message moves from queued to playing to done.

It is off by default. It runs as a thread inside the daemon process, next to the
mic watcher and the Handy analyzer; the daemon loop itself never touches a socket.
Every request needs a bearer token from ~/.claude-tts/http-token, even on loopback,
so a random tab cannot make the house voice speak.

Timing: a page that highlights words as they are spoken asks for marks. The daemon
synthesizes the text sentence by sentence, measures each WAV, and concatenates
them, so sentence boundaries are exact. Word positions inside a sentence are
proportional to character count and are labelled "estimated". That works for
every backend (Piper, Kokoro, sherpa) without needing phoneme timing from any.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
import wave
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from claude_code_tts import __version__
from claude_code_tts.config import TTS_CONFIG_DIR, TTS_QUEUE_DIR, load_raw_config

TOKEN_FILE = TTS_CONFIG_DIR / "http-token"
DEFAULT_PORT = 7457
DEFAULT_BIND = "127.0.0.1"
JOB_TTL_SECONDS = 300
MAX_BODY_BYTES = 256 * 1024
DEFAULT_MAX_CHARS = 10000

JOB_STATES = ("queued", "synthesizing", "playing", "paused", "done", "cancelled", "failed")


# --- Config and token ---


def get_http_config() -> dict:
    """The "http" block of config.json with defaults filled in."""
    defaults: dict[str, Any] = {
        "enabled": False,
        "port": DEFAULT_PORT,
        "bind": DEFAULT_BIND,
        "allowed_origins": [],
    }
    return {**defaults, **load_raw_config().get("http", {})}


def read_token() -> str | None:
    """Return the bearer token if one exists."""
    try:
        token = TOKEN_FILE.read_text().strip()
    except OSError:
        return None
    return token or None


def ensure_token() -> str:
    """Return the bearer token, creating it (mode 600) on first use."""
    token = read_token()
    if token:
        return token
    TTS_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    fd = os.open(str(TOKEN_FILE), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token + "\n")
    os.chmod(TOKEN_FILE, 0o600)
    return token


# --- Job registry (shared between the daemon loop and the HTTP thread) ---


class JobRegistry:
    """Per-message state the daemon loop writes and the HTTP thread reads.

    Only messages that arrived through the bridge (they carry a "source")
    are tracked. Finished jobs stay readable for JOB_TTL_SECONDS so a page
    that polls slowly still sees the final state.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, dict] = {}
        self._cancel: set[str] = set()

    def create(self, job_id: str, **fields: Any) -> dict:
        job = {"id": job_id, "state": "queued", "updated_at": time.time(), **fields}
        with self._lock:
            self._jobs[job_id] = job
        return dict(job)

    def update(self, job_id: str | None, **fields: Any) -> None:
        if not job_id:
            return
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.update(fields)
            job["updated_at"] = time.time()

    def get(self, job_id: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return dict(job) if job else None

    def state(self, job_id: str | None) -> str | None:
        if not job_id:
            return None
        job = self.get(job_id)
        return job["state"] if job else None

    def request_cancel(self, job_id: str) -> None:
        with self._lock:
            self._cancel.add(job_id)

    def take_cancel(self, job_id: str | None) -> bool:
        """True once if a cancel was requested for this job; clears the request."""
        if not job_id:
            return False
        with self._lock:
            if job_id in self._cancel:
                self._cancel.discard(job_id)
                return True
            return False

    def evict_finished(self, ttl: float = JOB_TTL_SECONDS) -> int:
        cutoff = time.time() - ttl
        removed = 0
        with self._lock:
            for job_id in list(self._jobs):
                job = self._jobs[job_id]
                if job["state"] in ("done", "cancelled", "failed") and job["updated_at"] < cutoff:
                    del self._jobs[job_id]
                    self._cancel.discard(job_id)
                    removed += 1
        return removed


JOBS = JobRegistry()


# --- Queue side ---


def write_bridge_message(
    text: str,
    *,
    persona: str,
    persona_config: dict,
    source: str,
    label: str = "",
    want_marks: bool = False,
) -> dict:
    """Write a queue file the way a hook does, tagged with its source.

    session_id is "browser" and project is "<source>:<label>", so the daemon's
    speaker-transition chime fires when a room and the page interleave.
    """
    TTS_QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = f"{time.time():.6f}"
    msg_id = secrets.token_hex(8)
    project = f"{source}:{label}" if label else source
    message = {
        "id": msg_id,
        "timestamp": float(timestamp),
        "session_id": "browser",
        "project": project,
        "text": text,
        "persona": persona,
        "speed": persona_config.get("speed", 2.0),
        "speed_method": persona_config.get("speed_method", "playback"),
        "source": source,
        "want_marks": bool(want_marks),
    }
    queue_file = TTS_QUEUE_DIR / f"{timestamp}_{msg_id}.json"
    tmp_file = queue_file.with_suffix(".tmp")
    tmp_file.write_text(json.dumps(message))
    tmp_file.rename(queue_file)
    JOBS.create(msg_id, source=source, project=project, persona=persona)
    return message


def flush_source(source: str) -> int:
    """Delete queued (not yet playing) messages from one source. Returns the count."""
    removed = 0
    if not TTS_QUEUE_DIR.exists():
        return 0
    for f in TTS_QUEUE_DIR.glob("*.json"):
        try:
            msg = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if msg.get("source") == source:
            f.unlink(missing_ok=True)
            JOBS.update(msg.get("id"), state="cancelled", position_ms=0)
            removed += 1
    return removed


# --- Marks: sentence-exact, word-estimated timing ---

# Terminal punctuation, optionally followed by one closing quote or bracket, then
# whitespace. Two fixed-width lookbehinds because Python's re allows no other kind.
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+|(?<=[.!?…][\"')\]])\s+")


def split_sentences(text: str) -> list[str]:
    """Split prose into sentences on terminal punctuation followed by whitespace."""
    parts = [p.strip() for p in _SENTENCE_END.split(text.strip())]
    parts = [p for p in parts if p]
    return parts or [text.strip()]


def build_marks(
    sentences: list[str],
    durations_s: list[float],
    *,
    sentence_timing: str = "exact",
) -> dict:
    """Turn per-sentence durations (already in playback time) into marks.

    Words inside a sentence get a share of its duration proportional to their
    character count, so a long word is highlighted longer than a short one.
    """
    out_sentences: list[dict] = []
    out_words: list[dict] = []
    cursor = 0.0
    for i, (sentence, dur) in enumerate(zip(sentences, durations_s, strict=True)):
        start = cursor
        end = cursor + dur
        out_sentences.append(
            {"i": i, "start_ms": round(start * 1000), "end_ms": round(end * 1000), "text": sentence}
        )
        words = sentence.split()
        total_chars = sum(len(w) for w in words) or 1
        w_cursor = start
        for w in words:
            w_dur = dur * len(w) / total_chars
            out_words.append(
                {
                    "s": i,
                    "start_ms": round(w_cursor * 1000),
                    "end_ms": round((w_cursor + w_dur) * 1000),
                    "text": w,
                }
            )
            w_cursor += w_dur
        cursor = end
    return {
        "sentence_timing": sentence_timing,
        "word_timing": "estimated",
        "sentences": out_sentences,
        "words": out_words,
    }


def wav_duration_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        rate = w.getframerate()
        return w.getnframes() / rate if rate else 0.0


def concat_wavs(parts: list[Path], out: Path) -> bool:
    """Concatenate WAV files with identical parameters into one. False on mismatch."""
    if not parts:
        return False
    params = None
    frames: list[bytes] = []
    for p in parts:
        with wave.open(str(p), "rb") as w:
            this = (w.getnchannels(), w.getsampwidth(), w.getframerate())
            if params is None:
                params = this
            elif this != params:
                return False
            frames.append(w.readframes(w.getnframes()))
    assert params is not None
    with wave.open(str(out), "wb") as w:
        w.setnchannels(params[0])
        w.setsampwidth(params[1])
        w.setframerate(params[2])
        for chunk in frames:
            w.writeframes(chunk)
    return True


def synthesize_with_marks(
    text: str,
    generate: Callable[[str, Path], bool],
    output_file: Path,
    *,
    playback_speed: float = 1.0,
) -> dict | None:
    """Synthesize text one sentence at a time and return marks in playback time.

    `generate(sentence, path)` is the daemon's own synthesis call. playback_speed
    is the afplay rate (1.0 when speed is baked into the audio by length_scale),
    used to convert WAV seconds into the seconds the listener actually hears.
    Returns None if any sentence fails or the parts cannot be joined; the caller
    then falls back to whole-text synthesis.
    """
    sentences = split_sentences(text)
    parts: list[Path] = []
    durations: list[float] = []
    factor = playback_speed if playback_speed > 0 else 1.0
    try:
        for i, sentence in enumerate(sentences):
            part = output_file.with_name(f"{output_file.stem}_part{i}.wav")
            if not generate(sentence, part) or not part.exists():
                return None
            parts.append(part)
            durations.append(wav_duration_seconds(part) / factor)
        if len(parts) == 1:
            parts[0].replace(output_file)
            parts = []
        elif not concat_wavs(parts, output_file):
            return None
        return build_marks(sentences, durations)
    finally:
        for p in parts:
            p.unlink(missing_ok=True)


def estimated_marks(text: str, wav_seconds: float, *, playback_speed: float = 1.0) -> dict:
    """Marks for a WAV synthesized in one piece: one sentence span, words estimated."""
    factor = playback_speed if playback_speed > 0 else 1.0
    return build_marks([text.strip()], [wav_seconds / factor], sentence_timing="estimated")


def to_playback_ms(wav_seconds: float, speed: float, speed_method: str) -> int:
    """Convert a position in WAV seconds to milliseconds of listening time."""
    factor = speed if (speed_method == "playback" and speed > 0) else 1.0
    return round(wav_seconds / factor * 1000)


# --- HTTP surface ---


class BridgeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        *,
        token: str,
        allowed_origins: list[str],
        log_fn: Callable[[str], None],
        read_playback_state: Callable[[], dict],
        clear_current_message: Callable[[], None],
    ) -> None:
        super().__init__(address, BridgeHandler)
        self.token = token
        self.allowed_origins = set(allowed_origins)
        self.log_fn = log_fn
        self.read_playback_state = read_playback_state
        self.clear_current_message = clear_current_message


class BridgeHandler(BaseHTTPRequestHandler):
    server: BridgeHTTPServer
    protocol_version = "HTTP/1.1"

    # -- plumbing --

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        self.server.log_fn(f"http {self.address_string()} {format % args}")

    def _origin_allowed(self) -> str | None:
        origin = self.headers.get("Origin")
        if origin and origin in self.server.allowed_origins:
            return origin
        return None

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        origin = self._origin_allowed()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return False
        return secrets.compare_digest(header[len("Bearer ") :].strip(), self.server.token)

    def _gate(self) -> bool:
        """Origin and token checks shared by every non-preflight request."""
        origin = self.headers.get("Origin")
        if origin and not self._origin_allowed():
            self._send_json(403, {"error": "origin not allowed", "origin": origin})
            return False
        if not self._authorized():
            self._send_json(401, {"error": "missing or invalid bearer token"})
            return False
        return True

    def _read_body(self) -> dict | None:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            self._send_json(413, {"error": "body too large"})
            return None
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._send_json(400, {"error": "body is not JSON"})
            return None
        if not isinstance(body, dict):
            self._send_json(400, {"error": "body must be a JSON object"})
            return None
        return body

    # -- verbs --

    def do_OPTIONS(self) -> None:  # noqa: N802
        origin = self._origin_allowed()
        if not origin:
            self._send_json(403, {"error": "origin not allowed"})
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Vary", "Origin")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        if not self._gate():
            return
        path = self.path.split("?", 1)[0]
        if path == "/health":
            self._send_json(200, {"ok": True, "version": __version__})
        elif path == "/voices":
            config = load_raw_config()
            personas = {
                name: {"description": p.get("description", ""), "speed": p.get("speed", 2.0)}
                for name, p in config.get("personas", {}).items()
            }
            self._send_json(
                200, {"active": config.get("active_persona", "claude-prime"), "personas": personas}
            )
        elif path.startswith("/jobs/"):
            job = JOBS.get(path[len("/jobs/") :])
            if job is None:
                self._send_json(404, {"error": "unknown job"})
            else:
                job.pop("updated_at", None)
                self._send_json(200, job)
        else:
            self._send_json(404, {"error": "no such route"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._gate():
            return
        path = self.path.split("?", 1)[0]
        body = self._read_body()
        if body is None:
            return
        if path == "/speak":
            self._speak(body)
        elif path == "/stop":
            self._stop(body)
        else:
            self._send_json(404, {"error": "no such route"})

    def _speak(self, body: dict) -> None:
        text = str(body.get("text", "")).strip()
        if not text:
            self._send_json(400, {"error": "text is required"})
            return
        config = load_raw_config()
        personas = config.get("personas", {})
        persona = str(body.get("persona") or config.get("active_persona", "claude-prime"))
        if persona not in personas:
            self._send_json(400, {"error": "unknown persona", "persona": persona})
            return
        persona_config = personas[persona]
        max_chars = int(persona_config.get("max_chars", DEFAULT_MAX_CHARS))
        if len(text) > max_chars:
            self._send_json(413, {"error": "text too long", "max_chars": max_chars})
            return
        source = re.sub(r"[^A-Za-z0-9_.-]", "", str(body.get("source") or "browser"))[:32]
        label = str(body.get("label", ""))[:80]
        msg = write_bridge_message(
            text,
            persona=persona,
            persona_config=persona_config,
            source=source or "browser",
            label=label,
            want_marks=bool(body.get("want_marks", False)),
        )
        self._send_json(202, {"id": msg["id"], "state": "queued"})

    def _stop(self, body: dict) -> None:
        source = str(body.get("source") or "browser")
        flushed = flush_source(source)
        stopped_current = False
        state = self.server.read_playback_state()
        current = state.get("current_message") or {}
        if current.get("source") == source and current.get("id"):
            if state.get("audio_pid"):
                # Playing right now: the play loop sees the cancel and kills the player.
                JOBS.request_cancel(current["id"])
            else:
                # Paused between plays: nothing to kill, just forget the replay.
                self.server.clear_current_message()
                JOBS.update(current["id"], state="cancelled")
            stopped_current = True
        self._send_json(200, {"flushed": flushed, "stopped_current": stopped_current})


# --- Lifecycle ---


class Bridge:
    """Owns the listener thread. start() returns False if the port is taken."""

    def __init__(
        self,
        *,
        log_fn: Callable[[str], None],
        read_playback_state: Callable[[], dict],
        clear_current_message: Callable[[], None],
    ) -> None:
        self._log = log_fn
        self._read_state = read_playback_state
        self._clear_current = clear_current_message
        self._server: BridgeHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int | None:
        return self._server.server_address[1] if self._server else None

    def start(self, http_config: dict | None = None) -> bool:
        cfg = http_config or get_http_config()
        bind = str(cfg.get("bind", DEFAULT_BIND))
        port = int(cfg.get("port", DEFAULT_PORT))
        token = ensure_token()
        try:
            self._server = BridgeHTTPServer(
                (bind, port),
                token=token,
                allowed_origins=[str(o) for o in cfg.get("allowed_origins", [])],
                log_fn=self._log,
                read_playback_state=self._read_state,
                clear_current_message=self._clear_current,
            )
        except OSError as e:
            self._log(f"HTTP bridge could not listen on {bind}:{port}: {e}")
            self._server = None
            return False
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="tts-http-bridge", daemon=True
        )
        self._thread.start()
        self._log(f"HTTP bridge listening on {bind}:{self.port} (token in {TOKEN_FILE})")
        return True

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None
