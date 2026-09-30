"""play_order: the background lane speaks after everything else, timestamps hold within a lane."""

from __future__ import annotations

from pathlib import Path

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
