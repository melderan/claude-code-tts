"""play_order: the background lane speaks after everything else, timestamps hold within a lane."""

from __future__ import annotations

from pathlib import Path

import claude_code_tts.msgqueue as mq
from claude_code_tts.daemon import next_speakable, play_order


def m(ts: float, **extra: object) -> dict:
    return {"timestamp": ts, "text": f"t{ts}", "_file": Path(f"{ts}.json"), **extra}


def test_background_goes_after_every_other_message() -> None:
    msgs = [m(1.0, lane="background"), m(2.0, lane="background"), m(3.0), m(4.0)]
    assert [x["timestamp"] for x in play_order(msgs)] == [3.0, 4.0, 1.0, 2.0]


def test_order_is_stable_within_a_lane_and_control_stays_first() -> None:
    msgs = [m(1.0, type="control"), m(2.0, lane="background"), m(3.0, lane="background"), m(4.0)]
    ordered = play_order(msgs)
    assert [x["timestamp"] for x in ordered] == [1.0, 4.0, 2.0, 3.0]
    assert ordered[0].get("type") == "control"


def test_unknown_lane_is_foreground() -> None:
    msgs = [m(1.0, lane="background"), m(2.0, lane="whatever")]
    assert [x["timestamp"] for x in play_order(msgs)] == [2.0, 1.0]


def test_prefetch_follows_play_order() -> None:
    msgs = play_order([m(1.0, lane="background"), m(2.0, lane="background"), m(3.0)])
    # Speaking the foreground 3.0 now, the next to synthesize is the oldest background block.
    assert next_speakable(msgs, Path("3.0.json"))["timestamp"] == 1.0  # type: ignore[index]


def test_queued_bridge_jobs_are_reregistered_after_a_restart(tmp_path: Path) -> None:
    import json
    from unittest.mock import patch

    import claude_code_tts.daemon as d
    from claude_code_tts.bridge import JOBS

    q = tmp_path / "queue"
    q.mkdir()
    (q / "1.0_a.json").write_text(
        json.dumps(
            {
                "id": "a1",
                "timestamp": 1.0,
                "text": "x",
                "source": "page",
                "project": "page:doc",
                "persona": "p",
                "lane": "background",
            }
        )
    )
    (q / "2.0_room.json").write_text(
        json.dumps({"id": "r", "timestamp": 2.0, "text": "y", "session_id": "s", "project": "proj"})
    )
    JOBS._jobs.pop("a1", None)
    with patch.object(mq, "QUEUE_DIR", q):
        assert d.register_queued_bridge_jobs() == 1
        assert d.register_queued_bridge_jobs() == 0, "second start does not duplicate"
    job = JOBS.get("a1")
    assert job is not None and job["state"] == "queued" and job["lane"] == "background"
    assert JOBS.get("r") is None, "a room message has no job"
