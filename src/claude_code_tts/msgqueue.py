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
OnRemoved = Callable[[dict], None]


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


# --- The message schema ---
#
# One JSON object per file, named "<timestamp:.6f>_<id>.json", written as ".tmp" and renamed so
# the daemon never globs a half-written file. Nothing parses the name; readers use the fields.
# Writers: hook = audio.py:write_queue_message, bridge = bridge.py:write_bridge_message,
# control = msgqueue.py:write_control_message, all through write_message below.
#
# Compatibility is additive only. Hooks in a sandbox and the daemon on the host can be different
# versions in either direction, permanently, so no field is ever dropped (even one nothing reads)
# and a reader treats a missing field as the old shape and ignores fields it does not know.
#
#   field               writers        read by
#   v                   all            nobody yet; absent means a 9.36.x or older writer
#   id                  all            daemon.py:prepare_message (WAV name; job id when source is
#                                      set), register_queued_bridge_jobs; bridge.py:flush_source
#   timestamp           all            scan and play order, cleanup_old_messages, enforce_max_depth
#   type "control"      control        next_speakable; daemon.py:daemon_loop hands it to
#                                      handle_control_message
#   session_id          all            daemon.py:prepare_message ("browser" for the bridge,
#                                      "system" for control)
#   project             hook, bridge   daemon.py:prepare_message, register_queued_bridge_jobs,
#                                      daemon_loop and daemon_status (log and print lines);
#                                      enforce_max_depth (log line)
#   text                all            daemon.py:prepare_message, handle_control_message,
#                                      next_speakable
#   persona             hook, bridge   daemon.py:prepare_message, register_queued_bridge_jobs;
#                                      handle_control_message reads it but control never writes it
#   speed               hook, bridge   daemon.py:prepare_message
#   speed_method        hook, bridge   daemon.py:prepare_message
#   voice_kokoro        hook           daemon.py:prepare_message
#   voice_kokoro_blend  hook           daemon.py:prepare_message
#   voice_sherpa        hook           nobody (the persona decides); kept for older daemons
#   speaker_sherpa      hook           nobody (the persona decides); kept for older daemons
#   voice_mlx           hook           daemon.py:prepare_message, only when engine is "mlx"
#   speaker_mlx         hook           daemon.py:prepare_message, only when engine is "mlx"
#   lang_mlx            hook           daemon.py:prepare_message, only when engine is "mlx"
#   pitch_filter        hook           daemon.py:prepare_message copies it into playback state
#   engine              hook, optional daemon.py:prepare_message ("mlx": the message's mlx voice)
#   source              bridge         daemon.py:prepare_message, register_queued_bridge_jobs;
#                                      remove_source
#   want_marks          bridge         daemon.py:prepare_message
#   lane                bridge, opt.   play_order ("background"), register_queued_bridge_jobs
#   pre_action          control, opt.  daemon.py:handle_control_message ("drain")
#   post_action         control, opt.  daemon.py:handle_control_message (restart, reload_config,
#                                      stop)

SCHEMA_VERSION = 1


def write_message(fields: dict) -> tuple[Path, dict]:
    """Write one queue message; the one writer. Returns the file and the message as written.

    Adds "id" and "timestamp" unless the caller set them, and always "v". The timestamp is
    rounded to the microsecond so the file name and the field agree.
    """
    msg = {
        "id": fields.get("id") or secrets.token_hex(8),
        "timestamp": float(f"{float(fields.get('timestamp') or time.time()):.6f}"),
        **{k: v for k, v in fields.items() if k not in ("id", "timestamp", "v")},
        "v": SCHEMA_VERSION,
    }

    ensure_dir()
    queue_file = QUEUE_DIR / f"{msg['timestamp']:.6f}_{msg['id']}.json"
    tmp_file = queue_file.with_suffix(".tmp")
    tmp_file.write_text(json.dumps(msg))
    tmp_file.rename(queue_file)
    return queue_file, msg


def write_control_message(
    text: str = "",
    pre_action: str | None = None,
    post_action: str | None = None,
    log: Log | None = None,
) -> Path:
    """Write a control message to the queue directory."""
    fields: dict = {"type": "control", "session_id": "system", "text": text}
    if pre_action:
        fields["pre_action"] = pre_action
    if post_action:
        fields["post_action"] = post_action
    queue_file, _ = write_message(fields)
    _say(log, f"Control message written: {queue_file.name}")
    return queue_file


class _NotAMessage(ValueError):
    """Valid JSON that is not an object: `[]`, `null`, a bare string or number."""


def _load(f: Path) -> dict:
    """One queue file as a dict; raises OSError, JSONDecodeError or _NotAMessage.

    Every writer writes an object. Anything else is a bad write, and it must fail the same
    way unparseable JSON does: `[]` or `null` used to pass json.load and then raise
    TypeError or AttributeError at the first key access, outside every catch, so the file
    was never deleted and the loop failed on it every pass.
    """
    with open(f) as fp:
        msg = json.load(fp)
    if not isinstance(msg, dict):
        raise _NotAMessage(f"not a JSON object: {type(msg).__name__}")
    return msg


def text_of(msg: dict) -> str:
    """The message's text, or "" when it is missing or not a string.

    The loop skips an empty message and fails its job; a text of 123 or a list used to
    pass the emptiness check through str() and then raise at the first slice.
    """
    text = msg.get("text", "")
    return text if isinstance(text, str) else ""


def scan(log: Log | None = None) -> list[dict]:
    """All messages in the queue, sorted by timestamp, each with its path under "_file".

    A file that cannot be read or parsed, or holds anything but a JSON object, is deleted,
    so one bad write does not stop the queue for good.
    """
    messages: list[dict] = []
    if not QUEUE_DIR.exists():
        return messages

    for f in QUEUE_DIR.glob("*.json"):
        try:
            msg = _load(f)
        except (OSError, json.JSONDecodeError, _NotAMessage) as e:
            _say(log, f"Failed to read queue file {f}: {e}", "WARN")
            f.unlink(missing_ok=True)
            continue
        msg["_file"] = f
        messages.append(msg)

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
    max_age_seconds: int,
    ledger: PauseLedger | None = None,
    log: Log | None = None,
    on_removed: OnRemoved | None = None,
) -> int:
    """Remove messages older than max_age, not counting paused time. Returns count removed.

    on_removed is called with each aged-out message, so the caller can settle what this
    module does not know about: a bridge message's job would otherwise read "queued"
    forever. A file that is not a message is deleted with a WARN and not reported.
    """
    removed = 0
    now = time.time()

    for f in QUEUE_DIR.glob("*.json"):
        try:
            msg = _load(f)
        except (OSError, json.JSONDecodeError, _NotAMessage) as e:
            _say(log, f"Failed to read queue file {f}: {e}", "WARN")
            f.unlink(missing_ok=True)
            removed += 1
            continue
        try:
            ts = float(msg.get("timestamp", 0))
        except (TypeError, ValueError):
            ts = 0.0  # no usable timestamp: as old as it gets
        held = ledger.held_since(ts, now) if ledger else 0.0
        if now - ts - held > max_age_seconds:
            try:
                f.unlink()
            except OSError:
                continue  # gone already: the loop or a flush took it
            removed += 1
            _say(log, f"Removed stale message: {f.name}")
            if on_removed is not None:
                on_removed(msg)

    return removed


def enforce_max_depth(
    max_depth: int,
    ledger: PauseLedger | None = None,
    log: Log | None = None,
    on_removed: OnRemoved | None = None,
) -> int:
    """Remove oldest messages if queue exceeds max depth.

    Messages that waited through a pause are held, not trimmed: they neither
    count toward the depth nor get removed. Control messages are never trimmed
    and do not count either: a restart from `claude-tts daemon restart` behind a
    burst of speech would otherwise be dropped and the daemon never restart.
    on_removed is called with each trimmed message (see cleanup_old_messages).
    """
    now = time.time()
    messages = [
        m
        for m in scan(log)
        if m.get("type") != "control"
        and (not ledger or ledger.held_since(float(m.get("timestamp", 0) or 0), now) <= 0)
    ]
    removed = 0

    while len(messages) > max_depth:
        oldest = messages.pop(0)
        oldest["_file"].unlink(missing_ok=True)
        removed += 1
        _say(log, f"Queue overflow, removed: {oldest.get('project', 'unknown')}")
        if on_removed is not None:
            on_removed(oldest)

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
        if text_of(m).strip():
            return m
    return None
