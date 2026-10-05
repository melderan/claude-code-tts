"""Twin hooks in threads, with the step order fixed: the orders that only a true interleave reaches.

House review of finding 6, round five (Geordi, 2026-10-02): two hooks run in threads, and the
wrapped calls named in a test's `seq` run in exactly that order, each completing before the next
starts, while unlisted calls run freely. N1 found the claim-loser's speech moving the watermark
past the winner's reply; b1-b4 pin the orders that keep the not-owned drop branch; m5b pins the
owned span's lower edge.
"""
from __future__ import annotations

import argparse
import json
import threading
from unittest.mock import patch

import pytest

import tests.test_spoken as ts
from claude_code_tts import cli, spoken
from claude_code_tts.config import TTSConfig
from tests.test_spoken import (
    R3_FINAL,
    R3_INTER,
    R4_REPLY,
    _append,
    _assistant_msg,
    _hook,
    _stop_spoke_final,
    _tail_stop,
    _tool_result,
    _tool_use,
    _user,
)

fake_state_dir = ts.fake_state_dir  # the shared fixture, registered under its own name here

X_TEXT = "the new turn's first text before its next tool call"


LOG = []


class Sched:
    """Threads run hooks; wrapped calls listed in `seq` run in exactly that order (each completes
    before the next starts); unlisted calls run freely."""

    def __init__(self, seq):
        self.seq = list(seq)
        self.cv = threading.Condition()
        self.err = None
        self.dropped: list = []

    def gate(self, name, real):
        def w(*a, **k):
            me = threading.current_thread().name
            key = (me, name)
            LOG.append(key)
            with self.cv:
                if key not in self.seq:
                    pass
                else:
                    ok = self.cv.wait_for(lambda: self.seq and self.seq[0] == key, timeout=5)
                    if not ok:
                        self.err = f"deadlock at {key}, next {self.seq[:1]}"
                        self.seq.clear()
                        self.cv.notify_all()
                        return real(*a, **k)
                    try:
                        return real(*a, **k)
                    finally:
                        self.seq.pop(0)
                        self.cv.notify_all()
            return real(*a, **k)
        return w

    def done(self, name):
        """A hook finished; steps of its that were never reached are dropped and remembered,
        so a test whose order names a step the code no longer takes fails instead of passing
        on an order it never pinned (Geordi, round six)."""
        with self.cv:
            self.dropped += [k for k in self.seq if k[0] == name]
            self.seq[:] = [k for k in self.seq if k[0] != name]
            self.cv.notify_all()


def run_threads(monkeypatch, transcript, hooks, seq, hold=False):
    """hooks: {name: (hook_type, payload extras)}. Returns [(name, text)] in speech order."""
    LOG.clear()  # a deadlock message names this run's steps, not every test's before it
    names = list(hooks)
    reads = [(n, "_read_watermark") for n in names] + [(n, "_read_pending_record") for n in names]
    sched = Sched(reads + list(seq))
    said = []
    lock = threading.Lock()
    payloads = {}
    for name, (_ht, extra) in hooks.items():
        p = {"transcript_path": str(transcript), "tool_name": "Bash", "session_id": "sess-1"}
        p.update(extra)
        payloads[name] = json.dumps(p)

    class Stdin:
        def read(self):
            return payloads[threading.current_thread().name]

    def speak(t, cfg):
        with lock:
            said.append((threading.current_thread().name, t))

    for fn in ("_read_watermark", "_read_pending_record", "_take_landed", "_claim_lines", "_claim_watermark", "_write_watermark"):
        monkeypatch.setattr(cli, fn, sched.gate(fn, getattr(cli, fn)))
    monkeypatch.setattr(spoken, "claim", sched.gate("claim", spoken.claim))
    if hold:
        monkeypatch.setattr(spoken.Claim, "release", lambda self: None)
    cfg = TTSConfig(mode="direct", muted=False, intermediate=True, session_id="-Users-dev", project_name="home")
    with patch("sys.stdin", Stdin()), patch("claude_code_tts.cli.load_config", return_value=cfg), \
         patch("claude_code_tts.audio.speak", side_effect=speak), patch("claude_code_tts.session.pin_session"), \
         patch("claude_code_tts.cli.time.sleep", side_effect=lambda s: threading.Event().wait(s) if s <= 0.05 else None):
        errs = []

        def body(ht):
            try:
                cli._speak_from_hook(argparse.Namespace(hook_type=ht))
            except Exception as e:  # pragma: no cover
                errs.append(repr(e))
            finally:
                sched.done(threading.current_thread().name)

        ts = [threading.Thread(target=body, args=(ht,), name=n) for n, (ht, _) in hooks.items()]
        for t in ts:
            t.start()
        for t in ts:
            t.join(10)
    assert not errs, errs
    assert sched.err is None, (sched.err, LOG)
    assert not sched.seq, f"steps never reached: {sched.seq}"
    assert not sched.dropped, f"steps a hook finished without taking: {sched.dropped}"
    return said


def heard(said):
    return " ".join(t for _, t in said)


STOP = {"last_assistant_message": R4_REPLY, "prompt_id": "p2"}


# --- N1: the claim-loser's speech moves the watermark past the winner's unclaimed reply ----------

def test_n1_stop_owner_loses_claim_speaks_owned_then_winner_drops_the_reply_line(tmp_path, fake_state_dir, monkeypatch):
    """Reply already in the file. H1 takes (owner), H2 takes (nothing), H2 wins prompt:p2, H1 loses
    and speaks the INTER it owns without moving the watermark (round six); H2's _claim_lines then
    claims the reply once. INTER once, the reply once, the landed final never."""
    t = _stop_spoke_final(tmp_path, "n1")
    _tail_stop(t)
    _append(t, [_assistant_msg("m4", R4_REPLY)])
    said = run_threads(monkeypatch, t, {"H1": ("stop", STOP), "H2": ("stop", STOP)},
                       [("H1", "_take_landed"), ("H2", "_take_landed"), ("H2", "claim"), ("H1", "claim"),
                        ("H1", "_claim_lines"), ("H2", "_claim_lines")])
    h = heard(said)
    assert h.count(R3_FINAL) == 0 and h.count(R3_INTER) == 1 and h.count(R4_REPLY) == 1, said


def test_n1_control_winner_speaks_first(tmp_path, fake_state_dir, monkeypatch):
    t = _stop_spoke_final(tmp_path, "n1c")
    _tail_stop(t)
    _append(t, [_assistant_msg("m4", R4_REPLY)])
    said = run_threads(monkeypatch, t, {"H1": ("stop", STOP), "H2": ("stop", STOP)},
                       [("H1", "_take_landed"), ("H2", "_take_landed"), ("H2", "claim"), ("H1", "claim"),
                        ("H2", "_claim_lines"), ("H1", "_claim_lines")])
    h = heard(said)
    assert h.count(R3_FINAL) == 0 and h.count(R3_INTER) == 1 and h.count(R4_REPLY) == 1, said


def test_n1_ptu_owner_loses_claim_then_winner_cannot_claim_its_text(tmp_path, fake_state_dir, monkeypatch):
    """PostToolUse twins, new-turn text X after the prompt. H1 owns INTER; both claim message:m4 (X);
    H2 wins; H1 speaks INTER and writes nothing (round six); H2's _claim_watermark(X) succeeds."""
    t = _stop_spoke_final(tmp_path, "n1p")
    _append(t, [_assistant_msg("m2", R3_INTER), _tool_use("m2"), _tool_result(),
                _assistant_msg("m3", R3_FINAL), _user("next"), _assistant_msg("m4", X_TEXT),
                _tool_use("m4"), _tool_result()])
    P = {"prompt_id": "p2"}
    said = run_threads(monkeypatch, t, {"H1": ("post_tool_use", P), "H2": ("post_tool_use", P)},
                       [("H1", "_take_landed"), ("H2", "_take_landed"), ("H2", "claim"), ("H1", "claim"),
                        ("H2", "_claim_watermark")])  # the loser writes no watermark since round six
    h = heard(said)
    assert h.count(R3_FINAL) == 0 and h.count(R3_INTER) == 1 and h.count(X_TEXT) == 1, said


def test_n2_ptu_owner_that_wins_speaks_the_owned_lines_before_its_last_text(tmp_path, fake_state_dir):
    """A PostToolUse speaks its last text; the owned intermediate before a newer X was never said
    until round six, where it reads the lines its take moved past first, as a Stop does."""
    t = _stop_spoke_final(tmp_path, "n2")
    _append(t, [_assistant_msg("m2", R3_INTER), _tool_use("m2"), _tool_result(),
                _assistant_msg("m3", R3_FINAL), _user("next"), _assistant_msg("m4", X_TEXT),
                _tool_use("m4"), _tool_result()])
    h1 = _hook(t, "post_tool_use", prompt="p2")
    _append(t, [_assistant_msg("m5", R4_REPLY)])
    st = _hook(t, "stop", text=R4_REPLY, prompt="p2")
    h = " ".join(h1 + st)
    assert h.count(R3_INTER) == 1 and h.count(X_TEXT) == 1 and h.count(R3_FINAL) == 0, (h1, st)


# --- item 3: orders for the not-owned drop branch ------------------------------------------------

def test_b1_nonowner_ptu_vs_owner_stop(tmp_path, fake_state_dir, monkeypatch):
    t = _stop_spoke_final(tmp_path, "b1")
    _append(t, [_assistant_msg("m2", R3_INTER), _assistant_msg("m3", R3_FINAL), _user("next"),
                _tool_use("m4"), _tool_result()])
    said = run_threads(monkeypatch, t, {"S": ("stop", STOP), "P": ("post_tool_use", {"prompt_id": "p2"})},
                       # P owns nothing after S's take and has no text left, so it never claims
                       [("S", "_take_landed"), ("P", "_take_landed"), ("S", "claim"), ("S", "_claim_lines")])
    h = heard(said)
    assert h.count(R3_FINAL) == 0 and h.count(R3_INTER) == 1 and h.count(R4_REPLY) == 1, said


@pytest.mark.parametrize("hold", [False, True])
def test_b2_nonowner_stop_claims_first_and_holds(tmp_path, fake_state_dir, monkeypatch, hold):
    t = _stop_spoke_final(tmp_path, f"b2{hold}")
    _tail_stop(t)
    said = run_threads(monkeypatch, t, {"H1": ("stop", STOP), "H2": ("stop", STOP)},
                       [("H1", "_take_landed"), ("H2", "_take_landed"), ("H2", "claim"), ("H1", "claim"),
                        ("H1", "_claim_lines"), ("H2", "_claim_lines")], hold=hold)
    h = heard(said)
    assert h.count(R3_FINAL) == 0 and h.count(R3_INTER) == 1 and h.count(R4_REPLY) == 1, said


def test_b3_watermark_writes_race_nonowner_first(tmp_path, fake_state_dir, monkeypatch):
    t = _stop_spoke_final(tmp_path, "b3")
    _tail_stop(t)
    said = run_threads(monkeypatch, t, {"H1": ("stop", STOP), "H2": ("stop", STOP)},
                       [("H1", "_take_landed"), ("H2", "_take_landed"), ("H2", "claim"), ("H2", "_claim_lines"),
                        ("H1", "claim"), ("H1", "_claim_lines")])
    h = heard(said)
    assert h.count(R3_FINAL) == 0 and h.count(R3_INTER) == 1 and h.count(R4_REPLY) == 1, said


def test_b4_third_hook_writes_the_watermark_back(tmp_path, fake_state_dir, monkeypatch):
    """A lagged hook with nothing new writes its old current_lines (below INTER) between the
    non-owner's take and its _claim_lines. Stand-in: the raw write of the old watermark."""
    t = _stop_spoke_final(tmp_path, "b4")
    old = cli._count_lines(t)
    _tail_stop(t)
    real_cl = cli._claim_lines

    def cl(state_file, lock_dir, *a, **k):
        if threading.current_thread().name == "H2":
            real_ww(state_file, lock_dir, old)  # H3's 'no new lines' write
        return real_cl(state_file, lock_dir, *a, **k)
    real_ww = cli._write_watermark
    monkeypatch.setattr(cli, "_claim_lines", cl)
    said = run_threads(monkeypatch, t, {"H1": ("stop", STOP), "H2": ("stop", STOP)},
                       [("H1", "_take_landed"), ("H2", "_take_landed"), ("H2", "claim"), ("H2", "_claim_lines"),
                        ("H1", "claim"), ("H1", "_claim_lines")], hold=True)
    h = heard(said)
    assert h.count(R3_FINAL) == 0 and h.count(R3_INTER) == 1 and h.count(R4_REPLY) == 1, said


def test_m5b_lagged_claim_ending_right_after_the_intermediate_is_not_owned(tmp_path, fake_state_dir, monkeypatch):
    """As test_lagged_ptu_claimed_inter_before_stop_take_no_double, but the lagged claim's watermark
    lands on the line right after the intermediate: owned_from - 1 would take it back."""
    t = _stop_spoke_final(tmp_path, "m5b")
    start = cli._count_lines(t)
    _append(t, [_assistant_msg("m2", R3_INTER), _tool_use("m2"), _tool_result(),
                _assistant_msg("m3", R3_FINAL), _user("next")])
    said = []
    real = cli._take_landed

    def w(state_file, lock_dir, *a, **k):
        if not said:
            assert cli._claim_watermark(state_file, lock_dir, start, start + 1)
            said.append(R3_INTER)
        return real(state_file, lock_dir, *a, **k)
    monkeypatch.setattr(cli, "_take_landed", w)
    st = _hook(t, "stop", text=R4_REPLY, prompt="p2")
    assert " ".join(said + st).count(R3_INTER) == 1, (said, st)

# --- Round six (Geordi, 2026-10-02): adversarial orders on the claim-loser path ---------------


def _ptu_tail(t):
    _append(t, [_assistant_msg("m2", R3_INTER), _tool_use("m2"), _tool_result(),
                _assistant_msg("m3", R3_FINAL), _user("next"), _assistant_msg("m4", X_TEXT),
                _tool_use("m4"), _tool_result()])


def test_adv_ptu_loser_speaks_after_winner(tmp_path, fake_state_dir, monkeypatch):
    t = _stop_spoke_final(tmp_path, "a1")
    _ptu_tail(t)
    P = {"prompt_id": "p2"}
    said = run_threads(monkeypatch, t, {"H1": ("post_tool_use", P), "H2": ("post_tool_use", P)},
                       [("H1", "_take_landed"), ("H2", "_take_landed"), ("H2", "claim"),
                        ("H2", "_claim_watermark"), ("H1", "claim")])
    h = heard(said)
    assert h.count(R3_FINAL) == 0 and h.count(R3_INTER) == 1 and h.count(X_TEXT) == 1, said
    assert [n for n, x in said if R3_INTER in x] == ["H1"], said


def test_adv_stop_loser_with_empty_owned_span_speaks_nothing(tmp_path, fake_state_dir, monkeypatch):
    t = _stop_spoke_final(tmp_path, "a2")
    start = cli._count_lines(t)
    _append(t, [_assistant_msg("m2", R3_INTER), _tool_use("m2"), _tool_result(),
                _assistant_msg("m3", R3_FINAL), _user("next")])
    raw = []
    real = cli._take_landed

    def w(state_file, lock_dir, *a, **k):
        if threading.current_thread().name == "H1" and not raw:
            assert cli._claim_watermark(state_file, lock_dir, start, start + 1)
            raw.append(R3_INTER)
        return real(state_file, lock_dir, *a, **k)
    monkeypatch.setattr(cli, "_take_landed", w)
    said = run_threads(monkeypatch, t, {"H1": ("stop", STOP), "H2": ("stop", STOP)},
                       # H1's owned span holds no text, so after losing the claim it speaks nothing
                       [("H1", "_take_landed"), ("H2", "_take_landed"), ("H2", "claim"), ("H1", "claim"),
                        ("H2", "_claim_lines")])
    assert [x for n, x in said if n == "H1"] == [], said
    h = " ".join(raw) + " " + heard(said)
    assert h.count(R3_INTER) == 1 and h.count(R4_REPLY) == 1 and h.count(R3_FINAL) == 0, (raw, said)


def _stale(monkeypatch, t, who):
    old = cli._count_lines(t)
    real_cl = cli._claim_lines
    real_ww = cli._write_watermark

    def cl(state_file, lock_dir, *a, **k):
        if threading.current_thread().name == who:
            real_ww(state_file, lock_dir, old)  # lagged H3: 'no new lines' writes its old count
        return real_cl(state_file, lock_dir, *a, **k)
    monkeypatch.setattr(cli, "_claim_lines", cl)


def test_adv_three_hooks_stale_write_before_winner_claim_lines_n1_order(tmp_path, fake_state_dir, monkeypatch):
    t = _stop_spoke_final(tmp_path, "a3")
    _stale(monkeypatch, t, "H2")
    _tail_stop(t)
    _append(t, [_assistant_msg("m4", R4_REPLY)])
    said = run_threads(monkeypatch, t, {"H1": ("stop", STOP), "H2": ("stop", STOP)},
                       [("H1", "_take_landed"), ("H2", "_take_landed"), ("H2", "claim"), ("H1", "claim"),
                        ("H1", "_claim_lines"), ("H2", "_claim_lines")], hold=True)
    h = heard(said)
    assert h.count(R3_FINAL) == 0 and h.count(R3_INTER) == 1 and h.count(R4_REPLY) == 1, said


def test_adv_three_hooks_stale_write_before_loser_claim_lines_winner_first(tmp_path, fake_state_dir, monkeypatch):
    t = _stop_spoke_final(tmp_path, "a4")
    _stale(monkeypatch, t, "H1")
    _tail_stop(t)
    _append(t, [_assistant_msg("m4", R4_REPLY)])
    said = run_threads(monkeypatch, t, {"H1": ("stop", STOP), "H2": ("stop", STOP)},
                       [("H1", "_take_landed"), ("H2", "_take_landed"), ("H2", "claim"), ("H1", "claim"),
                        ("H2", "_claim_lines"), ("H1", "_claim_lines")], hold=True)
    h = heard(said)
    assert h.count(R3_FINAL) == 0 and h.count(R3_INTER) == 1 and h.count(R4_REPLY) == 1, said


def test_adv_three_hooks_reply_from_input_n1_order(tmp_path, fake_state_dir, monkeypatch):
    t = _stop_spoke_final(tmp_path, "a5")
    _stale(monkeypatch, t, "H2")
    _tail_stop(t)
    said = run_threads(monkeypatch, t, {"H1": ("stop", STOP), "H2": ("stop", STOP)},
                       [("H1", "_take_landed"), ("H2", "_take_landed"), ("H2", "claim"), ("H1", "claim"),
                        ("H1", "_claim_lines"), ("H2", "_claim_lines")], hold=True)
    h = heard(said)
    assert h.count(R3_FINAL) == 0 and h.count(R3_INTER) == 1 and h.count(R4_REPLY) == 1, said


def test_adv_n2_ptu_owner_wins_but_loses_cas_to_a_stop(tmp_path, fake_state_dir, monkeypatch):
    """PTU owner P (keyed message:m4) and the turn's Stop S (prompt:p2) both win their own keys.
    S, a non-owner, writes the watermark past X; P's CAS on X then fails, and P still speaks the
    INTER it owns before returning (round six; before it, P returned silent and INTER was lost)."""
    t = _stop_spoke_final(tmp_path, "a6")
    _ptu_tail(t)
    said = run_threads(monkeypatch, t, {"P": ("post_tool_use", {"prompt_id": "p2"}), "S": ("stop", STOP)},
                       [("P", "_take_landed"), ("S", "_take_landed"), ("S", "claim"), ("S", "_claim_lines"),
                        ("P", "claim"), ("P", "_claim_watermark")])
    h = heard(said)
    assert h.count(R3_FINAL) == 0 and h.count(R3_INTER) == 1 and h.count(X_TEXT) == 1 and h.count(R4_REPLY) == 1, said


def test_adv_ptu_loser_with_empty_owned_span_speaks_nothing(tmp_path, fake_state_dir, monkeypatch):
    """m5b's lagged claim on the PTU loser path: INTER was claimed and spoken by a lagged hook up to
    the line after it; H1's take owns from there, so its owned span holds no text."""
    t = _stop_spoke_final(tmp_path, "a7")
    start = cli._count_lines(t)
    _ptu_tail(t)
    raw = []
    real = cli._take_landed

    def w(state_file, lock_dir, *a, **k):
        if threading.current_thread().name == "H1" and not raw:
            assert cli._claim_watermark(state_file, lock_dir, start, start + 1)
            raw.append(R3_INTER)
        return real(state_file, lock_dir, *a, **k)
    monkeypatch.setattr(cli, "_take_landed", w)
    P = {"prompt_id": "p2"}
    said = run_threads(monkeypatch, t, {"H1": ("post_tool_use", P), "H2": ("post_tool_use", P)},
                       [("H1", "_take_landed"), ("H2", "_take_landed"), ("H2", "claim"), ("H1", "claim"),
                        ("H2", "_claim_watermark")])
    assert [x for n, x in said if n == "H1"] == [], said
    h = " ".join(raw) + " " + heard(said)
    assert h.count(R3_INTER) == 1 and h.count(X_TEXT) == 1 and h.count(R3_FINAL) == 0, (raw, said)


# --- round seven (house review): the last adversarial pass on F1, the PostToolUse owner that
# loses the CAS. 4d also pins "nothing is written" on that path.

R7_P = {"prompt_id": "p2"}


def _r7_total(h):
    assert h.count(R3_FINAL) == 0 and h.count(R3_INTER) == 1 and h.count(X_TEXT) == 1 and h.count(R4_REPLY) == 1, h


# 4a. F1 with the roles swapped: the Stop owns INTER, the PostToolUse wins X's watermark claim.
@pytest.mark.parametrize("ptu_cas_first", [True, False])
def test_r7_swap_stop_owner_ptu_winner(tmp_path, fake_state_dir, monkeypatch, ptu_cas_first):
    t = _stop_spoke_final(tmp_path, f"r7a{ptu_cas_first}")
    _ptu_tail(t)
    if ptu_cas_first:
        seq = [("S", "_take_landed"), ("P", "_take_landed"), ("P", "claim"), ("P", "_claim_watermark"),
               ("S", "claim"), ("S", "_claim_lines")]
    else:
        seq = [("S", "_take_landed"), ("P", "_take_landed"), ("P", "claim"), ("S", "claim"),
               ("S", "_claim_lines"), ("P", "_claim_watermark")]
    said = run_threads(monkeypatch, t, {"S": ("stop", STOP), "P": ("post_tool_use", R7_P)}, seq)
    _r7_total(heard(said))
    assert [n for n, x in said if R3_INTER in x] == ["S"], said


# 4b. F1 with a lagged third hook writing a stale watermark between P's failed CAS and its speech;
# a follow-up hook of the next event then runs. S's last write is ordered before P's claim, so the
# stale write is the file's last word: the adversarial case. It sits below the prompt, the Stop's
# record reads as "never landed" and the landed reply is spoken a second time. The hole is the
# backward write, not the record (issue #14: a hook with nothing new must never move the watermark
# back). Strict xfail: when #14 lands this passes and says so.
@pytest.mark.xfail(strict=True, reason="#14: a stale watermark write moves the mark back below the prompt")
def test_r7_f1_stale_write_between_failed_cas_and_owner_speech(tmp_path, fake_state_dir, monkeypatch):
    t = _stop_spoke_final(tmp_path, "r7b")
    old = cli._count_lines(t)
    _ptu_tail(t)
    real_cw = cli._claim_watermark
    real_ww = cli._write_watermark
    stale = []

    def cw(state_file, lock_dir, *a, **k):
        ok = real_cw(state_file, lock_dir, *a, **k)
        if threading.current_thread().name == "P" and not ok:
            real_ww(state_file, lock_dir, old)  # H3: 'no new lines', its old count
            stale.append(old)
        return ok
    monkeypatch.setattr(cli, "_claim_watermark", cw)
    said = run_threads(monkeypatch, t, {"P": ("post_tool_use", R7_P), "S": ("stop", STOP)},
                       [("P", "_take_landed"), ("S", "_take_landed"), ("S", "claim"), ("S", "_claim_lines"),
                        ("S", "_write_watermark"), ("P", "claim"), ("P", "_claim_watermark")])
    assert stale, "the CAS did not fail: not the F1 path"
    _r7_total(heard(said))
    assert [n for n, x in said if R3_INTER in x] == ["P"], said
    _append(t, [_assistant_msg("m5", R4_REPLY)])
    follow = _hook(t, "post_tool_use", prompt="p2")
    assert follow == [], follow  # today [R4_REPLY]: the xfail above


# 4c. An owner with an empty owned span on the F1 path speaks nothing.
def test_r7_f1_owner_with_empty_owned_span_speaks_nothing(tmp_path, fake_state_dir, monkeypatch):
    t = _stop_spoke_final(tmp_path, "r7c")
    start = cli._count_lines(t)
    _ptu_tail(t)
    raw = []
    real = cli._take_landed
    real_cw = cli._claim_watermark
    cas = []

    def w(state_file, lock_dir, *a, **k):
        if threading.current_thread().name == "P" and not raw:
            assert real_cw(state_file, lock_dir, start, start + 1)  # a lagged hook claimed INTER
            raw.append(R3_INTER)
        return real(state_file, lock_dir, *a, **k)

    def cw(state_file, lock_dir, *a, **k):
        ok = real_cw(state_file, lock_dir, *a, **k)
        if threading.current_thread().name == "P":
            cas.append(ok)
        return ok
    monkeypatch.setattr(cli, "_take_landed", w)
    monkeypatch.setattr(cli, "_claim_watermark", cw)
    said = run_threads(monkeypatch, t, {"P": ("post_tool_use", R7_P), "S": ("stop", STOP)},
                       [("P", "_take_landed"), ("S", "_take_landed"), ("S", "claim"), ("S", "_claim_lines"),
                        ("P", "claim"), ("P", "_claim_watermark")])
    assert cas == [False], cas  # the F1 path: P won its utterance claim, lost the CAS
    assert [x for n, x in said if n == "P"] == [], said
    _r7_total(" ".join(raw) + " " + heard(said))


# 4d. Pins "nothing is written" on the F1 path: the Stop read one line more (its reply landed after
# P read the file), so a P that wrote its own count would move the watermark back below the reply.
# The reply lands after P's scan returns: P's scan reads to the end of the file, so landing it after
# P's second count let P see the reply in about 19 runs of 20, and a P that wrote its own count lived
# (house review, round eight). S's last write is ordered before P's claim, so it cannot cover P's.
def test_r7_f1_owner_writes_nothing_follow_up_hook_is_silent(tmp_path, fake_state_dir, monkeypatch):
    t = _stop_spoke_final(tmp_path, "r7d")
    _ptu_tail(t)
    p_scanned = threading.Event()
    real_scan = cli._scan_transcript

    def scan(*a, **k):
        out = real_scan(*a, **k)
        if threading.current_thread().name == "P":
            p_scanned.set()
        return out
    real_rw = cli._read_watermark
    landed: list = []

    def rw(state_file, lock_dir, transcript):
        if threading.current_thread().name == "S" and not landed:
            landed.append(1)
            assert p_scanned.wait(5)
            _append(t, [_assistant_msg("m5", R4_REPLY)])  # the reply lands after P read the file
        return real_rw(state_file, lock_dir, transcript)
    real_cw = cli._claim_watermark
    cas = []

    def cw(state_file, lock_dir, *a, **k):
        ok = real_cw(state_file, lock_dir, *a, **k)
        if threading.current_thread().name == "P":
            cas.append(ok)
        return ok
    monkeypatch.setattr(cli, "_scan_transcript", scan)
    monkeypatch.setattr(cli, "_read_watermark", rw)
    monkeypatch.setattr(cli, "_claim_watermark", cw)
    said = run_threads(monkeypatch, t, {"P": ("post_tool_use", R7_P), "S": ("stop", STOP)},
                       [("P", "_take_landed"), ("S", "_take_landed"), ("S", "claim"), ("S", "_claim_lines"),
                        ("S", "_write_watermark"), ("P", "claim"), ("P", "_claim_watermark")])
    assert cas == [False], cas  # the F1 path: P won its utterance claim, lost the CAS
    _r7_total(heard(said))
    assert [n for n, x in said if R3_INTER in x] == ["P"], said
    _append(t, [_tool_use("m6"), _tool_result()])
    follow = _hook(t, "post_tool_use", prompt="p3")
    assert follow == [], follow
