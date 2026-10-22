#!/usr/bin/env python3
"""sleep-race-check.py - no test bets a fixed sleep on another thread's timing (gate step).

The shape it refuses, in a test function:

    w.start()
    ...
    time.sleep(0.2)
    assert state["paused"] is True

The assert reads state a thread writes, and the sleep bets the thread got there first. On a
loaded runner it did not: ci for e8bb4fd failed on macOS py3.14 that way, and test_hold's mic test
failed the local gate the same way (fixed in 9.66.1). Where the assert says something did NOT
happen, the same bet passes on a starved runner without proving anything. The fix is to wait for
the event or for passes of the loop (tests/loop_passes.py), with a cap.

A finding is a `time.sleep(...)` statement followed in the same block, past any plain
assignments, by an `assert` that does not itself wait (calls nothing named `*wait*` or
`_polled*`). It does not look for the thread: helpers start them (`_run_loop`), and a filter on
`.start()` in the test body missed ten of twenty. A sleep that is not a bet (a deadline, a
backoff or an age that must pass on the clock) is kept by writing `# sleep-ok: <why>` on its
line; a marker without a reason is itself a finding.

    sleep-race-check.py [PATH...]    default: tests/
Exit 0 clean, 1 on any finding, 2 on a file that does not parse.
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Iterator
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MARKER = "# sleep-ok:"


def _is_sleep(stmt: ast.stmt) -> bool:
    if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)):
        return False
    f = stmt.value.func
    return (isinstance(f, ast.Attribute) and f.attr == "sleep") or (isinstance(f, ast.Name) and f.id == "sleep")


def _call_name(node: ast.Call) -> str:
    f = node.func
    return f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else ""


def _waits(stmt: ast.Assert) -> bool:
    """An assert that polls (`assert _wait_for(...)`) is the fix, not the bet."""
    return any(
        isinstance(n, ast.Call) and ("wait" in _call_name(n) or _call_name(n).startswith("_polled"))
        for n in ast.walk(stmt.test)
    )


def _blocks(node: ast.AST) -> Iterator[list[ast.stmt]]:
    for child in ast.walk(node):
        for field in ("body", "orelse", "finalbody"):
            block = getattr(child, field, None)
            if isinstance(block, list) and block and isinstance(block[0], ast.stmt):
                yield block
        for h in getattr(child, "handlers", []):
            yield h.body


def _next_check(block: list[ast.stmt], i: int) -> ast.stmt | None:
    for stmt in block[i + 1:]:
        if not isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            return stmt
    return None


def check_source(src: str, name: str) -> list[str]:
    """Findings for one file's source, as `name:line: message`."""
    lines = src.splitlines()
    tree = ast.parse(src, filename=name)
    found: list[str] = []
    seen: set[int] = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) or not fn.name.startswith("test"):
            continue
        for block in _blocks(fn):
            for i, stmt in enumerate(block):
                if not _is_sleep(stmt) or stmt.lineno in seen:
                    continue
                nxt = _next_check(block, i)
                if not isinstance(nxt, ast.Assert) or _waits(nxt):
                    continue
                seen.add(stmt.lineno)
                text = lines[stmt.lineno - 1]
                if MARKER in text:
                    if text.split(MARKER, 1)[1].strip():
                        continue
                    found.append(f"{name}:{stmt.lineno}: {MARKER} needs a reason")
                    continue
                found.append(
                    f"{name}:{stmt.lineno}: fixed sleep before an assert in {fn.name};"
                    f" wait for the event or the loop's passes, or mark the line '{MARKER} <why>'"
                )
    return found


def main(argv: list[str]) -> int:
    roots = [Path(a) for a in argv] or [REPO / "tests"]
    files = sorted(p for r in roots for p in ([r] if r.is_file() else r.rglob("*.py")))
    findings: list[str] = []
    for p in files:
        try:
            rel = str(p.resolve().relative_to(REPO))
        except ValueError:
            rel = str(p)
        try:
            findings += check_source(p.read_text(), rel)
        except SyntaxError as e:
            print(f"sleep-race-check: {rel} does not parse: {e}", file=sys.stderr)
            return 2
    for f in findings:
        print(f)
    print(f"sleep-race-check: {len(files)} files, {len(findings)} finding(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
