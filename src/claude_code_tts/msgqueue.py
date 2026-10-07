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
import math
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
#   pre_action          control, opt.  daemon.py:handle_control_message ("drain"); "supersede"
#                                      is taken by Supersedes before the loop picks anything
#   supersede_session   control, opt.  Supersedes: the session whose older messages are stale
#   supersede_project   control, opt.  Supersedes log line only
#   supersede_claude_session control, opt.  Supersedes: Claude Code's session id of the prompt
#   claude_session_id   hook, opt.     Supersedes: matched against supersede_claude_session
#   hook_started        hook, opt.     Supersedes: when the writing hook started (the turn's
#                                      end); compared to the cutoff instead of timestamp
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


SUPERSEDE = "supersede"

# Sessions that are not a room: the bridge's pages and the daemon's own control path.
_NOT_A_ROOM = ("", "system", "browser")


def write_supersede_message(
    session_id: str, project: str = "", claude_session: str = "", log: Log | None = None
) -> Path | None:
    """Say "a new prompt arrived in session_id now": its older queued messages are stale.

    Written by the UserPromptSubmit hook. The cutoff is this message's own timestamp, taken
    by write_message from the same clock the hook writers of that room use. claude_session
    is Claude Code's own session id from the hook input: two instances in one folder share
    session_id, and with it a prompt in one leaves the other's messages alone.
    An older daemon logs "Control message: pre=supersede" at INFO, speaks nothing and
    removes the file.
    """
    if session_id in _NOT_A_ROOM:
        return None
    fields: dict = {
        "type": "control",
        "session_id": "system",
        "text": "",
        "pre_action": SUPERSEDE,
        "supersede_session": session_id,
    }
    if project:
        fields["supersede_project"] = project
    if claude_session:
        fields["supersede_claude_session"] = claude_session
    queue_file, _ = write_message(fields)
    _say(log, f"Supersede written for {session_id}: {queue_file.name}", "DEBUG")
    return queue_file


def is_supersede(msg: dict) -> bool:
    """A supersede control: taken by Supersedes.apply(), never handed to the control handler."""
    return msg.get("type") == "control" and msg.get("pre_action") == SUPERSEDE


DROP_ALL = "drop_all"


def write_drop_all_message(drop_ids: list[str], log: Log | None = None) -> Path:
    """Say "drop these queued messages": the kraken is released over an empty queue.

    Written by `claude-tts kraken --drop` before it releases the hold, so a long hold's worth of
    speech is dropped, not replayed. drop_ids are the ids the CLI saw in the queue; the daemon
    drops exactly those (by id, never by time: hooks on other machines or in containers stamp with
    their own clocks), takes the control at the top of its next pass, held or not, and writes a
    `dropped (kraken)` outcome for every hook message it removes. An older daemon logs
    "Control message: pre=drop_all" at INFO and removes the file; the CLI knows that and drops
    the files itself in that case.
    """
    fields: dict = {
        "type": "control", "session_id": "system", "text": "", "pre_action": DROP_ALL,
        "drop_ids": [str(i) for i in drop_ids],
    }
    queue_file, _ = write_message(fields)
    _say(log, f"Drop-all written for {len(drop_ids)} message(s): {queue_file.name}")
    return queue_file


def is_drop_all(msg: dict) -> bool:
    """A drop-all control: taken by apply_drop_all(), never handed to the control handler."""
    return msg.get("type") == "control" and msg.get("pre_action") == DROP_ALL


def apply_drop_all(
    messages: list[dict], log: Log | None = None, on_removed: Callable[[dict], None] | None = None
) -> list[dict]:
    """Take every drop-all control in messages and drop the messages it names.

    Returns the messages left, in the same order, without the controls: other controls (a
    restart behind the drop still restarts), and speech the CLI had not seen when it asked.
    Hook and bridge messages alike go; on_removed is called with each (the daemon settles a
    bridge job and writes the ledger outcome there). A file gone already is no error, and a
    control that cannot be removed is logged, not raised: the loop must go on.
    """
    controls = [m for m in messages if is_drop_all(m)]
    if not controls:
        return messages
    wanted: set[str] = set()
    for c in controls:
        ids = c.get("drop_ids")
        if isinstance(ids, list):
            wanted.update(str(i) for i in ids)
    kept: list[dict] = []
    dropped = 0
    for m in messages:
        if is_drop_all(m):
            try:
                Path(m["_file"]).unlink(missing_ok=True)
            except OSError as e:
                _say(log, f"Drop-all control {m['_file']} could not be removed: {e}", "WARN")
            continue
        if m.get("type") == "control" or str(m.get("id") or "") not in wanted:
            kept.append(m)
            continue
        try:
            Path(m["_file"]).unlink()
        except OSError:
            continue  # gone already: aged out, superseded or flushed meanwhile
        dropped += 1
        if on_removed is not None:
            on_removed(m)
    _say(log, f"Drop-all: {dropped} of {len(wanted)} named message(s) dropped on release")
    return kept


def _stamp(value: object) -> float | None:
    """A time a comparison can trust, or None."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        when = float(value)
    except ValueError:
        return None
    return when if math.isfinite(when) else None


def _spoken_since(msg: dict) -> float | None:
    """When the message's turn ended as far as its writer knows: hook_started, else timestamp.

    The Stop hook waits and rereads for up to several seconds before it writes, so its
    write time can fall after the next prompt; the time the hook started cannot.
    """
    started = _stamp(msg.get("hook_started"))
    return started if started is not None else _stamp(msg.get("timestamp"))


class Supersedes:
    """The cutoffs of recent supersede controls, kept in memory after their files are gone.

    A Stop hook can write its reply seconds after the control for the next prompt was taken,
    so a cutoff is remembered for keep_s (the queue's max_age_seconds: anything older ages
    out anyway). Keyed by (session_id, Claude Code session or ""). A daemon restart forgets
    them, the way PauseLedger forgets pauses.
    """

    def __init__(self, keep_s: float = 300.0) -> None:
        self.keep_s = keep_s
        self._cutoffs: dict[tuple[str, str], tuple[float, str, float]] = {}  # -> (cutoff, project, taken)
        self._unremovable: set[str] = set()

    def _take(self, c: dict, log: Log | None, now: float) -> None:
        name = str(c.get("_file", ""))
        try:
            Path(c["_file"]).unlink(missing_ok=True)
        except (OSError, KeyError, TypeError) as e:
            if name not in self._unremovable:  # said once, not on every pass
                self._unremovable.add(name)
                _say(log, f"Could not remove supersede control {name}: {e}", "WARN")
        session = c.get("supersede_session")
        when = _stamp(c.get("timestamp"))
        if not isinstance(session, str) or session in _NOT_A_ROOM or when is None:
            if name not in self._unremovable:
                _say(log, f"Ignored a supersede control without a session or time: {c.get('id')}", "WARN")
            return
        claude = c.get("supersede_claude_session")
        key = (session, claude if isinstance(claude, str) else "")
        project = str(c.get("supersede_project") or session)
        if key not in self._cutoffs or self._cutoffs[key][0] < when:
            self._cutoffs[key] = (when, project, now)

    def cutoff_for(self, msg: dict) -> tuple[float, str] | None:
        """The latest cutoff that applies to msg, with its project; None when none does.

        Claude Code's session id decides when both sides carry it; a side without it falls
        back to the folder-or-room session_id alone.
        """
        session = msg.get("session_id")
        if not isinstance(session, str) or session in _NOT_A_ROOM:
            return None
        raw = msg.get("claude_session_id")
        mine = raw if isinstance(raw, str) else ""
        best: tuple[float, str] | None = None
        for (s, claude), (when, project, _taken) in self._cutoffs.items():
            if s != session or (claude and mine and claude != mine):
                continue
            if best is None or when > best[0]:
                best = (when, project)
        return best

    def apply(
        self, messages: list[dict], log: Log | None = None, now: float | None = None,
        ledger: PauseLedger | None = None,
    ) -> list[dict]:
        """Take every supersede control in messages, then drop what a cutoff makes stale.

        Returns the messages left, in the same order, without the controls. A message is
        dropped when a cutoff applies to it and its hook started before that cutoff:
        replies and intermediate narration alike. Only queue files are touched, so the
        message playing now (the loop is busy playing it and never calls this meanwhile)
        is never cut, and playback.json is untouched on purpose: a message interrupted by a
        pause resumes after a new prompt, a design call left open. Never dropped: control
        messages, bridge messages (any source), and a message with no session or no
        usable time. The control itself never speaks. A cutoff ages like the messages it
        guards: paused time in ledger does not count toward keep_s.
        """
        t = time.time() if now is None else now
        for key, (_when, _project, taken) in list(self._cutoffs.items()):
            if t - taken - (ledger.held_since(taken, t) if ledger else 0.0) > self.keep_s:
                del self._cutoffs[key]
        for c in messages:
            if is_supersede(c):
                self._take(c, log, t)
        if not self._cutoffs:
            return [m for m in messages if not is_supersede(m)]

        kept: list[dict] = []
        for m in messages:
            if is_supersede(m):
                continue
            cutoff = None if m.get("type") == "control" or m.get("source") else self.cutoff_for(m)
            when = _spoken_since(m)
            if cutoff is None or when is None or when >= cutoff[0]:
                kept.append(m)
                continue
            try:
                Path(m["_file"]).unlink()
            except OSError:
                continue  # gone already: aged out or flushed meanwhile
            _say(
                log,
                f"Dropped stale message {m.get('id', '?')} from {m.get('project') or cutoff[1]}: "
                f"a new prompt in {m.get('session_id')} came {cutoff[0] - when:.1f}s after its turn ended",
            )
        return kept


def supersede(messages: list[dict], log: Log | None = None) -> list[dict]:
    """Supersedes.apply with no memory: only the controls in this very list count."""
    return Supersedes().apply(messages, log=log)


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
    messages: list[dict] | None = None,
) -> int:
    """Remove messages older than max_age, not counting paused time. Returns count removed.

    on_removed is called with each aged-out message, so the caller can settle what this
    module does not know about: a bridge message's job would otherwise read "queued"
    forever. A file that is not a message is deleted with a WARN and not reported.
    Given messages (a scan the caller already made), works on that list instead of the
    directory and removes what it ages out from the list too.
    """
    removed = 0
    now = time.time()

    def from_disk():
        for f in QUEUE_DIR.glob("*.json"):
            try:
                yield f, _load(f)
            except (OSError, json.JSONDecodeError, _NotAMessage) as e:
                _say(log, f"Failed to read queue file {f}: {e}", "WARN")
                f.unlink(missing_ok=True)
                nonlocal removed
                removed += 1

    entries = from_disk() if messages is None else [(Path(m["_file"]), m) for m in messages]
    for f, msg in list(entries):
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
            if messages is not None:
                messages.remove(msg)
            if on_removed is not None:
                on_removed(msg)

    return removed


def enforce_max_depth(
    max_depth: int,
    ledger: PauseLedger | None = None,
    log: Log | None = None,
    on_removed: OnRemoved | None = None,
    messages: list[dict] | None = None,
) -> int:
    """Remove oldest messages if queue exceeds max depth.

    Given messages (a scan the caller already made, in timestamp order), works on that
    list instead of a new scan and removes what it trims from the list too.

    Messages that waited through a pause are held, not trimmed: they neither
    count toward the depth nor get removed. Control messages are never trimmed
    and do not count either: a restart from `claude-tts daemon restart` behind a
    burst of speech would otherwise be dropped and the daemon never restart.
    on_removed is called with each trimmed message (see cleanup_old_messages).
    """
    now = time.time()
    source = scan(log) if messages is None else messages
    candidates = [
        m
        for m in source
        if m.get("type") != "control"
        and (not ledger or ledger.held_since(float(m.get("timestamp", 0) or 0), now) <= 0)
    ]
    removed = 0

    while len(candidates) > max_depth:
        oldest = candidates.pop(0)
        Path(oldest["_file"]).unlink(missing_ok=True)
        if messages is not None:
            messages.remove(oldest)
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
