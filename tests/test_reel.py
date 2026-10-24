"""The TLDL reel: a person's hold keeps the voices from them, and the reel tells them the plays.

JMO, 2026-10-24 (D269): highlights, not every first down. The test Haley set: a ledger of 50
replies with 3 plays makes a reel that names those 3 and none of the 47 first downs; the
control, 0 plays, makes a reel that says nothing happened that needs him.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_code_tts import reel
from claude_code_tts.bridge import _rfc3339, pause_view
from claude_code_tts.install import DEFAULT_CONFIG
from claude_code_tts.state import may_play

SINCE = 1_800_000_000.0

PLAYS = [
    ("alice--claude--cells", "PR #2058 merged after the second review, the cell builds from main."),
    ("alice--claude--sandboxes", "The stage cluster is blocked on the IAM role, nothing can start a sandbox."),
    ("alice--claude--jmo", "Card D271 needs your answer before the demo, it picks the region."),
]
FIRST_DOWNS = [
    "Taking the lint lane now, I will report when it is done.",
    "Rechecked the worktree, it is still clean at the same commit.",
    "On it, reading the note from the house room first.",
    "Progress: three of the five checks pass, two to go.",
    "Thanks, that is clear, I will start after the build slot opens.",
]


def _msg(i: int, session: str, text: str) -> dict:
    return {"id": f"m{i:03d}", "session_id": session, "project": session.rsplit("--", 1)[-1], "text": text,
            "timestamp": SINCE + 60 + i}


def ledger_of(n: int, plays: list[tuple[str, str]]) -> list[dict]:
    """n replies; the plays are spread through them, every other one a first down."""
    msgs = []
    at = {round((k + 1) * n / (len(plays) + 1)): p for k, p in enumerate(plays)}
    for i in range(n):
        if i in at:
            msgs.append(_msg(i, *at[i]))
        else:
            msgs.append(_msg(i, "alice--claude--notes", FIRST_DOWNS[i % len(FIRST_DOWNS)]))
    return msgs


def window_with(tmp_path: Path, msgs: list[dict]) -> reel.HoldWindow:
    w = reel.HoldWindow(SINCE, tmp_path)
    for m in msgs:
        assert w.note(m, now=m["timestamp"])
    return w


CFG = reel.settings({})
MIND_URL = "http://127.0.0.1:8642/v1/chat/completions"


class Mind:
    """A stand-in for the local mind: records each prompt and answers what it is told to."""

    def __init__(self, answer: str = "", fail: Exception | None = None) -> None:
        self.prompts: list[str] = []
        self.answer = answer
        self.fail = fail

    def __call__(self, url: str, model: str, prompt: str, timeout_s: float) -> str:
        self.prompts.append(prompt)
        if self.fail:
            raise self.fail
        return self.answer


# --- Haley's test and its control ----------------------------------------------------------


def test_fifty_replies_three_plays_the_reel_names_the_three_and_no_first_down(tmp_path: Path) -> None:
    msgs = ledger_of(50, PLAYS)
    w = window_with(tmp_path, msgs)
    mind = Mind(
        "Cells merged PR #2058, the cell builds from main. Sandboxes is blocked on the IAM role. "
        "The jmo room says card D271 needs your answer."
    )
    words, path, how = reel.build(w, {**CFG, "mind_url": MIND_URL}, now=SINCE + 7300, mind=mind, directory=tmp_path)

    assert len(mind.prompts) == 1
    for _, text in PLAYS:
        assert reel.first_sentence(text) in mind.prompts[0]
    for fd in FIRST_DOWNS:
        assert fd not in mind.prompts[0], "a first down reached the mind"
        assert fd not in words
    assert "#2058" in words and "IAM role" in words and "D271" in words
    assert words.startswith("Too long, didn't listen. 50 replies went unheard")
    assert path is not None and path.read_text() == words + "\n"
    assert how.startswith("ok, 3 kept")


def test_the_same_fifty_with_no_mind_speak_the_rooms_own_words(tmp_path: Path) -> None:
    w = window_with(tmp_path, ledger_of(50, PLAYS))
    words, _, how = reel.build(w, CFG, now=SINCE + 7300, directory=tmp_path)
    assert how == "off"
    for _, text in PLAYS:
        assert reel.first_sentence(text) in words
    for fd in FIRST_DOWNS:
        assert fd not in words


def test_control_zero_plays_says_nothing_needs_him_and_asks_no_mind(tmp_path: Path) -> None:
    w = window_with(tmp_path, ledger_of(50, []))
    mind = Mind("Cells merged PR #1.")
    words, _, how = reel.build(w, {**CFG, "mind_url": MIND_URL}, now=SINCE + 7300, mind=mind, directory=tmp_path)
    assert mind.prompts == []
    assert how == "none needed"
    assert words == "Too long, didn't listen. 50 replies went unheard from notes. Nothing happened that needs you."


def test_a_mind_that_does_not_answer_falls_back_to_the_rooms_words_and_says_so(tmp_path: Path) -> None:
    w = window_with(tmp_path, ledger_of(50, PLAYS))
    mind = Mind(fail=OSError("connection refused"))
    words, _, how = reel.build(w, {**CFG, "mind_url": MIND_URL}, now=SINCE + 7300, mind=mind, directory=tmp_path)
    assert how.startswith("no answer (OSError")
    assert "The local mind did not answer" in words
    assert reel.first_sentence(PLAYS[0][1]) in words


# --- the check on the mind's facts ----------------------------------------------------------


def test_a_shipped_claim_the_rooms_never_made_is_dropped_not_reworded() -> None:
    # 10-24: the mind said docker/sandboxes#7503 was merged while the room said it was open.
    cands = [{"session": "a--claude--sandboxes", "play": "PR #7503 is blocked on review, still open."},
             {"session": "a--claude--cells", "play": "PR #2058 merged."}]
    kept, dropped = reel.ground(
        "Sandboxes merged PR #7503. Cells merged PR #2058. Geordi released v9.99.0. "
        "Sandboxes is blocked on review of PR #7503.",
        cands,
    )
    assert kept == ["Cells merged PR #2058.", "Sandboxes is blocked on review of PR #7503."]
    assert dropped == ["Sandboxes merged PR #7503.", "Geordi released v9.99.0."]


def test_a_number_must_match_whole_and_pr_without_a_hash_is_checked_too() -> None:
    cands = [{"session": "a--claude--x", "play": "PR #12049 merged and 9.73.4 tagged."}]
    kept, dropped = reel.ground("PR 2049 merged. Version v9.73.4 tagged. PR #12049 merged.", cands)
    assert kept == ["Version v9.73.4 tagged.", "PR #12049 merged."]
    assert dropped == ["PR 2049 merged."]


def test_repeated_sentences_and_none_go() -> None:
    cands = [{"session": "a--claude--x", "play": "The build is broken on main."}]
    kept, _ = reel.ground("The build is broken. The build is broken. NONE.", cands)
    assert kept == ["The build is broken."]


# --- when a reel is due ----------------------------------------------------------------------


def test_due_at_fifty_unheard_or_two_hours_and_never_twice_in_an_hour(tmp_path: Path) -> None:
    w = window_with(tmp_path, ledger_of(49, PLAYS))
    assert reel.due(w, CFG, SINCE + 600) == ""
    w.note(_msg(49, "alice--claude--x", "one more first down"))
    assert reel.due(w, CFG, SINCE + 600) == "count"
    reel.build(w, CFG, now=SINCE + 600, directory=tmp_path)
    assert reel.due(w, CFG, SINCE + 700) == "", "nothing pending after the reel"
    for i in range(50, 120):
        w.note(_msg(i, "alice--claude--x", "more first downs"))
    assert reel.due(w, CFG, SINCE + 600 + 3599) == "", "within the hour of the last reel"
    assert reel.due(w, CFG, SINCE + 600 + 3600) == "count"


def test_due_by_age_with_few_unheard(tmp_path: Path) -> None:
    w = window_with(tmp_path, ledger_of(3, []))
    assert reel.due(w, CFG, SINCE + 7199) == ""
    assert reel.due(w, CFG, SINCE + 7200) == "age"


def test_a_played_reply_is_not_unheard(tmp_path: Path) -> None:
    msgs = ledger_of(5, [])
    w = window_with(tmp_path, msgs)
    w.note_outcome(msgs[0], "played")
    w.note_outcome(msgs[1], "dropped", "depth")
    w.note_outcome(msgs[2], "dropped", "superseded")
    assert len(w.unheard()) == 4
    assert w.cost() == {"trimmed": 1, "stale": 1, "expired": 0, "dropped": 0}
    assert [e["id"] for e in w.pending(dropped_only=True)] == ["m001"], "a stale reply is never told"


# --- the window on disk ----------------------------------------------------------------------


def test_the_window_survives_a_restart_and_keeps_no_words_of_a_first_down(tmp_path: Path) -> None:
    msgs = ledger_of(10, PLAYS[:1])
    w = window_with(tmp_path, msgs)
    w.note_outcome(msgs[0], "dropped", "depth")
    reel.build(w, CFG, now=SINCE + 7300, directory=tmp_path)
    again = reel.HoldWindow(SINCE, tmp_path)
    assert again.order == w.order and again.outcomes == w.outcomes and len(again.reels) == 1
    raw = (tmp_path / f"hold-{SINCE:.0f}.jsonl").read_text()
    for fd in FIRST_DOWNS:
        assert fd not in raw
    assert reel.first_sentence(PLAYS[0][1]) in raw


def test_a_reel_bridge_or_control_message_is_never_noted(tmp_path: Path) -> None:
    w = reel.HoldWindow(SINCE, tmp_path)
    assert not w.note({"id": "r", "session_id": reel.REEL_SESSION, "text": "merged"})
    assert not w.note({"id": "b", "session_id": "page", "source": "page", "text": "merged"})
    assert not w.note({"id": "c", "session_id": "system", "type": "control"})
    assert w.unheard() == []


# --- the daemon's watch ----------------------------------------------------------------------


class Daemon:
    """What the watch needs from the daemon, in memory."""

    def __init__(self, state: dict, config: dict | None = None) -> None:
        self.state = state
        self.config = config or {}
        self.queue: list[dict] = []
        self.logs: list[str] = []

    def watch(self, tmp_path: Path, mind: Mind | None = None) -> reel.ReelWatch:
        return reel.ReelWatch(
            log_fn=lambda m, _lvl: self.logs.append(m),
            read_state=lambda: self.state,
            may_play=lambda s, session: may_play(s, session),
            load_config=lambda: self.config,
            enqueue=self.queue.append,
            queued=lambda: self.queue,
            directory=tmp_path,
            mind=mind or Mind(),
        )


def test_a_full_hold_keeps_the_reel_and_a_hold_that_lets_tldl_through_plays_it(tmp_path: Path) -> None:
    d = Daemon({"paused": True, "paused_by": "user", "paused_since": SINCE, "let_through": []})
    w = d.watch(tmp_path)
    w.note_held(d.state, ledger_of(50, PLAYS))
    assert w.tick(SINCE + 600) == ""
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["unheard"] == 50 and status["reel_ready"] is True and d.queue == []

    d.state["let_through"] = ["tldl"]
    words = w.tick(SINCE + 630)
    assert words and len(d.queue) == 1
    assert d.queue[0]["session_id"] == "tldl" and d.queue[0]["text"] == words
    assert d.queue[0]["persona"] == "claude-prime"
    assert w.tick(SINCE + 660) == "", "one reel waits; no second one"
    d.queue.clear()
    assert w.tick(SINCE + 690) == "", "nothing new since the reel"


def test_a_recording_under_the_hold_keeps_the_reel(tmp_path: Path) -> None:
    d = Daemon({"paused": True, "paused_by": "user", "paused_since": SINCE, "let_through": ["tldl"], "mic_held": True})
    w = d.watch(tmp_path)
    w.note_held(d.state, ledger_of(50, PLAYS))
    assert w.tick(SINCE + 600) == "" and d.queue == []


def test_the_release_makes_one_reel_of_what_the_hold_dropped(tmp_path: Path) -> None:
    d = Daemon({"paused": True, "paused_by": "user", "paused_since": SINCE, "let_through": []})
    w = d.watch(tmp_path)
    msgs = ledger_of(60, PLAYS)
    w.note_held(d.state, msgs)
    w.tick(SINCE + 600)
    for m in msgs[:55]:
        w.note_outcome(m, "dropped", "kraken")
    d.state = {"paused": False}
    words = w.tick(SINCE + 660)
    assert words.startswith("Too long, didn't listen. 55 replies went unheard")
    assert len(d.queue) == 1
    assert json.loads((tmp_path / "status.json").read_text()).get("unheard") is None
    assert w.window is None
    assert w.tick(SINCE + 690) == ""


def test_a_mic_hold_opens_no_window(tmp_path: Path) -> None:
    d = Daemon({"paused": True, "paused_by": "mic", "paused_since": SINCE})
    w = d.watch(tmp_path)
    w.note_held(d.state, ledger_of(50, PLAYS))
    assert w.window is None and w.tick(SINCE + 600) == ""


def test_disabled_makes_no_reel(tmp_path: Path) -> None:
    d = Daemon({"paused": True, "paused_by": "user", "paused_since": SINCE, "let_through": ["tldl"]},
               {"reel": {"enabled": False}})
    w = d.watch(tmp_path)
    w.note_held(d.state, ledger_of(50, PLAYS))
    assert w.tick(SINCE + 600) == "" and d.queue == []


# --- readers ---------------------------------------------------------------------------------


def test_pause_view_carries_the_hold_and_the_reel_for_the_same_hold_only() -> None:
    state = {"paused": True, "paused_by": "user", "paused_since": SINCE}
    status = {"held_since": SINCE, "unheard": 312, "hold_cost": {"trimmed": 290, "stale": 12},
              "reel_ready": True, "reel_last_at": SINCE + 3600, "checked_at": SINCE + 4000}
    view = pause_view(state, 0.0, None, status)
    assert view["unheard"] == 312 and view["hold_cost"] == {"trimmed": 290, "stale": 12}
    assert view["reel_ready"] is True and view["reel_last_at"] == _rfc3339(SINCE + 3600)
    assert isinstance(view["held_for_s"], int)
    other = pause_view({**state, "paused_since": SINCE + 99}, 0.0, None, status)
    assert other["unheard"] == 0 and other["reel_ready"] is False and other["reel_last_at"] is None


def test_status_line_words_and_a_stale_status_reads_as_none(tmp_path: Path) -> None:
    status = {"held_since": SINCE, "unheard": 312, "hold_cost": {"trimmed": 290, "stale": 12, "expired": 0},
              "reel_ready": True}
    assert reel.describe_hold(status, now=SINCE + 7500) == "2h 5m, 312 unheard (trimmed 290, stale 12), TLDL reel ready"
    assert reel.describe_hold({}) == ""
    path = tmp_path / "status.json"
    path.write_text(json.dumps({**status, "checked_at": SINCE}))
    assert reel.read_status(now=SINCE + 60, path=path)["unheard"] == 312
    assert reel.read_status(now=SINCE + 301, path=path) == {}


def test_the_shipped_config_carries_the_reel_defaults() -> None:
    assert DEFAULT_CONFIG["reel"] == reel.DEFAULTS


@pytest.mark.parametrize("text", [t for _, t in PLAYS])
def test_each_play_is_a_play(text: str) -> None:
    assert reel.is_play(text)


@pytest.mark.parametrize("text", FIRST_DOWNS)
def test_each_first_down_is_not(text: str) -> None:
    assert not reel.is_play(text)


# --- the play loop notes what the hold keeps ---------------------------------------------------


def test_the_loop_notes_every_reply_a_hold_keeps_and_what_it_cost(tmp_path: Path) -> None:
    import threading

    import claude_code_tts.daemon as d
    import claude_code_tts.state as st
    from tests import loop_passes
    from tests.test_hold import _enqueue, _ledger_helpers, _wait_for

    loop_harness, stop_loop = _ledger_helpers()
    state_dir, queue_dir, spoken, patches = loop_harness(tmp_path, {})
    for p in patches:
        p.start()
    try:
        st.write_playback_state(paused=False, paused_by=None, audio_pid=None, current_message=None)
        st.hold(["b"])

        def run() -> None:
            d._shutdown_requested = False
            d._daemon_mode = True
            d.daemon_loop()

        runner = threading.Thread(target=run, daemon=True)
        runner.start()
        assert _wait_for(lambda: (state_dir / "daemon.heartbeat").exists())
        for i in range(24):
            _enqueue(queue_dir, f"a held {i}", "alice--claude--a")
        _enqueue(queue_dir, "PR #7 merged, the cell is green.", "alice--claude--a")
        _enqueue(queue_dir, "b speaks", "alice--claude--b")
        assert _wait_for(lambda: "b speaks" in spoken)
        assert _wait_for(lambda: len(list(queue_dir.glob("*.json"))) == 20)
        assert loop_passes.wait_passes(3), "the loop stopped passing"
        window = d._REEL.window if d._REEL else None
        assert window is not None
        assert len(window.unheard()) == 25, "every reply of a's, the five trimmed too; none of b's"
        assert window.cost()["trimmed"] == 5
        assert [e["play"] for e in window.entries.values() if e.get("play")] == ["PR #7 merged, the cell is green."]
    finally:
        stop_loop(runner)
        for p in reversed(patches):
            p.stop()


# --- the review's cases (2026-10-24) -----------------------------------------------------------


def test_negation_another_number_bare_numbers_and_unnumbered_shipped_claims_are_dropped() -> None:
    cands = [{"session": "a--claude--sandboxes", "play": "PR #7503 is not merged yet."},
             {"session": "a--claude--cells", "play": "PR #2058 merged; #2059 still failing on lint."}]
    kept, dropped = reel.ground(
        "Sandboxes merged PR #7503. Cells merged #2059. Cells merged 2060. "
        "PRs 2061 and 2062 merged. Geordi released the kit. Cells merged #2058. "
        "PR #7503 is not merged yet.",
        cands,
    )
    assert kept == ["Cells merged #2058.", "PR #7503 is not merged yet."]
    assert dropped == ["Sandboxes merged PR #7503.", "Cells merged #2059.", "Cells merged 2060.",
                       "PRs 2061 and 2062 merged.", "Geordi released the kit."]


def test_prs_named_together_are_each_checked() -> None:
    cands = [{"session": "a--claude--x", "play": "PRs 2061 and 2062 merged this morning."}]
    assert reel.ground("PRs 2061 and 2062 merged.", cands)[0] == ["PRs 2061 and 2062 merged."]
    assert reel.ground("PRs 2061 and 2063 merged.", cands)[0] == []
    after = [{"session": "a--claude--x", "play": "Merged #2058 and #2059 still failing."}]
    assert reel.ground("Merged #2058. Merged #2059.", after) == (["Merged #2058."], ["Merged #2059."])


@pytest.mark.parametrize("answer", ["", "NONE", "Sandboxes merged PR #9999."])
def test_a_mind_that_says_nothing_checkable_falls_back_to_the_rooms_words(tmp_path: Path, answer: str) -> None:
    w = window_with(tmp_path, ledger_of(50, PLAYS))
    words, _, _ = reel.build(w, {**CFG, "mind_url": MIND_URL}, now=SINCE + 7300, mind=Mind(answer), directory=tmp_path)
    assert "found nothing it could check" in words
    assert reel.first_sentence(PLAYS[2][1]) in words
    assert "None of them" not in words and "Nothing happened" not in words


def test_a_reply_noted_while_the_mind_thinks_is_the_next_reels(tmp_path: Path) -> None:
    w = window_with(tmp_path, ledger_of(50, PLAYS))

    class Slow(Mind):
        def __call__(self, url: str, model: str, prompt: str, timeout_s: float) -> str:
            w.note(_msg(500, "alice--claude--jmo", "Card D300 needs your answer today."))
            return "Sandboxes is blocked on the IAM role."

    reel.build(w, {**CFG, "mind_url": MIND_URL}, now=SINCE + 7300, mind=Slow(), directory=tmp_path)
    assert [e["id"] for e in w.pending()] == ["m500"]


def test_an_http_protocol_error_is_a_mind_that_did_not_answer(tmp_path: Path) -> None:
    import http.client

    w = window_with(tmp_path, ledger_of(50, PLAYS))
    mind = Mind(fail=http.client.IncompleteRead(b""))
    _, _, how = reel.build(w, {**CFG, "mind_url": MIND_URL}, now=SINCE + 7300, mind=mind, directory=tmp_path)
    assert how.startswith("no answer (IncompleteRead")


@pytest.mark.parametrize("url, ok", [
    (MIND_URL, True), ("http://localhost:8642/v1/chat/completions", True), ("http://[::1]:8642/x", True),
    ("https://example.com/v1/chat/completions", False), ("file:///etc/passwd", False),
    ("http://10.0.0.2:8642/x", False), ("http://127.0.0.1.example.com/x", False),
])
def test_the_mind_is_asked_only_on_this_machine(url: str, ok: bool) -> None:
    assert reel.local_url(url) is ok


def test_a_refused_url_is_never_asked(tmp_path: Path) -> None:
    w = window_with(tmp_path, ledger_of(50, PLAYS))
    mind = Mind("anything")
    _, _, how = reel.build(w, {**CFG, "mind_url": "https://example.com/x"}, now=SINCE + 7300, mind=mind, directory=tmp_path)
    assert mind.prompts == [] and how.startswith("refused")


def test_a_hold_file_line_not_of_our_shape_is_skipped(tmp_path: Path) -> None:
    (tmp_path / f"hold-{SINCE:.0f}.jsonl").write_text(
        '{"kind":"held","id":["x"]}\n{"kind":"outcome","id":{"a":1}}\n[1,2]\n{"kind":"held","id":"ok","session":"a--b--c"}\n'
    )
    w = reel.HoldWindow(SINCE, tmp_path)
    assert w.order == ["ok"]


def test_a_short_hold_that_dropped_a_few_first_downs_is_quiet_at_the_release(tmp_path: Path) -> None:
    d = Daemon({"paused": True, "paused_by": "user", "paused_since": SINCE, "let_through": []})
    w = d.watch(tmp_path)
    msgs = ledger_of(3, [])
    w.note_held(d.state, msgs)
    for m in msgs:
        w.note_outcome(m, "dropped", "kraken")
    d.state = {"paused": False}
    assert w.tick(SINCE + 300) == "" and d.queue == []


def test_one_dropped_play_is_told_at_the_release_however_short_the_hold(tmp_path: Path) -> None:
    d = Daemon({"paused": True, "paused_by": "user", "paused_since": SINCE, "let_through": []})
    w = d.watch(tmp_path)
    msg = _msg(1, *PLAYS[0])
    w.note_held(d.state, [msg])
    w.note_outcome(msg, "dropped", "kraken")
    d.state = {"paused": False}
    words = w.tick(SINCE + 300)
    assert "1 reply went unheard from cells" in words and "#2058" in words


def test_a_let_through_rooms_stale_reply_is_not_the_holds_cost(tmp_path: Path) -> None:
    d = Daemon({"paused": True, "paused_by": "user", "paused_since": SINCE, "let_through": ["b"]})
    w = d.watch(tmp_path)
    w.note_held(d.state, [])
    w.note_outcome(_msg(1, "alice--claude--b", "b's old reply"), "dropped", "superseded")
    w.note_outcome(_msg(2, "alice--claude--a", "a's old reply"), "dropped", "superseded")
    assert w.window is not None and w.window.order == ["m002"]


def test_a_reel_error_never_costs_the_ledger_its_line(monkeypatch: pytest.MonkeyPatch) -> None:
    import claude_code_tts.daemon as d

    class Broken:
        def note_outcome(self, *_a: object) -> None:
            raise TypeError("planted")

    written: list[tuple] = []
    monkeypatch.setattr(d, "_REEL", Broken())
    monkeypatch.setattr(d.voice_ledger, "record_outcome", lambda *a, **k: written.append(a) or True)
    d._outcome({"id": "x1", "session_id": "a--b--c"}, "played")
    assert written and written[0][:3] == ("a--b--c", "x1", "played")


def test_kraken_drop_spares_a_waiting_reel(tmp_path: Path) -> None:
    import argparse

    import claude_code_tts.msgqueue as mq
    from claude_code_tts import cli

    mq.write_message({"session_id": "tldl", "text": "the reel"})
    mq.write_message({"id": "h1", "session_id": "alice--claude--a", "text": "held"})
    names: list[list[str]] = []
    real = mq.write_drop_all_message
    try:
        mq.write_drop_all_message = lambda ids, log=None: names.append(list(ids)) or real(ids, log)  # type: ignore[assignment]
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("claude_code_tts.state.is_daemon_running", lambda: (False, None))
            mp.setattr("claude_code_tts.state.release", lambda: {"paused": False})
            cli.cmd_kraken(argparse.Namespace(drop=True))
    finally:
        mq.write_drop_all_message = real  # type: ignore[assignment]
        for f in mq.QUEUE_DIR.glob("*.json"):
            f.unlink()
    assert names == [["h1"]]
