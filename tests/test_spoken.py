"""The spoken store: one claim per utterance, so a doubled event speaks once.

Design v2 (2026-10-01) and its review list planted cases; each has a test here, named by its
number. A planted case that guards against a missing store has a control beside it that runs
the same events with the store disabled, or with a content-only key, and asserts the double
(or the drop) that the store prevents: the control failing means the planted test no longer
measures anything.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import stat
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import claude_code_tts.daemon as d
import claude_code_tts.state as st
import tests.test_watermark as _wm
from claude_code_tts import cli, spoken
from claude_code_tts.config import TTSConfig
from tests.test_pause_ledger import loop_harness, stop_loop

fake_state_dir = _wm.fake_state_dir  # the shared fixture, registered under its own name here
_append, _assistant_msg, _tool_result, _tool_use = _wm._append, _wm._assistant_msg, _wm._tool_result, _wm._tool_use
_user, _write_transcript = _wm._user, _wm._write_transcript


def _hook(
    transcript: Path,
    hook_type: str,
    text: str | None = None,
    prompt: str | None = None,
    session: str = "sess-1",
    speak_effect=None,
) -> list[str]:
    """Run one hook process's worth of _speak_from_hook; the texts it spoke."""
    said: list[str] = []
    payload: dict = {"transcript_path": str(transcript), "tool_name": "Bash", "session_id": session}
    if text is not None:
        payload["last_assistant_message"] = text
    if prompt is not None:
        payload["prompt_id"] = prompt

    def default_speak(t, cfg):
        said.append(t)

    with patch("sys.stdin", io.StringIO(json.dumps(payload))), \
         patch("claude_code_tts.cli.load_config") as mock_load, \
         patch("claude_code_tts.audio.speak", side_effect=speak_effect or default_speak), \
         patch("claude_code_tts.session.pin_session"), \
         patch("claude_code_tts.cli.time.sleep"):  # the hook's settle waits, not the daemon tests'
        mock_load.return_value = TTSConfig(
            mode="direct", muted=False, intermediate=True, session_id="-Users-dev", project_name="home",
        )
        cli._speak_from_hook(argparse.Namespace(hook_type=hook_type))
    return said


def _turn(tmp_path: Path, name: str) -> Path:
    """A transcript whose first turn was spoken; the second turn is under way (a tool ran)."""
    transcript = tmp_path / "projects" / "-Users-dev" / f"{name}.jsonl"
    _write_transcript(transcript, [_user("hi"), _assistant_msg("m0", "opening reply text here")])
    assert _hook(transcript, "stop", text="opening reply text here", prompt="p0") == ["opening reply text here"]
    _append(transcript, [_user("go"), _tool_use("m1"), _tool_result()])
    return transcript


@pytest.fixture
def store_disabled(monkeypatch):
    """Every claim granted, nothing stored: the hook as if spoken.py did not exist."""
    monkeypatch.setattr(spoken, "claim", lambda *a, **k: spoken.Claim(None))


@pytest.fixture
def content_only_key(monkeypatch):
    """The key v1 proposed and the review rejected: the text alone."""
    monkeypatch.setattr(spoken, "utterance_key", lambda scope, mkey, text: spoken.digest(spoken.normalize(text)))


# --- 1. Twins, same input, same second: one speech ----------------------------------------


def _case1(tmp_path, name):
    transcript = _turn(tmp_path, name)
    final = "the answer both twins were handed"
    return [_hook(transcript, "stop", text=final, prompt="p1"), _hook(transcript, "stop", text=final, prompt="p1")]


def test_case1_twin_stops_speak_once(tmp_path, fake_state_dir):
    assert _case1(tmp_path, "c1") == [["the answer both twins were handed"], []]


def test_case1_control_without_the_store_twins_speak_twice(tmp_path, fake_state_dir, store_disabled):
    assert _case1(tmp_path, "c1x") == [["the answer both twins were handed"]] * 2


# --- 2. Split pair (one twin from the input, one from the file): one speech ------------------


def _case2(tmp_path, name, monkeypatch):
    transcript = _turn(tmp_path, name)
    final = "the reply that landed between the twins"
    a = _hook(transcript, "stop", text=final, prompt="p1")  # spoke from the input; wrote .pending
    _append(transcript, [_assistant_msg("m2", final)])
    # Twin B read .pending before A wrote it, then found the line landed in the file.
    monkeypatch.setattr(cli, "_read_pending_record", lambda _p: ("", ""))
    b = _hook(transcript, "stop", text=final, prompt="p1")
    return [a, b]


def test_case2_split_pair_speaks_once(tmp_path, fake_state_dir, monkeypatch):
    assert _case2(tmp_path, "c2", monkeypatch) == [["the reply that landed between the twins"], []]


def test_case2_control_without_the_store_the_split_pair_speaks_twice(
    tmp_path, fake_state_dir, monkeypatch, store_disabled
):
    assert _case2(tmp_path, "c2x", monkeypatch) == [["the reply that landed between the twins"]] * 2


# --- 3. Landed line found 2 h later: silent (the record, not the store) ----------------------


def test_case3_a_line_found_two_hours_later_is_silent(tmp_path, fake_state_dir):
    transcript = _turn(tmp_path, "c3")
    final = "the reply nobody read back for two hours"
    assert _hook(transcript, "stop", text=final, prompt="p1") == [final]
    two_hours_ago = time.time() - 7200
    claims = list(spoken.hook_dir().iterdir()) if spoken.hook_dir().exists() else []
    for f in [*fake_state_dir.glob("*.pending"), *claims]:
        os.utime(f, (two_hours_ago, two_hours_ago))  # the store's claims are long stale too
    _append(transcript, [_assistant_msg("m2", final), _user("next"), _tool_use("m3"), _tool_result()])
    assert _hook(transcript, "post_tool_use") == []
    _append(transcript, [_assistant_msg("m4", "and the next turn speaks as usual"), _tool_use("m4"), _tool_result()])
    assert _hook(transcript, "post_tool_use") == ["and the next turn speaks as usual"]


# --- 3b. The line never landed: the record does not cut a later reply ending in its words ----


def test_case3b_a_record_whose_line_never_landed_does_not_cut_the_next_reply(tmp_path, fake_state_dir):
    """Geordi's finding 6 on b76cbf0 (2026-10-02): the Stop spoke "and the build is green now."
    from its input and the turn was interrupted, so its line never landed. The next reply ends
    in the same words and made a tool call; the suffix cut spoke only its first half. A user
    line before the first new assistant text means the response never landed: the record is
    dropped, not matched, and the reply is spoken whole."""
    transcript = _turn(tmp_path, "c3b")
    spoken_text = "and the build is green now."
    assert _hook(transcript, "stop", text=spoken_text, prompt="p1") == [spoken_text]
    assert list(fake_state_dir.glob("*.pending")), "the Stop recorded the response it spoke"
    reply = "I reran everything from scratch, and the build is green now."
    _append(transcript, [_user("next"), _assistant_msg("m2", reply), _tool_use("m2"), _tool_result()])
    assert _hook(transcript, "post_tool_use", prompt="p2") == [reply]
    assert not list(fake_state_dir.glob("*.pending")), "the stale record is gone"


def test_case3b_control_the_landed_line_before_the_next_prompt_is_still_the_record(tmp_path, fake_state_dir):
    """The same words, but the line did land before the next prompt: it is skipped, as in case 3."""
    # Invariant the drop depends on, in one line: a record exists before its line has landed
    # (the Stop spoke from its input because the file did not have it yet), and that line, when
    # it lands, lands before the next prompt's user line or (2.1.286) first after it; never later.
    transcript = _turn(tmp_path, "c3b-ctl")
    spoken_text = "and the build is green now."
    assert _hook(transcript, "stop", text=spoken_text, prompt="p1") == [spoken_text]
    _append(transcript, [_assistant_msg("m2", spoken_text), _user("next"),
                         _assistant_msg("m3", "I reran everything from scratch, and the build is green now."),
                         _tool_use("m3"), _tool_result()])
    assert _hook(transcript, "post_tool_use", prompt="p2") == ["I reran everything from scratch, and the build is green now."]


def test_case3c_the_stops_line_written_after_the_next_prompt_is_still_its_landing(tmp_path, fake_state_dir):
    """Geordi on f63b66b: the order the drop leans on has moved once (Claude Code 2.1.286 wrote
    the Stop's line late). A late line that lands after the next prompt is still the response:
    it is found wherever it is, skipped, and the record goes with it; nothing is dropped."""
    transcript = _turn(tmp_path, "c3c")
    spoken_text = "and the build is green now."
    assert _hook(transcript, "stop", text=spoken_text, prompt="p1") == [spoken_text]
    _append(transcript, [_user("next"), _assistant_msg("m2", spoken_text), _tool_use("m2"), _tool_result()])
    assert _hook(transcript, "post_tool_use", prompt="p2") == [], "the late line is the landing, not a reply"
    assert not list(fake_state_dir.glob("*.pending")), "taken with the line"
    _append(transcript, [_assistant_msg("m3", "and now the new turn's own words"), _tool_use("m3"), _tool_result()])
    assert _hook(transcript, "post_tool_use", prompt="p2") == ["and now the new turn's own words"]


def test_case3b_a_twin_stops_fresh_record_survives_the_drop(tmp_path, fake_state_dir, monkeypatch):
    """Geordi on f63b66b: the drop must compare the record's identity under the lock. The twin
    here writes the same words under the next turn's prompt_id between this hook's read of the
    stale record and its drop; that is a new record and must stay."""
    transcript = _turn(tmp_path, "c3b-twin")
    spoken_text = "and the build is green now."
    assert _hook(transcript, "stop", text=spoken_text, prompt="p1") == [spoken_text]
    pending_file = next(fake_state_dir.glob("*.pending"))
    assert cli._read_pending_record(pending_file)[1] == "p1"
    real_first = cli._first_prompt_line

    def twin_writes_between_read_and_drop(path, start, end):
        boundary = real_first(path, start, end)
        cli._write_pending(pending_file, spoken_text, "p2")  # the twin Stop of turn two
        return boundary

    monkeypatch.setattr(cli, "_first_prompt_line", twin_writes_between_read_and_drop)
    reply = "I reran everything from scratch, and the build is green now."
    _append(transcript, [_user("next"), _assistant_msg("m2", reply), _tool_use("m2"), _tool_result()])
    assert _hook(transcript, "post_tool_use", prompt="p2") == [reply]
    assert cli._read_pending_record(pending_file) == (cli._pending_key(spoken_text), "p2"), "the twin's record stays"


def test_a_stop_that_writes_a_record_sweeps_month_old_orphans_of_other_transcripts(tmp_path, fake_state_dir):
    """Geordi's finding 6, the other half: a record whose transcript is never read again had no
    hook left to drop it. Hygiene by the next Stop that writes one; a day-old neighbour stays."""
    old = fake_state_dir / "claude_tts_spoken_gone-room.pending"
    young = fake_state_dir / "claude_tts_spoken_busy-room.pending"
    for f, age in ((old, 31 * 86400), (young, 86400)):
        f.write_text("10 abc p9")
        os.utime(f, (time.time() - age, time.time() - age))
    transcript = _turn(tmp_path, "sweep")
    assert _hook(transcript, "stop", text="and the build is green now.", prompt="p1") == ["and the build is green now."]
    assert not old.exists() and young.exists()
    assert len(list(fake_state_dir.glob("*.pending"))) == 2, "the young neighbour and this transcript's own"


# --- 3d. Review of round two (Geordi's brother, 2026-10-02): the take's identity, the multi-block
# late landing's stated harm, the sweep's edge, and the record as what keeps case 3 silent ------

MB_LAST = "and the build is green now."
MB_FIRST = "First I fixed the broken config file."


def test_twin_stop_take_landed_keeps_the_twins_fresh_record(tmp_path, fake_state_dir, monkeypatch):
    """Twin Stops of turn two, identical stdin. Both read turn one's record R1, whose line landed.
    Twin B runs to the end (takes R1, claims, writes R2 for turn two) while twin A is between its
    read of R1 and its _take_landed. A take that cleared unconditionally lost R2, and turn two's
    line, when it landed, was spoken a second time under another claim key (message id, not
    prompt). The take clears only while the record is still the one it read."""
    t = _turn(tmp_path, "twin-take")
    s1, s2 = "the first turn's reply, spoken from input", "the second turn's reply, spoken from input"
    assert _hook(t, "stop", text=s1, prompt="p1") == [s1]
    _append(t, [_assistant_msg("m2", s1), _user("next"), _tool_use("m3"), _tool_result()])
    pending_file = next(fake_state_dir.glob("*.pending"))
    real_first = cli._first_prompt_line
    said_b: list[list[str]] = []
    state = {"ran": False}

    def b_runs_inside_a(path, start, end):
        boundary = real_first(path, start, end)
        if not state["ran"]:
            state["ran"] = True
            said_b.append(_hook(t, "stop", text=s2, prompt="p2"))  # twin B, start to finish
        return boundary

    monkeypatch.setattr(cli, "_first_prompt_line", b_runs_inside_a)
    said_a = _hook(t, "stop", text=s2, prompt="p2")
    assert said_b == [[s2]] and said_a == []
    r2_kept = cli._read_pending_record(pending_file) == (cli._pending_key(s2), "p2")
    monkeypatch.setattr(cli, "_first_prompt_line", real_first)
    _append(t, [_assistant_msg("m4", s2), _user("third"), _tool_use("m5"), _tool_result()])
    said3 = _hook(t, "post_tool_use", prompt="p3")
    assert (r2_kept, said3) == (True, []), "twin B's record stays and turn two's late line is silent"


def test_multiblock_late_landing_repeats_its_last_block(tmp_path, fake_state_dir):
    """The Stop spoke the last block from its input; the two-block message lands after the
    next prompt. Documents the stated lesser harm: the last block is heard a second time.
    Two rare conditions at once (a multi-block final message and a late write)."""
    t = _turn(tmp_path, "mb-late")
    assert _hook(t, "stop", text=MB_LAST, prompt="p1") == [MB_LAST]
    _append(t, [_user("next"), _assistant_msg("m2", MB_FIRST), _assistant_msg("m2", MB_LAST),
                _tool_use("m3"), _tool_result()])
    assert _hook(t, "post_tool_use", prompt="p2") == [f"{MB_FIRST} {MB_LAST}"]


def test_multiblock_on_time_landing_speaks_only_the_first_block(tmp_path, fake_state_dir):
    """Control: the same message landing before the next prompt speaks only what was not said."""
    t = _turn(tmp_path, "mb-ontime")
    assert _hook(t, "stop", text=MB_LAST, prompt="p1") == [MB_LAST]
    _append(t, [_assistant_msg("m2", MB_FIRST), _assistant_msg("m2", MB_LAST), _user("next"),
                _tool_use("m3"), _tool_result()])
    assert _hook(t, "post_tool_use", prompt="p2") == [MB_FIRST]


def test_the_landed_line_is_found_later_in_the_tail_than_its_first_line(tmp_path, fake_state_dir):
    """Gap M9: an intermediate no PostToolUse read sits first in the tail; the response is the
    tail's last line, whole. It is found there and skipped; the intermediate is what speaks."""
    t = _turn(tmp_path, "tail-later")
    final = "and that is the whole of it."
    assert _hook(t, "stop", text=final, prompt="p1") == [final]
    _append(t, [_assistant_msg("m2", "an intermediate nobody read"), _tool_use("m2"), _tool_result(),
                _assistant_msg("m3", final), _user("next"), _tool_use("m4"), _tool_result()])
    assert _hook(t, "post_tool_use", prompt="p2") == ["an intermediate nobody read"]
    assert not list(fake_state_dir.glob("*.pending")), "taken with the line, wherever it was in the tail"
    # and the landed line is covered by that speech's claim, not read again
    _append(t, [_assistant_msg("m5", "the new turn's own first words here"), _tool_use("m5"), _tool_result()])
    assert _hook(t, "post_tool_use", prompt="p2") == ["the new turn's own first words here"]


def test_a_tool_result_is_not_the_prompt_that_ends_the_tail(tmp_path, fake_state_dir):
    """The tail of the old turn can hold a tool call of its own (an intermediate no PostToolUse
    read, its tool result) before the response. A tool result is a user line; it is not the
    next prompt, so the response after it is still in the tail and still taken."""
    t = _turn(tmp_path, "tail-toolresult")
    final = "and that is the whole of it."
    assert _hook(t, "stop", text=final, prompt="p1") == [final]
    _append(t, [_assistant_msg("m2", "ok then"), _tool_use("m2"), _tool_result(),
                _assistant_msg("m3", final), _user("next"), _tool_use("m4"), _tool_result()])
    assert _hook(t, "post_tool_use", prompt="p2") == [], "the short intermediate says nothing, the response is taken"
    assert not list(fake_state_dir.glob("*.pending"))
    # The too-short path covered the taken line as well: the next Stop, which reads every
    # unspoken line before its response, does not say the old response again.
    new_reply = "the new turn's own first words here"
    _append(t, [_assistant_msg("m5", new_reply)])
    assert _hook(t, "stop", text=new_reply, prompt="p2") == [new_reply]


def test_a_meta_user_line_is_not_a_prompt(tmp_path):
    transcript = tmp_path / "t.jsonl"
    lines = [
        {"type": "user", "isMeta": True, "message": {"content": [{"type": "text", "text": "injected context"}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "x", "content": "ok"}]}},
        {"type": "user", "message": {"content": "a real prompt as a string"}},
    ]
    transcript.write_text("".join(json.dumps(line) + "\n" for line in lines))
    assert cli._first_prompt_line(transcript, 0, 3) == 2
    assert cli._first_prompt_line(transcript, 0, 2) is None
    lines.append({"type": "user", "message": {"content": [{"type": "image", "source": {"data": "x"}}]}})
    lines.append({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "y"},
                                                          {"type": "text", "text": "rides on a tool result"}]}})
    transcript.write_text("".join(json.dumps(line) + "\n" for line in lines))
    assert cli._first_prompt_line(transcript, 3, 5) == 3, "an image-only prompt is a prompt"
    assert cli._first_prompt_line(transcript, 4, 5) is None, "text riding on a tool result is not"


def test_a_drop_that_checked_the_turn_alone_would_lose_the_twins_record(tmp_path, fake_state_dir, monkeypatch):
    """Gap M5: the twin of this very turn writes different words under the same prompt_id between
    the read and the drop (its stdin differed: a Stop re-fired after an edit). Identity is key and
    turn together; checking the turn alone would drop the fresh record."""
    transcript = _turn(tmp_path, "c3b-twin-turn")
    spoken_text = "and the build is green now."
    assert _hook(transcript, "stop", text=spoken_text, prompt="p1") == [spoken_text]
    pending_file = next(fake_state_dir.glob("*.pending"))
    real_first = cli._first_prompt_line

    def twin_writes_other_words_same_turn(path, start, end):
        boundary = real_first(path, start, end)
        cli._write_pending(pending_file, "different words from the twin", "p1")
        return boundary

    monkeypatch.setattr(cli, "_first_prompt_line", twin_writes_other_words_same_turn)
    reply = "I reran everything from scratch, and the build is green now."
    _append(transcript, [_user("next"), _assistant_msg("m2", reply), _tool_use("m2"), _tool_result()])
    assert _hook(transcript, "post_tool_use", prompt="p2") == [reply]
    assert cli._read_pending_record(pending_file) == (cli._pending_key("different words from the twin"), "p1")


def test_sweep_leaves_the_record_at_exactly_thirty_days(tmp_path, fake_state_dir):
    p = fake_state_dir / "claude_tts_spoken_x.pending"
    p.write_text("1 a")
    now = time.time()
    os.utime(p, (now - cli.PENDING_ORPHAN_AGE_S + 60, now - cli.PENDING_ORPHAN_AGE_S + 60))
    assert cli._sweep_pending_orphans(fake_state_dir / "claude_tts_spoken_me.pending", now) == 0 and p.exists()


def test_case3_control_without_the_record_the_late_found_line_speaks_again(tmp_path, fake_state_dir):
    """Control for case 3: the record is what keeps the two-hours-later line silent."""
    t = _turn(tmp_path, "c3-ctl")
    final = "the reply nobody read back for two hours"
    assert _hook(t, "stop", text=final, prompt="p1") == [final]
    for f in fake_state_dir.glob("*.pending"):
        f.unlink()
    _append(t, [_assistant_msg("m2", final), _user("next"), _tool_use("m3"), _tool_result()])
    assert _hook(t, "post_tool_use") == [final]


# --- 3e. Review of round three (Geordi, 2026-10-02): the take owns the lines it moves past,
# and a prompt is any line the person wrote --------------------------------------------------

R3_FINAL = "and that is the whole of it."
R3_INTER = "an intermediate nobody read"


def _stop_spoke_final(tmp_path, name):
    t = _turn(tmp_path, name)
    assert _hook(t, "stop", text=R3_FINAL, prompt="p1") == [R3_FINAL]
    return t


# --- item 2: leaving the watermark ---------------------------------------------------------

def test_r3_twin_between_take_and_claim_respeaks_the_landed_line(tmp_path, fake_state_dir, monkeypatch):
    """Twin PostToolUse hooks of one event. H1 takes the landed line at idx 1 without moving the
    watermark; H2 runs whole before H1 claims. H2 sees no record and a watermark before the
    landed line, so the landed line is its last text: spoken a second time, and H1's claim of
    the intermediate then loses. The take moved the watermark in one step under the lock to
    close exactly this window (comment at the take, 2026-10-01)."""
    t = _stop_spoke_final(tmp_path, "r3-twin")
    _append(t, [_assistant_msg("m2", R3_INTER), _tool_use("m2"), _tool_result(),
                _assistant_msg("m3", R3_FINAL), _user("next"), _tool_use("m4"), _tool_result()])
    real_take = cli._take_landed
    said_h2: list[list[str]] = []

    def h2_runs_after_h1_takes(*a, **k):
        owned = real_take(*a, **k)  # the take says whether H1 owns the lines it moved past
        if not said_h2:
            said_h2.append(_hook(t, "post_tool_use", prompt="p2"))
        return owned

    monkeypatch.setattr(cli, "_take_landed", h2_runs_after_h1_takes)
    said_h1 = _hook(t, "post_tool_use", prompt="p2")
    assert said_h1 + said_h2[0] == [R3_INTER], (said_h1, said_h2)


def test_r3_short_new_turn_text_leaves_the_landed_line_for_the_stop(tmp_path, fake_state_dir):
    """Tail [intermediate, landed]; the new turn's first text is short ("ok then"). The take
    leaves the watermark (idx 1), the hook's last text is too short, and the too-short cover
    applies only when the landed line is after that text. Nothing moves the watermark; the
    record is gone; the next Stop reads every unspoken line and says the landed line again."""
    t = _stop_spoke_final(tmp_path, "r3-short")
    _append(t, [_assistant_msg("m2", R3_INTER), _tool_use("m2"), _tool_result(),
                _assistant_msg("m3", R3_FINAL), _user("next"),
                _assistant_msg("m4", "ok then"), _tool_use("m4"), _tool_result()])
    first = _hook(t, "post_tool_use", prompt="p2")
    reply = "the new turn's own reply, long enough to speak"
    _append(t, [_assistant_msg("m5", reply)])
    second = _hook(t, "stop", text=reply, prompt="p2")
    heard = " ".join(first + second)
    assert heard.count(R3_FINAL) == 0, (first, second)


def test_r3_intermediate_spoken_by_claim_is_not_reread_by_the_stop(tmp_path, fake_state_dir):
    """Control for the asked double: the intermediate spoken by its own claim is not read again
    by the next Stop."""
    t = _stop_spoke_final(tmp_path, "r3-ctl")
    _append(t, [_assistant_msg("m2", R3_INTER), _tool_use("m2"), _tool_result(),
                _assistant_msg("m3", R3_FINAL), _user("next"), _tool_use("m4"), _tool_result()])
    assert _hook(t, "post_tool_use", prompt="p2") == [R3_INTER]
    reply = "the new turn's own reply, long enough to speak"
    _append(t, [_assistant_msg("m5", reply)])
    assert _hook(t, "stop", text=reply, prompt="p2") == [reply]


# --- item 7 -----------------------------------------------------------------------------------

def _mixed_user_line(text):
    return {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "x", "content": "ok"}, {"type": "text", "text": text}]}}


def test_r3_tool_result_with_a_text_block_in_the_tail(tmp_path, fake_state_dir):
    """A tool result line that also carries a text block counts as a prompt and ends the tail
    early: the response after it is missed, the record dropped, the response spoken again.
    Whether Claude Code writes this shape mid-turn is unmeasured."""
    t = _stop_spoke_final(tmp_path, "r3-mixed")
    _append(t, [_assistant_msg("m2", R3_INTER), _tool_use("m2"), _mixed_user_line("hook feedback text"),
                _assistant_msg("m3", R3_FINAL), _user("next"), _tool_use("m4"), _tool_result()])
    assert _hook(t, "post_tool_use", prompt="p2") == [R3_INTER]


def test_r3_meta_text_line_inside_the_tail_does_not_end_it(tmp_path, fake_state_dir):
    """An isMeta user line with text inside the tail, before the response: not a prompt."""
    t = _stop_spoke_final(tmp_path, "r3-meta")
    meta = {"type": "user", "isMeta": True, "message": {"content": [{"type": "text", "text": "injected caveat"}]}}
    _append(t, [_assistant_msg("m2", R3_INTER), _tool_use("m2"), _tool_result(), meta,
                _assistant_msg("m3", R3_FINAL), _user("next"), _tool_use("m4"), _tool_result()])
    assert _hook(t, "post_tool_use", prompt="p2") == [R3_INTER]


def test_r3_two_prompts_landing_between(tmp_path, fake_state_dir):
    """[prompt, landed, prompt, new reply]: the landed line is the first after the first prompt."""
    t = _stop_spoke_final(tmp_path, "r3-two")
    reply = "the reply to the second prompt, spoken whole"
    _append(t, [_user("next"), _assistant_msg("m2", R3_FINAL), _user("and another"),
                _assistant_msg("m3", reply), _tool_use("m3"), _tool_result()])
    assert _hook(t, "post_tool_use", prompt="p3") == [reply]


def test_r3_image_only_prompt_is_not_a_boundary(tmp_path, fake_state_dir):
    """A prompt with an image block and no text is not seen as a prompt: no boundary, the tail runs
    into the new turn, and finding 6 (a reply ending in the spoken words is cut) comes back.
    Whether Claude Code writes an image-only prompt without a text block is unmeasured."""
    t = _stop_spoke_final(tmp_path, "r3-image")
    img = {"type": "user", "message": {"content": [{"type": "image", "source": {"type": "base64", "data": "x"}}]}}
    reply = "I looked at the screenshot, and that is the whole of it."
    _append(t, [img, _assistant_msg("m2", reply), _tool_use("m2"), _tool_result()])
    assert _hook(t, "post_tool_use", prompt="p2") == [reply]


def test_r3_stop_with_empty_response(tmp_path, fake_state_dir):
    """A Stop whose response field is empty writes no record and speaks nothing new."""
    t = _stop_spoke_final(tmp_path, "r3-empty")
    _append(t, [_assistant_msg("m2", R3_FINAL)])  # turn one's line lands
    said = _hook(t, "stop", text="", prompt="p2")
    assert said == [] and not list(fake_state_dir.glob("*.pending")), said


# --- regression isolation: the same two shapes with no tool call in the tail (passes on +0002) -

def test_r3_twin_race_no_tool_in_tail(tmp_path, fake_state_dir, monkeypatch):
    t = _stop_spoke_final(tmp_path, "r3-twin-nt")
    _append(t, [_assistant_msg("m2", R3_INTER), _assistant_msg("m3", R3_FINAL), _user("next"),
                _tool_use("m4"), _tool_result()])
    real_take = cli._take_landed
    said_h2: list[list[str]] = []

    def h2_runs_after_h1_takes(*a, **k):
        owned = real_take(*a, **k)  # the take says whether H1 owns the lines it moved past
        if not said_h2:
            said_h2.append(_hook(t, "post_tool_use", prompt="p2"))
        return owned

    monkeypatch.setattr(cli, "_take_landed", h2_runs_after_h1_takes)
    said_h1 = _hook(t, "post_tool_use", prompt="p2")
    assert R3_FINAL not in said_h1 + said_h2[0], (said_h1, said_h2)


def test_r3_short_new_turn_text_no_tool_in_tail(tmp_path, fake_state_dir):
    t = _stop_spoke_final(tmp_path, "r3-short-nt")
    _append(t, [_assistant_msg("m2", R3_INTER), _assistant_msg("m3", R3_FINAL), _user("next"),
                _assistant_msg("m4", "ok then"), _tool_use("m4"), _tool_result()])
    first = _hook(t, "post_tool_use", prompt="p2")
    reply = "the new turn's own reply, long enough to speak"
    _append(t, [_assistant_msg("m5", reply)])
    second = _hook(t, "stop", text=reply, prompt="p2")
    assert " ".join(first + second).count(R3_FINAL) == 0, (first, second)


def test_a_stop_that_takes_a_landed_line_speaks_the_unread_text_before_it(tmp_path, fake_state_dir):
    """No PostToolUse ran in the new turn; its Stop is the first hook to see the tail
    [intermediate, landed]. Its take moves the watermark past both, so its claim filter would
    drop the intermediate as "spoken by a PostToolUse meanwhile"; the take's ownership keeps
    it, and the Stop reads it before its own response, as it does for any unspoken line."""
    t = _stop_spoke_final(tmp_path, "r4-stop-owns")
    _append(t, [_assistant_msg("m2", R3_INTER), _assistant_msg("m3", R3_FINAL), _user("next")])
    reply = "the new turn's own reply, long enough to speak"
    _append(t, [_assistant_msg("m4", reply)])
    assert _hook(t, "stop", text=reply, prompt="p2") == [f"{R3_INTER} {reply}"]
    assert not list(fake_state_dir.glob("*.pending"))


# --- 3f. Review of round four (Geordi, 2026-10-02): twin orders, true interleaves, the lagged
# PostToolUse, the owner that dies, and every prompt shape --------------------------------------

R4_REPLY = "the new turn's own reply, long enough to speak"


def _tail_ptu(t):
    # [inter, tool, result, landed, prompt, tool, result]: a PostToolUse of the new turn sees the tail
    _append(t, [_assistant_msg("m2", R3_INTER), _tool_use("m2"), _tool_result(),
                _assistant_msg("m3", R3_FINAL), _user("next"), _tool_use("m4"), _tool_result()])


def _tail_stop(t):
    # [inter, landed, prompt]: the new turn's Stop (reply in its input) is first to see the tail
    _append(t, [_assistant_msg("m2", R3_INTER), _assistant_msg("m3", R3_FINAL), _user("next")])


def _heard(*lists):
    return " ".join(x for items in lists for x in items)


# --- item 3: the owner that dies ----------------------------------------------------------

def test_an_owner_that_dies_after_its_take_loses_the_lines_it_owned(tmp_path, fake_state_dir):
    """The accepted loss (Geordi, round four, item 3): the owner's take moved the watermark past
    the intermediate; it dies before speaking. A twin cannot tell a dead owner from one about to
    speak, and recovering the lines would double them every time the owner lives, so nobody
    recovers them. Same kind as every claim-before-speak path. The landed line stays silent."""
    t = _stop_spoke_final(tmp_path, "r4-die")
    _tail_ptu(t)

    def boom(*a, **k):
        raise RuntimeError("SIGKILL stand-in")

    with pytest.raises(RuntimeError):
        _hook(t, "post_tool_use", prompt="p2", speak_effect=boom)
    h2 = _hook(t, "post_tool_use", prompt="p2")  # twin, after the owner died
    _append(t, [_assistant_msg("m5", R4_REPLY)])
    st = _hook(t, "stop", text=R4_REPLY, prompt="p2")  # next Stop
    heard = _heard(h2, st)
    assert heard.count(R3_FINAL) == 0 and heard.count(R3_INTER) == 0, (h2, st)


# --- item 4: twin orders, PostToolUse -------------------------------------------------------

def _wrap_take(monkeypatch, before=None, after=None):
    real = cli._take_landed
    state = {"done": False}
    def w(*a, **k):
        if not state["done"] and before:
            state["done"] = True
            before()
        owned = real(*a, **k)
        if not state["done"] and after:
            state["done"] = True
            after()
        return owned
    monkeypatch.setattr(cli, "_take_landed", w)
    return real


def test_twin_h2_takes_first_ptu(tmp_path, fake_state_dir, monkeypatch):
    t = _stop_spoke_final(tmp_path, "r4-o1")
    _tail_ptu(t)
    h2 = []
    _wrap_take(monkeypatch, before=lambda: h2.append(_hook(t, "post_tool_use", prompt="p2")))
    h1 = _hook(t, "post_tool_use", prompt="p2")
    heard = _heard(h1, *h2)
    assert heard.count(R3_FINAL) == 0 and heard.count(R3_INTER) == 1, (h1, h2)


def test_twin_h1_whole_then_h2_ptu(tmp_path, fake_state_dir):
    t = _stop_spoke_final(tmp_path, "r4-o2")
    _tail_ptu(t)
    h1 = _hook(t, "post_tool_use", prompt="p2")
    h2 = _hook(t, "post_tool_use", prompt="p2")
    heard = _heard(h1, h2)
    assert heard.count(R3_FINAL) == 0 and heard.count(R3_INTER) == 1, (h1, h2)


def test_twin_interleave_h1take_h2take_h2speak_h1speak_ptu(tmp_path, fake_state_dir, monkeypatch):
    t = _stop_spoke_final(tmp_path, "r4-o3")
    _tail_ptu(t)
    h2 = []
    _wrap_take(monkeypatch, after=lambda: h2.append(_hook(t, "post_tool_use", prompt="p2")))
    h1 = _hook(t, "post_tool_use", prompt="p2")
    heard = _heard(h1, *h2)
    assert heard.count(R3_FINAL) == 0 and heard.count(R3_INTER) == 1, (h1, h2)


# --- item 4: twin orders, Stop (same stdin, one claim key prompt:p2) ----------------------

def test_twin_h2_takes_first_stop(tmp_path, fake_state_dir, monkeypatch):
    t = _stop_spoke_final(tmp_path, "r4-s1")
    _tail_stop(t)
    h2 = []
    _wrap_take(monkeypatch, before=lambda: h2.append(_hook(t, "stop", text=R4_REPLY, prompt="p2")))
    h1 = _hook(t, "stop", text=R4_REPLY, prompt="p2")
    heard = _heard(h1, *h2)
    assert heard.count(R3_FINAL) == 0 and heard.count(R3_INTER) == 1 and heard.count(R4_REPLY) == 1, (h1, h2)


def test_twin_h1_whole_then_h2_stop(tmp_path, fake_state_dir):
    t = _stop_spoke_final(tmp_path, "r4-s2")
    _tail_stop(t)
    h1 = _hook(t, "stop", text=R4_REPLY, prompt="p2")
    h2 = _hook(t, "stop", text=R4_REPLY, prompt="p2")
    heard = _heard(h1, h2)
    assert heard.count(R3_FINAL) == 0 and heard.count(R3_INTER) == 1 and heard.count(R4_REPLY) == 1, (h1, h2)


def test_twin_interleave_h1take_h2take_h2speak_h1speak_stop(tmp_path, fake_state_dir, monkeypatch):
    """H1 owns the intermediate; H2 (not owner, same claim key) wins the spoken-store claim and
    speaks only its reply; H1 loses the claim and returns: the intermediate is never said."""
    t = _stop_spoke_final(tmp_path, "r4-s3")
    _tail_stop(t)
    h2 = []
    _wrap_take(monkeypatch, after=lambda: h2.append(_hook(t, "stop", text=R4_REPLY, prompt="p2")))
    h1 = _hook(t, "stop", text=R4_REPLY, prompt="p2")
    heard = _heard(h1, *h2)
    assert heard.count(R3_FINAL) == 0 and heard.count(R3_INTER) == 1 and heard.count(R4_REPLY) == 1, (h1, h2)


# --- ownership overrides the lagged-PostToolUse guard -------------------------------------

def test_lagged_ptu_claimed_inter_before_stop_take_no_double(tmp_path, fake_state_dir, monkeypatch):
    """The Stop scanned [inter, landed] at the old watermark. Before its take, a lagged PostToolUse
    (whose scan saw only the intermediate) claims and speaks the intermediate: watermark moves to
    just past its tool lines. The Stop's take still moves the watermark (past the landed line),
    so it 'owns' through the landed line, and owned_through overrides the read-compare-write that
    exists for exactly this race (comment in _speak_claimed): the intermediate is said twice."""
    t = _stop_spoke_final(tmp_path, "r4-lag")
    start = cli._count_lines(t)
    _append(t, [_assistant_msg("m2", R3_INTER), _tool_use("m2"), _tool_result(),
                _assistant_msg("m3", R3_FINAL), _user("next")])
    ptu_said = []
    def lagged_ptu(state_file=None, lock_dir=None, *rest):
        pass
    real = cli._take_landed
    def w(state_file, lock_dir, *a, **k):
        if not ptu_said:
            # the lagged PostToolUse's own claim of the intermediate (line `start`), up to its tool lines
            assert cli._claim_watermark(state_file, lock_dir, start, start + 3)
            ptu_said.append(R3_INTER)
        return real(state_file, lock_dir, *a, **k)
    monkeypatch.setattr(cli, "_take_landed", w)
    st = _hook(t, "stop", text=R4_REPLY, prompt="p2")
    heard = _heard(ptu_said, st)
    assert heard.count(R3_INTER) == 1, (ptu_said, st)


# --- item 6: prompt predicate, each shape -------------------------------------------------

@pytest.mark.parametrize("line,is_prompt", [
    ({"type": "user", "message": {"content": "a string prompt"}}, True),
    ({"type": "user", "message": {"content": [{"type": "image", "source": {"data": "x"}}]}}, True),
    ({"type": "user", "message": {"content": [{"type": "text", "text": "t"}]}}, True),
    ({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "y"},
                                              {"type": "text", "text": "rides"}]}}, False),
    ({"type": "user", "message": {"content": []}}, False),
    ({"type": "user", "message": {"content": ""}}, False),
    ({"type": "user", "isMeta": True, "message": {"content": "meta string"}}, False),
    ({"type": "user", "isMeta": True, "message": {"content": [{"type": "text", "text": "m"}]}}, False),
    ({"type": "user", "message": {"content": [{"type": "text", "text": ""}]}}, False),
    ({"type": "user", "message": {"content": [{"type": "text", "text": "  "}, {"type": "image", "source": {}}]}}, True),
])
def test_prompt_shapes(tmp_path, line, is_prompt):
    p = tmp_path / "t.jsonl"
    p.write_text(json.dumps(line) + "\n")
    assert (cli._first_prompt_line(p, 0, 1) == 0) is is_prompt


# --- controls with the spoken store off: ownership alone must keep the intermediate single ----



@pytest.mark.parametrize("order", ["h2first", "h1whole", "interleave"])
@pytest.mark.parametrize("kind", ["post_tool_use", "stop"])
def test_store_off_ownership_alone(tmp_path, fake_state_dir, monkeypatch, store_disabled, order, kind):
    t = _stop_spoke_final(tmp_path, f"r4-off-{kind}-{order}")
    (_tail_ptu if kind == "post_tool_use" else _tail_stop)(t)
    kw = {"prompt": "p2"} if kind == "post_tool_use" else {"text": R4_REPLY, "prompt": "p2"}
    h2 = []

    def run2():
        h2.append(_hook(t, kind, **kw))

    if order == "h2first":
        _wrap_take(monkeypatch, before=run2)
    elif order == "interleave":
        _wrap_take(monkeypatch, after=run2)
    h1 = _hook(t, kind, **kw)
    if order == "h1whole":
        run2()
    heard = _heard(h1, *h2)
    assert heard.count(R3_FINAL) == 0 and heard.count(R3_INTER) == 1, (h1, h2)


def test_nonowner_holding_its_claim_while_owner_claims_ptu(tmp_path, fake_state_dir, monkeypatch):
    """H1 takes and owns; H2 (non-owner) runs while H1 is between take and claim, and its own
    claim is still held when H1 claims (stand-in: H2's release is a no-op). With the drop
    branch H2 has nothing to claim; without it H2 claims the intermediate's key and H1 loses."""
    t = _stop_spoke_final(tmp_path, "r4-hold")
    _tail_ptu(t)
    h2 = []
    def run2():
        with monkeypatch.context() as m:
            m.setattr(cli.spoken.Claim, "release", lambda self: None)
            h2.append(_hook(t, "post_tool_use", prompt="p2"))
    _wrap_take(monkeypatch, after=run2)
    h1 = _hook(t, "post_tool_use", prompt="p2")
    heard = _heard(h1, *h2)
    assert heard.count(R3_FINAL) == 0 and heard.count(R3_INTER) == 1, (h1, h2)


def _true_interleave(monkeypatch, run2_inner):
    """Both hooks have read record and watermark; H1's take runs, then H2's take, then H2 finishes,
    then H1 finishes. H1's take call starts H2; H2's take call performs H1's take first."""
    real = cli._take_landed
    st = {"n": 0, "h1_owned": None}
    def w(*a, **k):
        st["n"] += 1
        if st["n"] == 1:              # H1 arrives at its take; H2 runs whole now, up to and past its take
            run2_inner()
            return st["h1_owned"]
        if st["n"] == 2:              # H2 arrives at its take: H1's take happens first
            st["h1_owned"] = real(*st["h1_args"][0], **st["h1_args"][1])
            return real(*a, **k)
        return real(*a, **k)
    def first(*a, **k):
        st["h1_args"] = (a, k)
        return w(*a, **k)
    monkeypatch.setattr(cli, "_take_landed", lambda *a, **k: first(*a, **k) if st["n"] == 0 else w(*a, **k))
    return st


@pytest.mark.parametrize("hold", [False, True])
@pytest.mark.parametrize("store", ["on", "off"])
@pytest.mark.parametrize("kind", ["post_tool_use", "stop"])
def test_true_interleave_h1take_h2take_h2speak_h1speak(tmp_path, fake_state_dir, monkeypatch, kind, store, hold):
    if store == "off":
        monkeypatch.setattr(cli.spoken, "claim", lambda *a, **k: cli.spoken.Claim(None))
    t = _stop_spoke_final(tmp_path, f"r4-ti-{kind}-{store}-{hold}")
    (_tail_ptu if kind == "post_tool_use" else _tail_stop)(t)
    kw = {"prompt": "p2"} if kind == "post_tool_use" else {"text": R4_REPLY, "prompt": "p2"}
    h2 = []
    def run2():
        with monkeypatch.context() as m:
            if hold:
                m.setattr(cli.spoken.Claim, "release", lambda self: None)
            h2.append(_hook(t, kind, **kw))
    st = _true_interleave(monkeypatch, run2)
    h1 = _hook(t, kind, **kw)
    assert st["n"] == 2, st
    heard = _heard(h1, *h2)
    assert heard.count(R3_FINAL) == 0 and heard.count(R3_INTER) == 1, (h1, h2)


# --- 4. Short text and contained text: no false match -----------------------------------------


def test_case4_short_and_contained_texts_are_different_utterances(tmp_path, fake_state_dir):
    transcript = _turn(tmp_path, "c4")
    _append(transcript, [_assistant_msg("m1b", "Running the tests to confirm the fix works."), _tool_use("m1b"),
                         _tool_result()])
    assert _hook(transcript, "post_tool_use") == ["Running the tests to confirm the fix works."]
    assert _hook(transcript, "stop", text="the tests to confirm the fix", prompt="p1") == [
        "the tests to confirm the fix"
    ]
    _append(transcript, [_assistant_msg("m2", "the tests to confirm the fix"), _user("next"),
                         _assistant_msg("m3", "the tests to confirm the fix, and the docs too"), _tool_use("m3"),
                         _tool_result()])
    assert _hook(transcript, "post_tool_use") == ["the tests to confirm the fix, and the docs too"]


# --- (a) "Done." twice in one session, two turns, inside the TTL: both speak ------------------
# v2 says 60 s apart; that passes under any key once the 30 s TTL has run out, so the turns
# here are seconds apart, where only the prompt_id tells them apart. "Done." itself is under
# the hook's 10-character floor, so the text is a longer one that is just as repeatable.


def _case_a(tmp_path, name):
    transcript = _turn(tmp_path, name)
    done = "All done, the tests pass."
    first = _hook(transcript, "stop", text=done, prompt="p1")
    _append(transcript, [_assistant_msg("m2", done), _user("and the other one"), _tool_use("m3"), _tool_result()])
    assert _hook(transcript, "post_tool_use") == [], "the landed line of turn one is the record's"
    second = _hook(transcript, "stop", text=done, prompt="p2")
    return [first, second]


def test_case_a_the_same_words_in_two_turns_both_speak(tmp_path, fake_state_dir):
    assert _case_a(tmp_path, "ca") == [["All done, the tests pass."]] * 2


def test_case_a_the_same_words_in_the_next_turn_with_no_tool_call_both_speak(tmp_path, fake_state_dir):
    """Review of 1e92b8e: with no tool call between, the turn-two Stop is the first hook to
    see turn one's landed line; it takes the line, and the record of turn one (same words)
    must not be taken for turn two's response."""
    transcript = _turn(tmp_path, "ca-notool")
    done = "All done, the tests pass."
    first = _hook(transcript, "stop", text=done, prompt="p1")
    _append(transcript, [_assistant_msg("m2", done), _user("and the other one")])
    second = _hook(transcript, "stop", text=done, prompt="p2")
    assert [first, second] == [[done], [done]]
    # and turn two's own line, when it lands, is still skipped once
    _append(transcript, [_assistant_msg("m3", done), _user("next"), _tool_use("m4"), _tool_result()])
    assert _hook(transcript, "post_tool_use") == []


def test_case_a_control_a_content_only_key_drops_the_second_turn(tmp_path, fake_state_dir, content_only_key):
    assert _case_a(tmp_path, "cax") == [["All done, the tests pass."], []]


# --- (a') "Done." in two rooms within 30 s: both speak on the daemon side ---------------------


def _qmsg(mid: str | None, project: str, text: str = "Done.") -> dict:
    m: dict = {"project": project, "text": text, "session_id": "s"}
    if mid is not None:
        m["id"] = mid
    return m


def test_case_a_prime_two_rooms_and_two_messages_saying_done_both_speak(tmp_path):
    assert d.claim_message(_qmsg("aaaa", "room-a")) is not None
    assert d.claim_message(_qmsg("bbbb", "room-b")) is not None
    # Two different messages in one project, the review's case against (project, text):
    assert d.claim_message(_qmsg("cccc", "room-a")) is not None


def test_case_a_prime_control_a_content_only_key_drops_the_second_room(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "message_claim_key", lambda m: (spoken.digest(spoken.normalize(m["text"])), "x"))
    assert d.claim_message(_qmsg("aaaa", "room-a")) is not None
    assert d.claim_message(_qmsg("bbbb", "room-b")) is None


def test_daemon_drops_a_double_of_one_message_id_and_logs_it(tmp_path):
    log_file = tmp_path / "daemon.log"
    with patch.object(d, "LOG_FILE", log_file):
        assert d.claim_message(_qmsg("dddd", "room-a")) is not None
        assert d.claim_message(_qmsg("dddd", "room-a")) is None
        assert d.claim_message(_qmsg(None, "room-a", "said without an id")) is not None
        assert d.claim_message(_qmsg(None, "room-a", "said  without an ID\n")) is None
    lines = log_file.read_text().splitlines()
    drops = [ln for ln in lines if "Duplicate dropped:" in ln]
    assert len(drops) == 2 and all("[INFO]" in ln for ln in drops)
    assert "message dddd" in drops[0]
    assert "without an id (keyed by project and text)" in drops[1]
    assert "said" not in "".join(drops), "a drop line carries no text"
    assert d.log_stats(lines)["duplicates"] == 2
    assert "duplicates dropped: 2" in d.format_log_stats(d.log_stats(lines), "daemon.log", 10)


def _two_copies_of_one_message(queue_dir: Path) -> None:
    for i in range(2):  # a replay or a bridge double: same id, its own file
        ts = time.time() + i * 0.01
        (queue_dir / f"{ts:.6f}_same{i}.json").write_text(json.dumps(
            {"id": "same-id", "timestamp": ts, "session_id": "s", "project": "p", "text": "one message, sent twice"}
        ))


def _run_loop_until(queue_dir: Path, patches, done) -> None:
    for p in patches:
        p.start()
    runner = threading.Thread(target=lambda: (setattr(d, "_shutdown_requested", False), d.daemon_loop()),
                              daemon=True)
    try:
        d._daemon_mode = True
        runner.start()
        for _ in range(200):
            if done():
                break
            time.sleep(0.025)
        time.sleep(0.1)
    finally:
        stop_loop(runner)
        for p in patches:
            p.stop()


def _counting_player(patches: list) -> list[str]:
    played: list[str] = []
    real_play = d.daemon_play_audio

    def counting_play(wav, *a, **k):
        played.append(str(wav))
        return real_play(wav, *a, **k)

    patches.append(patch.object(d, "daemon_play_audio", side_effect=counting_play))
    return played


def test_daemon_loop_plays_a_doubled_message_once(tmp_path):
    """The prefetch may synthesize the double while the first plays (it claims nothing); the
    claim at dequeue is what keeps it from playing."""
    state_dir, queue_dir, synthesized, patches = loop_harness(tmp_path, {})
    played = _counting_player(patches)
    _two_copies_of_one_message(queue_dir)
    _run_loop_until(queue_dir, patches, lambda: not list(queue_dir.glob("*.json")))
    assert len(played) == 1
    assert "Duplicate dropped: p, message same-id, already spoken" in (state_dir / "daemon.log").read_text()


def test_daemon_loop_control_without_the_store_plays_it_twice(tmp_path, monkeypatch):
    monkeypatch.setattr(spoken, "claim", lambda *a, **k: spoken.Claim(None))
    state_dir, queue_dir, synthesized, patches = loop_harness(tmp_path, {})
    played = _counting_player(patches)
    _two_copies_of_one_message(queue_dir)
    _run_loop_until(queue_dir, patches, lambda: not list(queue_dir.glob("*.json")) and len(played) == 2)
    assert len(played) == 2


# --- (b) The hook claims, the queue write raises: the next hook speaks the text --------------


def _case_b(tmp_path, name):
    transcript = _turn(tmp_path, name)
    final = "the reply whose queue write failed"

    def broken(t, cfg):
        raise OSError("queue directory is gone")

    with pytest.raises(OSError):
        _hook(transcript, "stop", text=final, prompt="p1", speak_effect=broken)
    retry = _hook(transcript, "stop", text=final, prompt="p1")
    return transcript, final, retry


def test_case_b_a_failed_queue_write_gives_the_claim_back(tmp_path, fake_state_dir):
    _, final, retry = _case_b(tmp_path, "cb")
    assert retry == [final]


def test_case_b_control_a_claim_never_given_back_silences_the_retry(tmp_path, fake_state_dir, monkeypatch):
    monkeypatch.setattr(spoken.Claim, "release", lambda self: None)
    _, _, retry = _case_b(tmp_path, "cbx")
    assert retry == []


def test_case_b_after_a_failure_the_landed_line_is_spoken_not_skipped(tmp_path, fake_state_dir):
    transcript = _turn(tmp_path, "cb2")
    final = "the reply whose queue write failed"
    assert _hook(transcript, "stop", text=final, prompt="p1", speak_effect=lambda t, c: False) == []
    assert not list(fake_state_dir.glob("*.pending")), "a record of a reply nobody spoke is dropped"
    _append(transcript, [_assistant_msg("m2", final), _user("next"), _tool_use("m3"), _tool_result()])
    assert _hook(transcript, "post_tool_use") == [final]


# --- 5. Replay of a paused message: not a duplicate -------------------------------------------


def test_case5_the_replay_of_a_paused_message_plays(tmp_path):
    state_dir, queue_dir, synthesized, patches = loop_harness(tmp_path, {})
    played: list[str] = []
    real_play = d.daemon_play_audio

    def generate_then_pause(text, persona, output_file, **kw):
        import wave

        synthesized.append(text)
        with wave.open(str(output_file), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(8000)
            w.writeframes(b"\x00\x00" * 800)
        if len(synthesized) == 1:
            st.set_paused(True, by="user")  # lands during synthesis: held before playback
        return True

    def counting_play(wav, *a, **k):
        played.append(str(wav))
        return real_play(wav, *a, **k)

    patches = [p for p in patches if getattr(p, "attribute", "") != "daemon_generate_speech"]
    patches += [patch.object(d, "daemon_generate_speech", side_effect=generate_then_pause),
                patch.object(d, "daemon_play_audio", side_effect=counting_play)]
    f = queue_dir / f"{time.time():.6f}_r.json"
    f.write_text(json.dumps({"id": "paused-id", "timestamp": time.time(), "session_id": "s", "project": "p",
                             "text": "held, then replayed"}))

    def unpause_when_held():
        for _ in range(200):
            held = st.read_playback_state().get("current_message") or {}
            if held.get("text") == "held, then replayed" and st.read_playback_state().get("paused"):
                st.set_paused(False)
                return
            time.sleep(0.02)

    helper = threading.Thread(target=unpause_when_held)
    helper.start()
    try:
        _run_loop_until(queue_dir, patches, lambda: len(played) >= 1)
    finally:
        helper.join(timeout=5)
    log = (state_dir / "daemon.log").read_text()
    assert synthesized == ["held, then replayed"] * 2 and len(played) == 1, log
    assert "Duplicate dropped" not in (state_dir / "daemon.log").read_text()


# --- 6. TTL at claim ---------------------------------------------------------------------------


def test_case6_a_claim_older_than_its_ttl_is_taken_over_and_a_younger_one_refused(tmp_path):
    key = spoken.digest("k")
    first = spoken.claim(tmp_path, key, 30.0)
    assert first is not None and spoken.claim(tmp_path, key, 30.0) is None
    path = tmp_path / key
    os.utime(path, (time.time() - 29, time.time() - 29))
    assert spoken.claim(tmp_path, key, 30.0) is None, "29 s: still fresh"
    os.utime(path, (time.time() - 31, time.time() - 31))
    again = spoken.claim(tmp_path, key, 30.0)
    assert again is not None and time.time() - path.stat().st_mtime < 5
    assert _names(tmp_path) == [key], "the moved stale copy is gone"


def test_case6_the_daemon_ttl_is_the_one_written_in_the_claim(tmp_path):
    key = spoken.digest("long message")
    c = spoken.claim(tmp_path, key, spoken.DAEMON_MIN_TTL_S)
    assert c is not None
    c.refresh(120.0)  # after a two-minute WAV ended
    os.utime(tmp_path / key, (time.time() - 60, time.time() - 60))
    assert spoken.claim(tmp_path, key, spoken.DAEMON_MIN_TTL_S) is None
    os.utime(tmp_path / key, (time.time() - 121, time.time() - 121))
    assert spoken.claim(tmp_path, key, spoken.DAEMON_MIN_TTL_S) is not None


# --- Two claimants on one stale file: exactly one claim (review of v2, change 3) -------------


def _stale(directory: Path) -> tuple[str, Path]:
    key = spoken.digest("stale")
    assert spoken.claim(directory, key, 30.0) is not None
    path = directory / key
    os.utime(path, (time.time() - 100, time.time() - 100))
    return key, path


def _names(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if p.name != ".lock")


def _a_stops_at_the_seam_then_b(tmp_path, monkeypatch) -> tuple[dict[str, bool], bool, Path, str]:
    """A judges the claim stale and stops there; B arrives on the same stale file; then A
    goes on. Returns who won, whether B was still waiting when A was let go, the path, key."""
    key, path = _stale(tmp_path)
    at_seam, go = threading.Event(), threading.Event()
    results: dict[str, bool] = {}

    def seam() -> None:
        if threading.current_thread().name == "A":
            at_seam.set()
            assert go.wait(5)

    monkeypatch.setattr(spoken, "_after_stale_stat", seam)

    def claimant() -> None:
        results[threading.current_thread().name] = spoken.claim(tmp_path, key, 30.0) is not None

    a = threading.Thread(target=claimant, name="A")
    b = threading.Thread(target=claimant, name="B")
    a.start()
    assert at_seam.wait(5)
    b.start()
    b.join(timeout=0.3)
    b_waited = b.is_alive()
    go.set()
    a.join(timeout=5)
    b.join(timeout=5)
    return results, b_waited, path, key


def test_two_claimants_on_one_stale_file_one_wins(tmp_path, monkeypatch):
    results, b_waited, path, key = _a_stops_at_the_seam_then_b(tmp_path, monkeypatch)
    assert b_waited, "B waits for A's takeover instead of racing it"
    assert results == {"A": True, "B": False}
    assert path.exists() and time.time() - path.stat().st_mtime < 5
    assert _names(tmp_path) == [key]


def test_two_claimants_control_without_the_lock_both_win(tmp_path, monkeypatch):
    """The real _take_over with the lock acquire made a no-op: B takes the stale file over
    while A waits at the seam, then A renames B's fresh claim away and both speak."""
    monkeypatch.setattr(spoken.fcntl, "flock", lambda fd, op: None)
    results, b_waited, _, _ = _a_stops_at_the_seam_then_b(tmp_path, monkeypatch)
    assert not b_waited
    assert results == {"A": True, "B": True}


def test_a_stalled_claimant_cannot_release_or_refresh_the_claim_that_took_its_place(tmp_path):
    """A claims, stalls past the TTL, C takes over and speaks, then A's write fails and A
    gives its claim back: that must not remove C's, or C's twin speaks again."""
    key = spoken.digest("stalled")
    a = spoken.claim(tmp_path, key, 30.0)
    assert a is not None
    os.utime(tmp_path / key, (time.time() - 31, time.time() - 31))
    c = spoken.claim(tmp_path, key, 30.0)
    assert c is not None
    before = (tmp_path / key).read_text()
    a.refresh(999.0)
    a.release()
    assert (tmp_path / key).read_text() == before, "C's claim is untouched"
    assert spoken.claim(tmp_path, key, 30.0) is None, "C's twin is still refused"
    c.release()
    assert spoken.claim(tmp_path, key, 30.0) is not None, "C's own release still works"


def test_two_claimants_threads_on_one_stale_file_one_wins(tmp_path):
    """No seam: many threads on one stale file, many rounds. Exactly one claim per round."""
    for _round in range(30):
        key, _ = _stale(tmp_path)
        barrier = threading.Barrier(6)
        won: list[bool] = []

        def go(key: str = key, barrier: threading.Barrier = barrier, won: list[bool] = won) -> None:
            barrier.wait()
            won.append(spoken.claim(tmp_path, key, 30.0) is not None)

        threads = [threading.Thread(target=go) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert won.count(True) == 1, won
        for p in tmp_path.iterdir():
            p.unlink()


def test_a_stale_file_that_vanishes_mid_takeover_is_claimed_once(tmp_path, monkeypatch):
    key, path = _stale(tmp_path)
    monkeypatch.setattr(spoken, "_after_stale_stat", lambda: path.unlink())  # released or pruned meanwhile
    assert spoken.claim(tmp_path, key, 30.0) is not None
    assert spoken.claim(tmp_path, key, 30.0) is None


def test_prune_keeps_the_lock_file_however_old(tmp_path):
    key = spoken.digest("k")
    c = spoken.claim(tmp_path, key, 30.0)
    assert c is not None
    c.release()  # creates the lock file
    lock = tmp_path / ".lock"
    assert lock.exists()
    os.utime(lock, (time.time() - 99999, time.time() - 99999))
    spoken.prune(tmp_path)
    assert lock.exists()


def test_prune_removes_only_old_claims(tmp_path):
    old, fresh = spoken.digest("old"), spoken.digest("fresh")
    spoken.claim(tmp_path, old, 30.0)
    spoken.claim(tmp_path, fresh, 30.0)
    os.utime(tmp_path / old, (time.time() - 4000, time.time() - 4000))
    assert spoken.prune(tmp_path) == 1
    assert _names(tmp_path) == [fresh]


# --- 7. Modes 0700 and 0600, no text on disk ---------------------------------------------------


def test_case7_the_hook_store_is_private_and_holds_no_text(tmp_path, fake_state_dir):
    transcript = _turn(tmp_path, "c7")
    secret = "the quarterly numbers are in the attached draft"
    assert _hook(transcript, "stop", text=secret, prompt="p1") == [secret]
    store = spoken.hook_dir()
    assert store.parent == Path(os.environ["TMPDIR"])
    assert stat.S_IMODE(store.stat().st_mode) == 0o700
    entries = [e for e in store.iterdir() if e.name != ".lock"]
    assert entries
    for e in entries:
        assert stat.S_IMODE(e.stat().st_mode) == 0o600
        assert len(e.name) == 64 and all(c in "0123456789abcdef" for c in e.name)
        body = e.read_text()
        assert "quarterly" not in body and "opening" not in body
        ttl, token = body.split()
        float(ttl)  # the TTL and the claimant's random token, nothing else
        assert len(token) == 16 and all(c in "0123456789abcdef" for c in token)


def test_case7_tmpdir_unset_falls_to_tmp(monkeypatch):
    monkeypatch.delenv("TMPDIR", raising=False)
    assert spoken.hook_dir() == Path("/tmp/claude-tts-spoken")
    monkeypatch.setenv("TMPDIR", "")
    assert spoken.hook_dir() == Path("/tmp/claude-tts-spoken")


def test_a_directory_someone_else_could_write_is_not_used(tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    link = tmp_path / "store"
    link.symlink_to(target)
    c = spoken.claim(link, spoken.digest("x"), 30.0)
    assert c is not None and not c.stored and not list(target.iterdir())


# --- 8. One normalization, both paths ----------------------------------------------------------

VECTOR = "Fixed it.\n\n```py\nx  =  1\n```\n| Größe | État |\n|---|---|\nÄRGER straße done.\n"


def test_case8_the_normalization_vector():
    assert spoken.normalize(VECTOR) == "fixed it. ```py x = 1 ``` | grösse | état | |---|---| ärger strasse done."
    assert spoken.normalize(VECTOR) == spoken.normalize("  " + VECTOR.replace("\n", " \n ").upper())


def test_case8_input_path_and_file_path_share_one_key(tmp_path, fake_state_dir, monkeypatch):
    """Twin A speaks VECTOR from its input; twin B finds it landed in the file, rewrapped."""
    transcript = _turn(tmp_path, "c8")
    landed = VECTOR.replace("\n\n", "\n").rstrip("\n")
    a = _hook(transcript, "stop", text=VECTOR, prompt="p1")
    _append(transcript, [_assistant_msg("m2", landed)])
    monkeypatch.setattr(cli, "_read_pending_record", lambda _p: ("", ""))
    b = _hook(transcript, "stop", text=VECTOR, prompt="p1")
    assert len(a) == 1 and b == []
    k_in = spoken.utterance_key("sess-1", "prompt:p1", VECTOR.strip())
    k_file = spoken.utterance_key("sess-1", "prompt:p1", landed)
    assert k_in == k_file
    assert cli._pending_key(VECTOR) == cli._pending_key(landed)
    assert cli._landed_match(landed, cli._pending_key(VECTOR)) == ""


def test_landed_match_cuts_at_a_word_boundary_when_casefold_changes_length():
    spoken_block = "Die Straße ist frei."
    landed = "Erster Block. " + spoken_block
    assert cli._landed_match(landed, cli._pending_key(spoken_block)) == "Erster Block."


# --- Review of 1e92b8e: the daemon's claim is given back by the loop's catch-all -------------


def test_an_exception_between_claim_and_playback_plays_the_message_on_the_next_pass(tmp_path):
    state_dir, queue_dir, synthesized, patches = loop_harness(tmp_path, {})
    played = _counting_player(patches)
    real_prepare = d.prepare_message
    calls = {"n": 0}

    def prepare_fails_once(msg, raw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom between the claim and the player")
        return real_prepare(msg, raw)

    patches.append(patch.object(d, "prepare_message", side_effect=prepare_fails_once))
    (queue_dir / f"{time.time():.6f}_x.json").write_text(json.dumps(
        {"id": "retry-id", "timestamp": time.time(), "session_id": "s", "project": "p", "text": "plays on retry"}
    ))
    _run_loop_until(queue_dir, patches, lambda: len(played) >= 1)
    log = (state_dir / "daemon.log").read_text()
    assert "Error in daemon loop: boom" in log
    assert len(played) == 1, log
    assert "Duplicate dropped" not in log


def test_the_daemon_restarts_the_claim_when_playback_ends(tmp_path):
    """The claim's TTL becomes max(30 s, WAV duration) and counts from the end of playback."""
    import wave

    state_dir, queue_dir, synthesized, patches = loop_harness(tmp_path, {})
    played = _counting_player(patches)
    ended: list[float] = []

    def forty_seconds(text, persona, output_file, **kw):
        synthesized.append(text)
        with wave.open(str(output_file), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(1000)
            w.writeframes(b"\x00\x00" * 40000)
        return True

    real_play = d.daemon_play_audio

    def play_then_mark(wav, *a, **k):
        played.append(str(wav))
        r = real_play(wav, *a, **k)
        ended.append(time.time())
        return r

    patches = [p for p in patches if getattr(p, "attribute", "") not in ("daemon_generate_speech", "daemon_play_audio")]
    patches += [patch.object(d, "daemon_generate_speech", side_effect=forty_seconds),
                patch.object(d, "daemon_play_audio", side_effect=play_then_mark)]
    (queue_dir / f"{time.time():.6f}_y.json").write_text(json.dumps(
        {"id": "long-id", "timestamp": time.time(), "session_id": "s", "project": "p", "text": "a long one"}
    ))
    _run_loop_until(queue_dir, patches, lambda: bool(ended))
    claim_file = st.SPOKEN_DIR / spoken.digest("queue-id", "long-id")
    ttl, _ = spoken._read_claim(claim_file)
    assert ttl is not None and abs(ttl - 40.0) < 0.5
    assert claim_file.stat().st_mtime >= ended[0] - 0.05
    for e in st.SPOKEN_DIR.iterdir():  # the daemon's store, visible through the mount: no text
        if e.name != ".lock":
            assert len(e.name) == 64 and "long" not in e.read_text()


def test_an_unusable_hook_store_is_said_once_at_info(tmp_path, fake_state_dir, monkeypatch):
    """Review of 1e92b8e: another user's /tmp/claude-tts-spoken turns dedupe off; say it once."""
    elsewhere = tmp_path / "not-ours"
    elsewhere.mkdir()
    spoken.hook_dir().symlink_to(elsewhere)
    lines: list[str] = []
    monkeypatch.setattr(cli, "debug", lines.append)
    transcript = _turn(tmp_path, "c6u")
    _append(transcript, [_user("more"), _tool_use("m5"), _tool_result()])
    _hook(transcript, "stop", text="a reply spoken while the store is unusable", prompt="p1")
    notices = [ln for ln in lines if ln.startswith("INFO: spoken store unusable")]
    assert len(notices) == 1 and "reply" not in notices[0]
