"""scripts/sleep-race-check.py: a fixed sleep before an assert is a finding, a wait or a reasoned marker is not."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "sleep-race-check.py"
spec = importlib.util.spec_from_file_location("sleep_race_check", SCRIPT)
assert spec is not None and spec.loader is not None
src = importlib.util.module_from_spec(spec)
spec.loader.exec_module(src)


def _found(body: str) -> list[str]:
    return src.check_source("import time\n\ndef test_x():\n" + body, "t.py")


@pytest.mark.parametrize(
    "body",
    [
        # The mic watcher shape fixed in 9.66.1.
        "    w.start()\n    time.sleep(0.2)\n    assert state['paused'] is True\n",
        # The thread starts in a helper; the sleep is the same bet.
        "    stop = _run_loop()\n    time.sleep(0.3)\n    assert spoken == []\n",
        # A read between the sleep and the assert does not make it a wait.
        "    time.sleep(0.4)\n    state = read()\n    assert state['paused']\n",
        # Inside a try body.
        "    try:\n        time.sleep(0.3)\n        assert x\n    finally:\n        stop()\n",
    ],
)
def test_a_fixed_sleep_before_an_assert_is_found(body):
    found = _found(body)
    assert len(found) == 1 and "fixed sleep before an assert in test_x" in found[0], found


@pytest.mark.parametrize(
    "body",
    [
        "    time.sleep(0.2)\n    assert _wait_for(lambda: x)\n",
        "    time.sleep(0.2)\n    assert loop_passes.wait_passes(3)\n",
        "    time.sleep(0.35)  # sleep-ok: the backoff must pass on the clock\n    assert x\n",
        "    time.sleep(0.05)\n    write()\n    assert x\n",
        "    assert loop_passes.wait_passes(3)\n    assert spoken == []\n",
    ],
)
def test_a_wait_a_reasoned_marker_or_an_action_between_is_not(body):
    assert _found(body) == []


def test_a_marker_without_a_reason_is_a_finding():
    found = _found("    time.sleep(0.2)  # sleep-ok:\n    assert x\n")
    assert found == ["t.py:4: # sleep-ok: needs a reason"]


def test_only_test_functions_are_read():
    assert src.check_source("import time\n\ndef helper():\n    time.sleep(1)\n    assert x\n", "t.py") == []


def test_the_suite_is_clean_and_the_exit_code_says_so(tmp_path):
    clean = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True)
    assert clean.returncode == 0, clean.stdout
    bad = tmp_path / "test_bad.py"
    bad.write_text("import time\n\ndef test_x():\n    time.sleep(0.2)\n    assert y\n")
    dirty = subprocess.run([sys.executable, str(SCRIPT), str(bad)], capture_output=True, text=True)
    assert dirty.returncode == 1 and "1 finding(s)" in dirty.stdout
    broken = tmp_path / "test_broken.py"
    broken.write_text("def (:\n")
    assert subprocess.run([sys.executable, str(SCRIPT), str(broken)], capture_output=True).returncode == 2
