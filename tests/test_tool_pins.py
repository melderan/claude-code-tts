"""The checkers the gate and ci run are pinned, and the justfile and the gate pin the same versions.

ty 0.0.99 renamed a rule and failed `ty check` on six untouched comments (9.86.3): an unpinned
checker changes its verdict on code nobody changed. A new version comes in by editing the pin,
in both places, on purpose.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHECKERS = ("ruff", "mypy", "ty")


def _pins(text: str) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for tool, version in re.findall(r'uv"?,? "?tool"?,? "?run"?,? "?([a-z]+)(?:@([0-9][0-9.]*))?', text):
        found.setdefault(tool, set()).add(version)
    return found


def test_every_checker_is_pinned_and_both_places_agree() -> None:
    just = _pins((ROOT / "justfile").read_text())
    gate = _pins((ROOT / "scripts" / "gate.py").read_text())
    for tool in CHECKERS:
        assert tool in just and tool in gate, f"{tool} is not run by both the justfile and the gate"
        assert "" not in just[tool] | gate[tool], f"{tool} runs unpinned somewhere"
        assert len(just[tool] | gate[tool]) == 1, f"{tool} pinned to different versions: {just[tool] | gate[tool]}"
