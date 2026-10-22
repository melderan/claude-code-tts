"""Count daemon_loop passes, so a test waits passes of the loop instead of seconds of the clock.

The loop calls write_heartbeat first on every pass; only those calls count (the player also
writes it once a second, from daemon_play_audio), so a wait for passes also waits out a
playback. A test that checks something did NOT
happen ("held: nothing may play") after a fixed sleep proves nothing on a starved runner: the
loop may not have looked yet, so the check passes for free. A test that checks something DID
happen after a fixed sleep fails there. Waiting until the loop has run a few more passes since
the event makes both mean what they say on any runner. loop_harness in test_pause_ledger
installs the counter; scripts/sleep-race-check.py refuses the fixed-sleep shape.
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable
from typing import Any

_count = 0
_lock = threading.Lock()


def counting(real: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap write_heartbeat so each call from daemon_loop counts one pass."""

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        global _count
        if sys._getframe(1).f_code.co_name == "daemon_loop":
            with _lock:
                _count += 1
        return real(*args, **kwargs)

    return wrapper


def passes() -> int:
    with _lock:
        return _count


def wait_for(pred: Callable[[], bool], timeout: float = 5.0) -> bool:
    """Poll pred until true or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def wait_passes(k: int = 3, timeout: float = 5.0) -> bool:
    """True once the loop has begun k more passes than when called.

    The pass running at the call may have read the state before the event; the one after it
    began after the event, so k=3 leaves one full pass of margin on top of that.
    """
    target = passes() + k
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if passes() >= target:
            return True
        time.sleep(0.01)
    return passes() >= target
