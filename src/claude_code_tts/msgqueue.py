"""The message queue on disk: ~/.claude-tts/queue/, one JSON file per message.

Hooks, the bridge and the daemon's own control path all write here, and the daemon reads,
orders, ages and trims. This module owns the directory and the functions that touch it;
QUEUE_DIR is the one name a test redirects. It imports only the standard library and
config, so the hook path (audio.write_queue_message) stays cheap. Functions that have
something to say take the caller's `log(msg, level)`; the daemon's logger lives in daemon.py.

Named msgqueue, not queue: a module called queue.py inside the package would shadow the
standard library's queue for any interpreter whose sys.path[0] is the package directory,
and concurrent.futures, logging.handlers and onnxruntime all import that one.

Moved out of daemon.py, audio.py and bridge.py as the second cut of docs/redesign-10.md.
"""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import Callable
from pathlib import Path

from claude_code_tts.config import TTS_CONFIG_DIR

QUEUE_DIR = TTS_CONFIG_DIR / "queue"

BACKGROUND_LANE = "background"

Log = Callable[[str, str], None]


def _say(log: Log | None, msg: str, level: str = "INFO") -> None:
    if log is not None:
        log(msg, level)


def ensure_dir() -> Path:
    """The queue directory, created if missing."""
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    return QUEUE_DIR


def depth() -> int:
    """How many messages wait, control messages included."""
    return len(list(QUEUE_DIR.glob("*.json"))) if QUEUE_DIR.exists() else 0


def write_control_message(
    text: str = "",
    pre_action: str | None = None,
    post_action: str | None = None,
    log: Log | None = None,
) -> Path:
    """Write a control message to the queue directory."""
    ensure_dir()

    msg: dict = {
        "id": secrets.token_hex(8),
        "timestamp": time.time(),
        "type": "control",
        "session_id": "system",
        "text": text,
    }
    if pre_action:
        msg["pre_action"] = pre_action
    if post_action:
        msg["post_action"] = post_action

    queue_file = QUEUE_DIR / f"{msg['timestamp']}_{msg['id']}.json"
    tmp_file = queue_file.with_suffix(".tmp")
    tmp_file.write_text(json.dumps(msg))
    tmp_file.rename(queue_file)
    _say(log, f"Control message written: {queue_file.name}")
    return queue_file


def scan(log: Log | None = None) -> list[dict]:
    """All messages in the queue, sorted by timestamp, each with its path under "_file".

    A file that cannot be read or parsed is deleted, so one bad write does not stop the
    queue for good.
    """
    messages: list[dict] = []
    if not QUEUE_DIR.exists():
        return messages

    for f in QUEUE_DIR.glob("*.json"):
        try:
            with open(f) as fp:
                msg = json.load(fp)
                msg["_file"] = f
                messages.append(msg)
        except (OSError, json.JSONDecodeError) as e:
            _say(log, f"Failed to read queue file {f}: {e}", "WARN")
            f.unlink(missing_ok=True)

    messages.sort(key=lambda m: m.get("timestamp", 0))
    return messages


def remove_source(source: str) -> list[dict]:
    """Delete queued (not yet playing) messages from one bridge source; the messages removed."""
    removed: list[dict] = []
    if not QUEUE_DIR.exists():
        return removed
    for f in QUEUE_DIR.glob("*.json"):
        try:
            msg = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if msg.get("source") == source:
            f.unlink(missing_ok=True)
            removed.append(msg)
    return removed


def play_order(messages: list[dict]) -> list[dict]:
    """The order the loop speaks in: everything else first, then the background lane.

    A long read queued by a page marks its blocks lane "background" so a
    room's one-liner arriving mid-read goes next, at the block boundary, instead
    of behind every queued block. Within a lane the timestamp order holds, and
    control messages are never background, so they still come first. Ageing and
    depth trimming keep using timestamp order; this is only who speaks next.
    """
    return sorted(messages, key=lambda m: 1 if m.get("lane") == BACKGROUND_LANE else 0)


class PauseLedger:
    """Seconds the daemon has spent paused, so a held queue does not age.

    A pause means "hold everything", so time spent paused is subtracted from a
    message's age before the max_age check, and a message that waited through a
    pause is exempt from depth trimming until it plays. The ledger lives in
    memory: a daemon restart forgets it and wall-clock age applies again.
    """

    def __init__(self) -> None:
        self._closed: list[tuple[float, float]] = []
        self._open: float | None = None

    def mark(self, paused: bool, now: float | None = None) -> None:
        """Record the pause flag as seen on this pass of the loop."""
        t = time.time() if now is None else now
        if paused and self._open is None:
            self._open = t
        elif not paused and self._open is not None:
            self._closed.append((self._open, t))
            self._open = None

    @property
    def paused(self) -> bool:
        return self._open is not None

    def open_for(self, now: float | None = None) -> float:
        """Seconds the current pause has been held; 0.0 when not paused."""
        if self._open is None:
            return 0.0
        t = time.time() if now is None else now
        return max(0.0, t - self._open)

    def held_since(self, since: float, now: float | None = None) -> float:
        """Paused seconds between since and now."""
        t = time.time() if now is None else now
        intervals = list(self._closed)
        if self._open is not None:
            intervals.append((self._open, t))
        held = 0.0
        for start, end in intervals:
            held += max(0.0, min(end, t) - max(start, since))
        return held


def cleanup_old_messages(
    max_age_seconds: int, ledger: PauseLedger | None = None, log: Log | None = None
) -> int:
    """Remove messages older than max_age, not counting paused time. Returns count removed."""
    removed = 0
    now = time.time()

    for f in QUEUE_DIR.glob("*.json"):
        try:
            with open(f) as fp:
                msg = json.load(fp)
            ts = float(msg.get("timestamp", 0))
            held = ledger.held_since(ts, now) if ledger else 0.0
            if now - ts - held > max_age_seconds:
                f.unlink()
                removed += 1
                _say(log, f"Removed stale message: {f.name}")
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            f.unlink(missing_ok=True)
            removed += 1

    return removed


def enforce_max_depth(max_depth: int, ledger: PauseLedger | None = None, log: Log | None = None) -> int:
    """Remove oldest messages if queue exceeds max depth.

    Messages that waited through a pause are held, not trimmed: they neither
    count toward the depth nor get removed.
    """
    now = time.time()
    messages = [
        m
        for m in scan(log)
        if not ledger or ledger.held_since(float(m.get("timestamp", 0) or 0), now) <= 0
    ]
    removed = 0

    while len(messages) > max_depth:
        oldest = messages.pop(0)
        oldest["_file"].unlink(missing_ok=True)
        removed += 1
        _say(log, f"Queue overflow, removed: {oldest.get('project', 'unknown')}")

    return removed


def next_speakable(messages: list[dict], current: Path) -> dict | None:
    """The message the loop will pick after `current`, if it is one worth synthesizing ahead.

    None when the queue is empty past `current`, or when a control message
    comes first: the loop handles control before anything else.
    """
    for m in messages:
        if m.get("_file") == current:
            continue
        if m.get("type") == "control":
            return None
        if str(m.get("text", "")).strip():
            return m
    return None
