"""The TLDL reel: what a person's hold kept from them, as a minute of highlights.

JMO, 2026-10-24 (D269): "do you need to see every first down or do you need to see the
touchdowns and the field goals and the miraculous catches". While a person holds the voices,
every reply the hold keeps from them is noted here; when enough have piled up (``every_unheard``
of them, or ``after_s`` into the hold) a reel is due, at most one per ``min_gap_s``. A reel is
spoken as its own session, ``tldl``, so it obeys the hold like any room: ``claude-tts hold --let
tldl`` lets it through a hold, a full hold (a meeting, an interview, a nap) keeps it until the
release, and a recording holds it as it holds everyone. At the release one reel covers the
replies the hold dropped (trimmed, expired, kraken) when they hold a play or are ten or more;
the ones still queued replay. A stale reply (a newer prompt in its room) is counted in the
hold's cost and never told: the person was at that room after it.

A play is something that needs the person (a card, an ask), something shipped (merged,
released, tagged), something broken or blocked, or a new friend. Everything else is a first
down and never reaches the reel. A keyword pass picks the candidates; the local mind, when one
is configured (``mind_url``, an OpenAI-shaped chat endpoint on this machine; any other host is
refused), cuts them to the plays and writes the words. Every sentence it writes that names a
number (a PR, a card, a Mac Ask, a version) must find that number in the candidates, and a
sentence that says a thing shipped must find a candidate saying that number shipped; a sentence
that fails is dropped, never reworded (2 of 7 PR claims were wrong in the first hand-built reel).
With no mind, a mind that does not answer, or nothing of its that holds up, the reel speaks the
rooms' own first sentences.

Files, all under ``~/.claude-tts/reel/``, appended or written whole, never deleted:

- ``hold-<since>.jsonl``: one line per reply the hold kept (id, session, time, and for a
  candidate its first sentence; a first down keeps no words), one per outcome, one per reel.
  The directory is shared with every sandbox that mounts it, so full replies are not kept here;
  they are in each room's own voice ledger.
- ``<UTC stamp>.txt``: the words of each reel, written before they are spoken.
- ``status.json``: the hold as the daemon last saw it, for ``claude-tts status`` and the bridge.
"""

from __future__ import annotations

import http.client
import json
import re
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from claude_code_tts.config import TTS_CONFIG_DIR, atomic_write_json

REEL_DIR = TTS_CONFIG_DIR / "reel"
STATUS_FILE = REEL_DIR / "status.json"
REEL_SESSION = "tldl"
TICK_EVERY_S = 30.0
# A status file this old was written by a daemon that is gone; readers treat it as absent.
# Longer than a tick plus the mind's longest answer, during which nothing rewrites it.
STATUS_MAX_AGE_S = 300.0
# At a release, a reel for what the hold dropped when it holds a play or is at least this many.
RELEASE_MIN_UNHEARD = 10

DEFAULTS: dict = {
    "enabled": True,
    "every_unheard": 50,
    "after_s": 7200,
    "min_gap_s": 3600,
    "max_words": 150,
    "max_candidates": 120,
    "mind_url": "",
    "mind_model": "",
    "mind_timeout_s": 120,
    "persona": "",
}

# The outcome reasons a hold's cost is counted in, by the name a reader sees.
COST_NAMES = {"depth": "trimmed", "superseded": "stale", "expired": "expired", "kraken": "dropped"}

PLAY = re.compile(
    r"\b(?:"
    r"merged|released|tagged|shipped|deployed|landed|"
    r"broke|broken|blocked|blocking|failing|failed|regression|outage|crash(?:ed|es|ing)?|"
    r"needs? (?:you|JMO|your)|your (?:call|answer|word|review)|card D\d+|D\d{2,4}|Mac Ask|MA\d+|"
    r"named|new friend"
    r")\b",
    re.IGNORECASE,
)
SHIPPED = re.compile(r"\b(?:merged|released|tagged|shipped|deployed|landed)\b", re.IGNORECASE)
NEGATION = re.compile(
    r"\b(?:not|no|yet|still|never|isn't|hasn't|wasn't|won't|unmerged|awaiting|waiting|pending|before|until)\b",
    re.IGNORECASE,
)
# A shipped word this many words or fewer from a number is about that number.
NEAR_WORDS = 4
# What a sentence names that can be checked against the candidates: a PR or several, a card, a
# Mac Ask, a version, or any bare number of three digits or more.
NUMBERED = re.compile(
    r"#\d+|\b(?:PRs?|pull requests?|issues?)\s+#?\d+(?:(?:,\s*|,?\s+and\s+)#?\d+)*"
    r"|\bD\d{2,4}\b|\bMA\d+\b|\bv?\d+\.\d+(?:\.\d+)?\b|\b\d{3,}\b",
    re.IGNORECASE,
)
HEAD = "Too long, didn't listen."
# Markdown that is not words; a "#" before a number is a PR and stays.
MARKUP = re.compile(r"[*`>]|#+(?=\s|$)|(?<!\w)_|_(?!\w)", re.MULTILINE)


def settings(raw_config: dict) -> dict:
    """The reel's settings: the config's ``reel`` block over the defaults."""
    block = raw_config.get("reel")
    return {**DEFAULTS, **(block if isinstance(block, dict) else {})}


def room_of(session_id: str) -> str:
    tag = session_id.rsplit("--", 1)[-1]
    return tag.removeprefix("claude-code-") or tag


def first_sentence(text: str, limit: int = 200) -> str:
    t = re.sub(r"\s+", " ", MARKUP.sub("", text)).strip()
    m = re.match(rf"(.{{12,{limit}}}?[.!?])(\s|$)", t)
    return (m.group(1) if m else t[:limit]).strip()


def is_play(text: str) -> bool:
    return bool(PLAY.search(text[:600]))


class HoldWindow:
    """Every reply one person's hold kept, keyed by the hold's start; safe across threads."""

    def __init__(self, since: float, directory: Path = REEL_DIR) -> None:
        self.since = since
        self.path = directory / f"hold-{since:.0f}.jsonl"
        self._lock = threading.Lock()
        self.order: list[str] = []
        self.entries: dict[str, dict] = {}
        self.outcomes: dict[str, str] = {}
        self.reels: list[dict] = []
        self.released = False
        self._load()

    def _load(self) -> None:
        try:
            lines = self.path.read_text("utf-8", errors="replace").splitlines()
        except OSError:
            return
        for line in lines:
            try:
                r = json.loads(line)
            except ValueError:
                continue  # a torn tail
            if not isinstance(r, dict):
                continue
            self._apply(r)

    def _apply(self, r: dict) -> None:
        kind = r.get("kind")
        if not isinstance(r.get("id", ""), str):
            return  # the file is on a shared mount; a line not of our shape is not ours
        if kind == "held" and r.get("id") and r["id"] not in self.entries:
            self.order.append(r["id"])
            self.entries[r["id"]] = r
        elif kind == "outcome" and r.get("id") in self.entries:
            self.outcomes[r["id"]] = str(r.get("outcome") or "")
        elif kind == "reel":
            self.reels.append(r)
        elif kind == "released":
            self.released = True

    def _append(self, r: dict) -> None:
        self._apply(r)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(r) + "\n")
        except OSError:
            pass  # the window in memory stands; the next write tries again

    def note(self, msg: dict, now: float | None = None) -> bool:
        """Note a reply the hold keeps; True the first time this id is seen."""
        msg_id = str(msg.get("id") or "")
        session = str(msg.get("session_id") or "")
        if not msg_id or not session or session == REEL_SESSION or msg.get("type") == "control" or msg.get("source"):
            return False
        with self._lock:
            if msg_id in self.entries:
                return False
            raw = msg.get("text")
            text = raw if isinstance(raw, str) else ""
            entry: dict = {
                "kind": "held", "id": msg_id, "session": session,
                "project": str(msg.get("project") or ""), "at": now or time.time(),
            }
            if is_play(text):
                entry["play"] = first_sentence(text)
            self._append(entry)
            return True

    def note_outcome(self, msg: dict, outcome: str, reason: str = "", held: bool = True) -> None:
        """How a noted reply ended; a dropped one the hold kept (held) and not seen yet is noted first."""
        msg_id = str(msg.get("id") or "")
        if outcome == "dropped" and held and msg_id not in self.entries:
            self.note(msg)
        with self._lock:
            if msg_id not in self.entries:
                return
            name = f"dropped:{reason}" if outcome == "dropped" else outcome
            if self.outcomes.get(msg_id) == name:
                return
            self._append({"kind": "outcome", "id": msg_id, "outcome": name, "at": time.time()})

    def mark_released(self) -> None:
        with self._lock:
            if not self.released:
                self._append({"kind": "released", "at": time.time()})

    def unheard(self) -> list[dict]:
        """Every noted reply that has not played, oldest first."""
        with self._lock:
            return [self.entries[i] for i in self.order if self.outcomes.get(i) != "played"]

    def cost(self) -> dict[str, int]:
        """What the hold dropped, by the name a reader sees: trimmed, stale, expired, dropped."""
        counts = dict.fromkeys(COST_NAMES.values(), 0)
        with self._lock:
            for outcome in self.outcomes.values():
                if outcome.startswith("dropped:"):
                    name = COST_NAMES.get(outcome.split(":", 1)[1], "dropped")
                    counts[name] += 1
        return counts

    def last_reel_at(self) -> float | None:
        with self._lock:
            return float(self.reels[-1]["at"]) if self.reels else None

    def pending(self, dropped_only: bool = False) -> list[dict]:
        """The unheard replies no reel has covered yet; at a release only the dropped ones.

        A stale reply (a newer prompt in its room superseded it) is left out: the person was
        at that room's keyboard after it, so it is no news to them.
        """
        return self.pending_upto(dropped_only)[0]

    def pending_upto(self, dropped_only: bool = False) -> tuple[list[dict], int]:
        """pending() and how far into the window it read, taken under one lock."""
        with self._lock:
            start = int(self.reels[-1].get("upto", 0)) if self.reels else 0
            out = []
            for i in self.order[start:]:
                o = self.outcomes.get(i, "")
                if o in ("played", "dropped:superseded") or (dropped_only and not o.startswith("dropped:")):
                    continue
                out.append(self.entries[i])
            return out, len(self.order)

    def record_reel(self, file: Path | None, plays: int, mind: str, upto: int, now: float | None = None) -> None:
        self._append({
            "kind": "reel", "at": now or time.time(), "upto": upto,
            "file": str(file) if file else "", "plays": plays, "mind": mind,
        })


def due(window: HoldWindow, cfg: dict, now: float, dropped_only: bool = False) -> str:
    """Why a reel is due now ("count" or "age"), or "" when it is not."""
    pending = window.pending(dropped_only)
    if not pending:
        return ""
    last = window.last_reel_at()
    if last is not None and now - last < float(cfg["min_gap_s"]):
        return ""
    if len(pending) >= int(cfg["every_unheard"]):
        return "count"
    if now - (last if last is not None else window.since) >= float(cfg["after_s"]):
        return "age"
    return ""


def release_due(window: HoldWindow) -> bool:
    """Whether the release of a hold gets a reel: what it dropped holds a play, or is at least
    RELEASE_MIN_UNHEARD replies. Neither the count bar nor the hour gap of due() applies, since
    nothing else will ever tell the person; a short hold that dropped a few first downs stays quiet.
    """
    pending = window.pending(dropped_only=True)
    return len(pending) >= RELEASE_MIN_UNHEARD or any(e.get("play") for e in pending)


def candidates(pending: list[dict], limit: int) -> list[dict]:
    """The replies that may be plays, newest `limit` of them, with repeated lines taken out."""
    seen: set[tuple[str, str]] = set()
    out = []
    for e in pending:
        line = str(e.get("play") or "")
        key = (str(e.get("session")), line)
        if not line or key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out[-limit:] if limit > 0 else out


def mind_prompt(cands: list[dict]) -> str:
    by_room: dict[str, list[str]] = {}
    for e in cands:
        stamp = datetime.fromtimestamp(float(e.get("at") or 0), timezone.utc).strftime("%H:%MZ")
        by_room.setdefault(room_of(str(e.get("session"))), []).append(f"{stamp} {e['play']}")
    rooms = "\n\n".join(f"Room {room}:\n" + "\n".join(lines) for room, lines in by_room.items())
    return (
        "You write a spoken highlights reel, like sports highlights, for a person who was away "
        "while these rooms worked. A PLAY is one of: something that needs the person (a card, a "
        "decision, an ask); something shipped (merged, released, tagged, deployed); something "
        "broken or blocked; a new friend or name. Everything else is a first down (taking a task, "
        "acks, progress, rechecks) and is left out. A later line can settle an earlier one (a fix "
        "after a break); then say the settled state. Room by room, at most three plays a room, one "
        "short plain sentence each, naming the room. Copy every number exactly as written; never "
        "add one. No markdown, no lists, no symbols. If there are no plays, write NONE.\n\n" + rooms
    )


def ask_mind(url: str, model: str, prompt: str, timeout_s: float, max_tokens: int = 400) -> str:
    """One chat completion from the local mind; raises on any failure, the caller falls back."""
    body: dict = {"max_tokens": max_tokens, "temperature": 0.2, "messages": [{"role": "user", "content": prompt}]}
    if model:
        body["model"] = model
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout_s) as r:  # noqa: S310  a configured local endpoint
        data = json.load(r)
    return str(data["choices"][0]["message"]["content"]).strip()


def sentences(text: str) -> list[str]:
    flat = re.sub(r"\s+", " ", re.sub(r"^\s*(?:[-*]|\d+[.)])\s", " ", MARKUP.sub("", text), flags=re.MULTILINE)).strip()
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", flat) if s.strip()]


def ground(text: str, cands: list[dict]) -> tuple[list[str], list[str]]:
    """The mind's sentences that hold up against the candidates, and the ones that do not.

    A sentence that names a number must find it in a candidate. One that says something shipped
    must name the number of what shipped, and a candidate must say that number shipped: the
    shipped word within NEAR_WORDS words of it, no "not", "yet" or "still" between ("PR 7503 is
    not merged yet" never backs "7503 merged"). A shipped claim with no number is dropped: there
    is nothing to check it against. Repeated sentences go too.
    """
    lines = [str(e.get("play") or "") for e in cands]
    joined = "\n".join(lines)
    kept: list[str] = []
    dropped: list[str] = []
    for s in sentences(text):
        if s.upper().startswith("NONE") or s.lower().startswith("too long"):
            continue
        if s in kept:
            continue
        numbers = [c for n in NUMBERED.findall(s) for c in _cores(n)]
        ok = all(_named(c, joined) for c in numbers)
        if ok and _claims_shipped(s):
            ok = bool(numbers) and all(any(_shipped_near(line, c) for line in lines) for c in numbers)
        (kept if ok else dropped).append(s)
    return kept, dropped


def _cores(number: str) -> list[str]:
    """The parts of a named number that must match: 2049 for "#2049" or "PR 2049", both of
    "PRs 2061 and 2062", 9.73.4 for "v9.73.4"."""
    n = number.lower()
    if re.match(r"(?:prs?|pull requests?|issues?)\s", n):
        return re.findall(r"\d+", n)
    n = n.lstrip("#")
    return [n[1:] if re.fullmatch(r"v\d.*", n) else n]


def _named(core: str, text: str) -> bool:
    """Whether text names the number as a whole token: 2049 is not found in 12049 or 2049.1."""
    return re.search(rf"(?<![\w.]){re.escape(core)}(?!\w|\.\d)", text.lower().replace("#", " ")) is not None


def _words(text: str) -> list[str]:
    return [w.rstrip(".,;:!?") for w in re.findall(r"[\w.']+", text.lower().replace("#", " "))]


def _claims_shipped(sentence: str) -> bool:
    """Whether the sentence says something shipped: a shipped word with no negation just before it."""
    words = _words(sentence)
    return any(SHIPPED.fullmatch(w) and not _negated(words[max(0, i - 3):i]) for i, w in enumerate(words))


def _negated(words: list[str]) -> bool:
    return any(NEGATION.fullmatch(w) for w in words)


def _shipped_near(line: str, core: str) -> bool:
    """Whether the line says that number shipped: a shipped word near it in the same clause, and
    nothing negating it ("#2058 merged; #2059 still failing" backs 2058, never 2059)."""
    for clause in re.split(r";|\bbut\b", line):
        words = _words(clause)
        nums = [i for i, w in enumerate(words) if w.lstrip("v") == core or w == core]
        ships = [i for i, w in enumerate(words) if SHIPPED.fullmatch(w)]
        for i in nums:
            for j in ships:
                lo, hi = min(i, j), max(i, j)
                if hi - lo > NEAR_WORDS or _negated(words[lo + 1:hi]) or _negated(words[max(0, j - 3):j]):
                    continue
                if i > j and _negated(words[i + 1:i + 3]):
                    continue  # "merged #2058 and #2059 still failing": the word after says 2059 did not
                return True
    return False


def fallback_lines(cands: list[dict], most: int = 4) -> list[str]:
    """The rooms' own first sentences, newest first, one per room: nothing to check, they said it."""
    out: list[str] = []
    rooms: set[str] = set()
    for e in reversed(cands):
        room = room_of(str(e.get("session")))
        if room in rooms:
            continue
        rooms.add(room)
        out.append(f"{room}: {e['play']}")
        if len(out) >= most:
            break
    return out


def compose(pending: list[dict], cands: list[dict], body: list[str], max_words: int, mind_note: str = "") -> str:
    """The words of one reel: the head, the count, the plays, capped at max_words on a sentence."""
    rooms = sorted({room_of(str(e.get("session"))) for e in pending})
    n = len(pending)
    count = f"{n} {'reply' if n == 1 else 'replies'} went unheard from {_join(rooms)}."
    if not cands:
        parts = [HEAD, count, "Nothing happened that needs you."]
    elif not body:
        parts = [HEAD, count, "None of them is a play that needs you."]
    else:
        parts = [HEAD, count, *([mind_note] if mind_note else []), *body]
    out: list[str] = []
    words = 0
    for p in parts:
        w = len(p.split())
        if out and words + w > max_words:
            break
        out.append(p)
        words += w
    return " ".join(out)


def _join(names: list[str]) -> str:
    if len(names) <= 2:
        return " and ".join(names) or "no room"
    if len(names) > 6:
        return f"{len(names)} rooms"
    return ", ".join(names[:-1]) + ", and " + names[-1]


def build(
    window: HoldWindow, cfg: dict, dropped_only: bool = False, now: float | None = None,
    mind: Callable[[str, str, str, float], str] = ask_mind, directory: Path = REEL_DIR,
) -> tuple[str, Path | None, str]:
    """Make one reel from what the window has pending: (words, file, how the mind did).

    The words are written to ``<UTC stamp>.txt`` before anything speaks them, and the reel is
    recorded in the window so the next one starts after it.
    """
    now = now or time.time()
    # Taken together, before the mind is asked: a reply noted while it thinks is the next reel's.
    pending, upto = window.pending_upto(dropped_only)
    cands = candidates(pending, int(cfg["max_candidates"]))
    body: list[str] = []
    note = ""
    url = str(cfg.get("mind_url") or "")
    if not cands:
        how = "none needed"
    elif not url:
        how = "off"
        body = fallback_lines(cands)
    elif not local_url(url):
        how = f"refused: {url} is not http on this machine"
        body = fallback_lines(cands)
    else:
        try:
            got = mind(url, str(cfg.get("mind_model") or ""), mind_prompt(cands), float(cfg["mind_timeout_s"]))
            body, dropped = ground(got, cands)
            how = f"ok, {len(body)} kept, {len(dropped)} dropped by the check"
            if not body:
                # Nothing the mind wrote held up (or it wrote nothing): the candidates are still
                # plays by the keyword pass, and "nothing needs you" would be a claim nobody made.
                note = "The local mind found nothing it could check, so here is what the rooms said."
                body = fallback_lines(cands)
        except (OSError, http.client.HTTPException, ValueError, KeyError, IndexError, TypeError) as e:
            how = f"no answer ({type(e).__name__}: {e})"
            note = "The local mind did not answer, so here is what the rooms said."
            body = fallback_lines(cands)
    words = compose(pending, cands, body, int(cfg["max_words"]), note)
    path: Path | None = directory / (datetime.fromtimestamp(now, timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".txt")
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path.write_text(words + "\n", encoding="utf-8")  # type: ignore[union-attr]
    except OSError:
        path = None
    window.record_reel(path, len(body), how, upto, now)
    return words, path, how


def local_url(url: str) -> bool:
    """Whether the mind's URL is http(s) on this machine; replies are never posted anywhere else.

    The config is on a directory sandboxes share, so a URL there is not trusted to stay local.
    """
    parts = urllib.parse.urlsplit(url)
    return parts.scheme in ("http", "https") and (parts.hostname or "") in ("127.0.0.1", "::1", "localhost")


def write_status(window: HoldWindow | None, cfg: dict, now: float | None = None, path: Path = STATUS_FILE) -> dict:
    """The hold as a reader sees it, written whole; {} written when no person's hold is on."""
    now = now or time.time()
    status: dict = {"checked_at": now}
    if window is not None and not window.released:
        last = window.last_reel_at()
        status.update({
            "held_since": window.since,
            "unheard": len(window.unheard()),
            "hold_cost": window.cost(),
            "reel_ready": bool(cfg.get("enabled")) and bool(due(window, cfg, now)),
            "reel_last_at": last,
        })
    try:
        atomic_write_json(path, status)
    except OSError:
        pass
    return status


def read_status(now: float | None = None, path: Path = STATUS_FILE) -> dict:
    """The status the daemon last wrote, or {} when there is none or it is stale."""
    try:
        data = json.loads(path.read_text("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    checked = data.get("checked_at")
    if not isinstance(checked, (int, float)) or (now or time.time()) - checked > STATUS_MAX_AGE_S:
        return {}
    return data


def describe_hold(status: dict, now: float | None = None) -> str:
    """The status line's words for a person's hold: its age, what it kept, what it cost; "" with none."""
    since = status.get("held_since")
    if not isinstance(since, (int, float)):
        return ""
    age_min = max(0, int(((now or time.time()) - since) // 60))
    age = f"{age_min // 60}h {age_min % 60}m" if age_min >= 60 else f"{age_min}m"
    cost = ", ".join(f"{k} {v}" for k, v in (status.get("hold_cost") or {}).items() if v)
    out = f"{age}, {int(status.get('unheard') or 0)} unheard" + (f" ({cost})" if cost else "")
    return out + (", TLDL reel ready" if status.get("reel_ready") else "")


class ReelWatch:
    """The daemon's side: notes what a person's hold keeps, and makes a reel when one is due.

    The play loop calls note_held() with the messages a hold keeps on each pass and
    note_outcome() for every outcome it writes; a thread ticks every TICK_EVERY_S, keeps the
    window for the current hold, writes status.json, and makes a reel off the play loop (the
    mind can take a minute). A reel is queued only when the hold lets the tldl session play
    and no reel is already waiting; a window whose hold ended gets one more reel for what the
    hold dropped, if one is due, and is closed.
    """

    def __init__(
        self,
        log_fn: Callable[[str, str], None],
        read_state: Callable[[], dict],
        may_play: Callable[[dict, str], bool],
        load_config: Callable[[], dict],
        enqueue: Callable[[dict], object],
        queued: Callable[[], list[dict]],
        directory: Path = REEL_DIR,
        mind: Callable[[str, str, str, float], str] = ask_mind,
        tick_every_s: float = TICK_EVERY_S,
    ) -> None:
        self._log = log_fn
        self._read_state = read_state
        self._may_play = may_play
        self._load_config = load_config
        self._enqueue = enqueue
        self._queued = queued
        self._dir = directory
        self._mind = mind
        self._tick_every_s = tick_every_s
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.window: HoldWindow | None = None

    @staticmethod
    def _person_hold(state: dict) -> float | None:
        since = state.get("paused_since")
        if not state.get("paused") or state.get("paused_by") != "user":
            return None
        if not isinstance(since, (int, float)) or isinstance(since, bool):
            return None
        return float(since)

    def note_held(self, state: dict, messages: list[dict]) -> None:
        """The play loop's pass under a hold: note each message the hold keeps."""
        since = self._person_hold(state)
        if since is None:
            return
        with self._lock:
            if self.window is None:
                self.window = HoldWindow(since, self._dir)
            window = self.window
        if window.since != since:
            return  # a new hold; the next tick closes the old window first
        for m in messages:
            window.note(m)

    def note_outcome(self, msg: dict, outcome: str, reason: str = "") -> None:
        window = self.window
        if window is None or window.released:
            return
        held = True
        if outcome == "dropped" and str(msg.get("id") or "") not in window.entries:
            # Not seen held: count it only if the hold keeps its room right now, so a let-through
            # room's stale reply, or a trim just after a release, is not the hold's cost.
            held = not self._may_play(self._read_state(), str(msg.get("session_id") or ""))
        window.note_outcome(msg, outcome, reason, held)

    def tick(self, now: float | None = None) -> str:
        """One round; returns the words of a reel it queued, else ""."""
        now = now or time.time()
        raw = self._load_config()
        cfg = settings(raw)
        with self._lock:
            # Read under the lock note_held opens a window under, so a hold that began after
            # an older read is never closed as it opens.
            state = self._read_state()
            since = self._person_hold(state)
            old = self.window
            if old is not None and (since is None or old.since != since):
                self.window = None
            else:
                old = None
            if since is not None and self.window is None:
                self.window = HoldWindow(since, self._dir)
            window = self.window
        spoken = ""
        if old is not None:
            if cfg["enabled"] and release_due(old):
                spoken = self._make(old, cfg, raw, now, dropped_only=True)
            old.mark_released()
        write_status(window, cfg, now, self._dir / STATUS_FILE.name)
        if (
            not spoken
            and window is not None
            and cfg["enabled"]
            and due(window, cfg, now)
            and self._may_play(state, REEL_SESSION)
            and not any(str(m.get("session_id") or "") == REEL_SESSION for m in self._queued())
        ):
            spoken = self._make(window, cfg, raw, now)
            write_status(window, cfg, now, self._dir / STATUS_FILE.name)
        return spoken

    def _make(self, window: HoldWindow, cfg: dict, raw: dict, now: float, dropped_only: bool = False) -> str:
        n = len(window.pending(dropped_only))
        words, path, how = build(window, cfg, dropped_only, now, self._mind, self._dir)
        persona = str(cfg.get("persona") or raw.get("active_persona") or "claude-prime")
        self._enqueue({"session_id": REEL_SESSION, "project": "TLDL", "name": "TLDL", "text": words, "persona": persona})
        where = "at the release" if dropped_only else "through the hold"
        self._log(f"TLDL reel queued {where}: {n} unheard, {len(words.split())} words, mind {how}; {path or 'not written'}", "INFO")
        return words

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="tts-reel", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001  a reel must never take the thread down
                self._log(f"TLDL reel round failed: {e}", "WARN")
            if self._stop.wait(self._tick_every_s):
                return
