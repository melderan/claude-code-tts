"""The voice ledger: what each hook was asked to speak, kept in full, and what became of it.

Two files, two writers, and never one file shared across a mount:

- ``~/.claude/voice-ledger/<session>.<start>.jsonl`` is written by the hook, in the room whose
  reply it is, one JSON line per utterance, before the text filter runs. The line carries the id
  the queue message carries, the hook event, and the reply's own words. It lives beside the
  transcripts so whatever archives ``~/.claude`` archives it too. ``start`` is the UTC second of
  this ``~/.claude``'s first ledger write (the mtime of an empty ``.start`` file made then and
  never written): a rebuilt room keeps its session id and loses ``~/.claude``, and without the
  stamp it would begin a new, shorter file under the name its archive already holds. Files from
  before 9.71.0 are ``<session>.jsonl``; they are read with the rest, oldest first.
- ``~/.claude-tts/ledger/<session>.outcomes.jsonl`` is written by the daemon on the machine it
  runs on: played, cancelled, failed, or dropped with the reason, keyed by the same id.

Nothing in this module deletes or shortens anything. Every write is one appended line (and,
once per ``~/.claude``, the empty ``.start`` file is created). A write
that fails returns False for the caller to log, and is never raised, so a full disk or a missing
directory never costs a word of speech. ``read`` joins a session's lines with the latest outcome
of each id; a torn line at the tail of either file is skipped and the lines before it stand.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from pathlib import Path

OUTCOMES_SUFFIX = ".outcomes.jsonl"
START_FILE = ".start"
_NAME = re.compile(r"(?P<session>.+?)(?:\.(?P<start>\d{8}T\d{6}Z))?\.jsonl")


def new_id() -> str:
    """An id for one utterance, the shape msgqueue gives a message, minted by the hook."""
    return secrets.token_hex(8)


def ledger_dir() -> Path:
    """Where the hook writes, under ~/.claude; read at call time so HOME decides."""
    return Path.home() / ".claude" / "voice-ledger"


def outcomes_dir() -> Path:
    """Where the daemon writes, under its own ~/.claude-tts."""
    return Path.home() / ".claude-tts" / "ledger"


def _file_name(session_id: str) -> str:
    """A session id as a file name: anything but letters, digits, dot, dash and underscore becomes an underscore."""
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in session_id)
    return safe.strip(".") or "unknown"


def _append(path: Path, entry: dict) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
        return True
    except (OSError, TypeError, ValueError):
        return False


def _start(directory: Path) -> str:
    """This directory's start stamp, made on its first write: the first O_EXCL create wins, so racing hooks agree."""
    marker = directory / START_FILE
    directory.mkdir(parents=True, exist_ok=True)
    try:
        os.close(os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644))
    except FileExistsError:
        pass
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(marker.stat().st_mtime))


def record(session_id: str, entry: dict, *, directory: Path | None = None) -> bool:
    """Append one utterance a hook accepted; ``ts`` and ``session`` are added unless the entry has them."""
    row = {"ts": time.time(), "session": session_id, **entry}
    d = directory or ledger_dir()
    try:
        start = _start(d)
    except OSError:
        return False
    return _append(d / f"{_file_name(session_id)}.{start}.jsonl", row)


def _session_files(session_id: str, directory: Path) -> list[Path]:
    """A session's ledger files, oldest first: the unstamped name from before 9.71.0, then by start stamp."""
    name = _file_name(session_id)
    found = []
    for p in directory.glob(f"{name}*.jsonl") if directory.is_dir() else []:
        m = _NAME.fullmatch(p.name)
        if m and m["session"] == name:
            found.append((m["start"] or "", p))
    return [p for _, p in sorted(found)]


def record_outcome(
    session_id: str,
    msg_id: str,
    outcome: str,
    reason: str = "",
    *,
    directory: Path | None = None,
    extra: dict | None = None,
    **more: object,
) -> bool:
    """Append how the message ``msg_id`` ended: played, cancelled, failed, or dropped with a reason.

    ``extra`` and keyword arguments both add fields to the line (played_s, near_end, sentences).
    """
    row: dict = {"ts": time.time(), "id": msg_id, "outcome": outcome}
    if reason:
        row["reason"] = reason
    row.update(extra or {})
    row.update(more)
    return _append((directory or outcomes_dir()) / f"{_file_name(session_id)}{OUTCOMES_SUFFIX}", row)


def _read_lines(path: Path) -> list[dict]:
    rows: list[dict] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue  # a torn line; the ones before it stand
                if isinstance(obj, dict):
                    rows.append(obj)
    except OSError:
        return rows
    return rows


def sessions(directory: Path | None = None) -> list[str]:
    """The sessions that have a ledger, by file name."""
    d = directory or ledger_dir()
    if not d.is_dir():
        return []
    return sorted({m["session"] for p in d.glob("*.jsonl") if (m := _NAME.fullmatch(p.name))})


def outcomes(session_id: str, *, directory: Path | None = None) -> list[dict]:
    """The daemon's outcome lines for one session, oldest first."""
    return _read_lines((directory or outcomes_dir()) / f"{_file_name(session_id)}{OUTCOMES_SUFFIX}")


def read(
    session_id: str,
    *,
    limit: int | None = None,
    directory: Path | None = None,
    outcomes_directory: Path | None = None,
) -> list[dict]:
    """One session's utterances, oldest first, each with the latest outcome of its id joined in.

    The joined keys are ``outcome``, ``outcome_reason`` (when the daemon gave one) and
    ``outcome_ts``. ``limit`` keeps the newest that many rows; 0 or None keeps them all.
    """
    rows = [r for p in _session_files(session_id, directory or ledger_dir()) for r in _read_lines(p)]
    latest: dict[str, dict] = {}
    for o in outcomes(session_id, directory=outcomes_directory):
        if o.get("id"):
            latest[str(o["id"])] = o
    for r in rows:
        hit = latest.get(str(r.get("id")))
        if hit is None:
            continue
        r["outcome"] = hit.get("outcome")
        if hit.get("reason"):
            r["outcome_reason"] = hit["reason"]
        r["outcome_ts"] = hit.get("ts")
    if limit:
        rows = rows[-limit:]
    return rows
