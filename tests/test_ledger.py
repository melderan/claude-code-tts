"""The voice ledger: every reply a hook accepted, in full and before the filter; outcomes joined by id.

The control behind these tests is the speech ring in handy.py, which keeps 50 rows and 500
characters: the ledger must keep the 51st and the 501st.
"""

from __future__ import annotations

import argparse
import io
import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import claude_code_tts.daemon as d
import claude_code_tts.msgqueue as mq
from claude_code_tts import ledger
from claude_code_tts.cli import _speak_from_hook, cmd_ledger
from claude_code_tts.config import TTSConfig

LONG = "word " * 200  # 1,000 characters: a short reply, twice what the speech ring keeps
SESSION = "alice--claude--x"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def queue_dir(tmp_path, monkeypatch):
    qd = tmp_path / "queue"
    monkeypatch.setattr(mq, "QUEUE_DIR", qd)
    return qd


class TestAppendOnly:
    def test_fifty_one_entries_stay_fifty_one_in_full(self, home):
        for i in range(51):
            assert ledger.record(SESSION, {"id": f"{i:02d}", "text": f"utterance {i:02d} " + LONG})
        rows = ledger.read(SESSION)
        assert len(rows) == 51
        assert rows[0]["id"] == "00" and rows[-1]["id"] == "50"
        assert all(len(r["text"]) == len("utterance 00 " + LONG) for r in rows)
        assert (home / ".claude" / "voice-ledger" / f"{SESSION}.jsonl").is_file()

    def test_the_module_has_no_way_to_delete_or_shorten(self):
        src = Path(ledger.__file__).read_text()
        for word in ("unlink", "DELETE", "truncate", ".remove(", "rmtree", "[:500]", "write_text"):
            assert word not in src, word

    def test_an_unwritable_directory_returns_false_and_raises_nothing(self, tmp_path):
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("x")
        assert ledger.record("s", {"id": "a", "text": "t"}, directory=blocker) is False
        assert ledger.record_outcome("s", "a", "played", directory=blocker) is False

    def test_a_torn_tail_line_is_skipped_and_the_rows_before_it_stand(self, home):
        ledger.record("s", {"id": "a", "text": "one"})
        with open(home / ".claude" / "voice-ledger" / "s.jsonl", "a") as f:
            f.write('{"id": "b", "text": "cut off')
        assert [r["id"] for r in ledger.read("s")] == ["a"]

    def test_session_ids_become_safe_file_names(self, home):
        ledger.record("../evil/..", {"id": "a", "text": "t"})
        assert ledger.sessions() == ["_evil_"]


class TestOutcomesJoin:
    def test_the_latest_outcome_of_an_id_joins_the_row(self, home):
        ledger.record("s", {"id": "a", "text": "one"})
        ledger.record("s", {"id": "b", "text": "two"})
        ledger.record_outcome("s", "a", "dropped", "superseded")
        ledger.record_outcome("s", "a", "played", played_s=3.2)
        rows = {r["id"]: r for r in ledger.read("s")}
        assert rows["a"]["outcome"] == "played" and "outcome_reason" not in rows["a"]
        assert "outcome" not in rows["b"]
        assert (home / ".claude-tts" / "ledger" / "s.outcomes.jsonl").is_file()
        assert [o["outcome"] for o in ledger.outcomes("s")] == ["dropped", "played"]

    def test_limit_keeps_the_newest_rows(self, home):
        for i in range(5):
            ledger.record("s", {"id": str(i), "text": "t"})
        assert [r["id"] for r in ledger.read("s", limit=2)] == ["3", "4"]
        assert len(ledger.read("s", limit=0)) == 5
        assert ledger.sessions() == ["s"]


def _transcript(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        {"type": "user", "message": {"content": [{"type": "text", "text": "hi"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}},
    ]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))


@pytest.fixture
def hook_state(tmp_path, monkeypatch):
    """The hook's spoken records and watermark locks under /tmp go to tmp_path, as test_watermark does.

    They are keyed by the transcript's stem, so without this a second run of the suite finds the
    first run's record and speaks nothing.
    """
    real_path = Path
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    def fake_path(arg):
        s = str(arg)
        if s.startswith("/tmp/claude_tts_spoken_") or s.startswith("/tmp/claude_tts_wm_"):
            return state_dir / real_path(s).name
        return real_path(arg)

    monkeypatch.setattr("claude_code_tts.cli.Path", fake_path)
    return state_dir


class TestTheHookWritesTheWords:
    RAW = "Done. The config is at ~/vault/tmp/config.json and `pytest -q` passed, see src/a.py:12. " * 3

    def _run(self, transcript: Path) -> None:
        payload = json.dumps(
            {"transcript_path": str(transcript), "session_id": "cc-1", "last_assistant_message": self.RAW}
        )
        cfg = TTSConfig(mode="queue", muted=False, intermediate=True, session_id=SESSION, project_name="x")
        with (
            patch("sys.stdin", io.StringIO(payload)),
            patch("claude_code_tts.cli.load_config", return_value=cfg),
            patch("claude_code_tts.audio.daemon_healthy", return_value=True),
            patch("claude_code_tts.session.pin_session"),
        ):
            _speak_from_hook(argparse.Namespace(hook_type="stop"))

    def test_the_ledger_holds_the_raw_reply_and_the_queue_message_carries_its_id(self, home, queue_dir, hook_state):
        transcript = home / ".claude" / "projects" / "-ledger-one" / "t1.jsonl"
        _transcript(transcript, self.RAW)
        self._run(transcript)
        rows = ledger.read("-ledger-one")  # the hook keys the ledger by the session it detected
        assert len(rows) == 1
        row = rows[0]
        assert row["text"] == self.RAW
        assert row["event"] == "stop" and row["claude_session"] == "cc-1" and row["transcript"] == "t1.jsonl"
        msgs = [json.loads(p.read_text()) for p in queue_dir.glob("*.json")]
        assert len(msgs) == 1
        assert msgs[0]["id"] == row["id"]
        assert msgs[0]["text"] != self.RAW and "~/" not in msgs[0]["text"]  # the queue holds the spoken form
        assert row["spoken_chars"] == len(msgs[0]["text"])

    def test_a_failed_ledger_write_costs_no_speech(self, home, queue_dir, hook_state):
        transcript = home / ".claude" / "projects" / "-ledger-two" / "t2.jsonl"
        _transcript(transcript, self.RAW)
        blocker = home / ".claude" / "voice-ledger"
        blocker.parent.mkdir(parents=True, exist_ok=True)
        blocker.write_text("in the way")
        self._run(transcript)
        assert len(list(queue_dir.glob("*.json"))) == 1


def _queued(n: int, session: str = SESSION, age: float = 0.0, **fields) -> list[dict]:
    msgs = []
    for i in range(n):
        f, m = mq.write_message(
            {"session_id": session, "project": "x", "text": f"t{i}", "timestamp": time.time() - age + i, **fields}
        )
        m["_file"] = f
        msgs.append(m)
    return msgs


class TestTheDaemonWritesTheOutcome:
    def test_a_depth_trim_is_dropped_depth(self, home, queue_dir):
        msgs = _queued(3)
        first_two = {m["id"] for m in msgs[:2]}
        assert d.enforce_max_depth(1, None, msgs) == 2
        outs = ledger.outcomes(SESSION)
        assert [(o["outcome"], o["reason"]) for o in outs] == [("dropped", "depth")] * 2
        assert {o["id"] for o in outs} == first_two

    def test_an_aged_out_message_is_dropped_expired(self, home, queue_dir):
        msgs = _queued(1, age=1000)
        assert d.cleanup_old_messages(10, None, msgs) == 1
        assert [(o["outcome"], o["reason"]) for o in ledger.outcomes(SESSION)] == [("dropped", "expired")]

    def test_a_superseded_message_is_dropped_superseded(self, home, queue_dir):
        msgs = _queued(3)
        supersedes = mq.Supersedes()
        with patch.object(supersedes, "apply", return_value=[msgs[2]]):
            kept = d.drop_superseded(msgs, supersedes, None)
        assert kept == [msgs[2]]
        outs = ledger.outcomes(SESSION)
        assert [(o["outcome"], o["reason"]) for o in outs] == [("dropped", "superseded")] * 2
        assert {o["id"] for o in outs} == {msgs[0]["id"], msgs[1]["id"]}

    def test_bridge_and_control_messages_write_no_outcome(self, home, queue_dir):
        msgs = _queued(2, source="page") + _queued(1, type="control")
        d.enforce_max_depth(0, None, msgs)
        d._outcome({"type": "control", "id": "c", "session_id": "system"}, "played")
        assert ledger.outcomes(SESSION) == []
        assert not (home / ".claude-tts" / "ledger").exists()

    def test_played_carries_how_much_played(self, home):
        d._outcome({"id": "m1", "session_id": SESSION}, "played", played_s=4.5)
        d._outcome({"id": "m1", "session_id": SESSION}, "failed", "no engine", sentences=2, total=5)
        outs = ledger.outcomes(SESSION)
        assert outs[0]["outcome"] == "played" and outs[0]["played_s"] == 4.5
        assert outs[1]["reason"] == "no engine" and outs[1]["sentences"] == 2


class TestTheCommand:
    def _args(self, **kw):
        base = {"session": SESSION, "last": 20, "sessions": False, "full": False, "json": False}
        base.update(kw)
        return argparse.Namespace(**base)

    def test_rows_print_with_their_outcome_and_length(self, home, capsys):
        ledger.record(SESSION, {"id": "a", "event": "stop", "text": "one two three"})
        ledger.record(SESSION, {"id": "b", "event": "post_tool_use", "text": LONG, "skipped": "too_short"})
        ledger.record_outcome(SESSION, "a", "dropped", "superseded")
        cmd_ledger(self._args())
        out = capsys.readouterr().out.splitlines()
        assert len(out) == 2
        assert "stop" in out[0] and "dropped (superseded)" in out[0] and "13 chars" in out[0]
        assert "skipped" in out[1] and "999 chars" in out[1] and len(out[1]) < 200  # spaces folded, text cut for the eye
        cmd_ledger(self._args(full=True))
        assert LONG.strip() in capsys.readouterr().out

    def test_json_and_sessions(self, home, capsys):
        ledger.record(SESSION, {"id": "a", "event": "stop", "text": "one"})
        cmd_ledger(self._args(json=True))
        assert json.loads(capsys.readouterr().out)[0]["id"] == "a"
        cmd_ledger(self._args(sessions=True))
        assert capsys.readouterr().out.split() == ["1", SESSION]

    def test_no_ledger_says_where_it_looked(self, home, capsys):
        cmd_ledger(self._args(session="nobody"))
        assert "No ledger for nobody" in capsys.readouterr().out
