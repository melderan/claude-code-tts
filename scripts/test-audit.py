#!/usr/bin/env python3
"""test-audit.py - which tests would notice a one-line change to the source? (`just test-audit`)

A test earns its place when some plausible one-line change to src/ or scripts/ makes it fail.
This script measures that per test instead of per mutant, so the output answers "how many of
these tests are real" rather than "how much of the code is covered":

  1. run the suite once with coverage contexts, so every test knows the lines it executed;
  2. generate single-line mutants for those lines: flipped comparisons and booleans, swapped
     operators, shifted constants, altered strings, early `return None`, dropped `not`,
     `break`/`continue` swapped, and each simple statement replaced with `pass`;
  3. run each mutant, in a scratch copy of the checkout, against only the tests that covered
     its line and have not yet killed anything. Once every test covering a line has proven
     itself, the remaining mutants on that line are skipped: this is why a few hundred runs
     settle several thousand mutants.

The report lists three groups. "validated" tests killed at least one mutant. "unvalidated"
tests executed source lines but survived every mutant on them: read each one; it is either
asserting nothing about the code or pinning a contract the code enforces twice. "no coverage"
tests never executed a src/ or scripts/ line in their own context: constants asserted at import
time, regexes, repo files, subprocesses, or logic re-implemented inside the test. The last kind
is the one to delete; the audit cannot tell them apart, a reader can.

Output goes to .logs/test-audit/<utc time>/ (gitignored): covmap.json, mutants.jsonl,
results.jsonl, report.json, and a report.txt with the two lists to read. Needs pytest and
coverage in the interpreter that runs it; `just test-audit` supplies them. Standard library
otherwise. An audit of the whole suite takes a few minutes on a laptop with `--workers 6`.

    just test-audit                     # whole suite
    just test-audit --workers 8         # more parallel copies
    just test-audit -k filter           # only tests matching a pytest -k expression
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SOURCES = ("src", "scripts")
PY = sys.executable

CMP_SWAP = {
    "==": "!=", "!=": "==", "<": ">=", ">=": "<", ">": "<=", "<=": ">",
    "is": "is not", "is not": "is", "in": "not in", "not in": "in",
}
BOOL_SWAP = {"and": "or", "or": "and"}
BIN_SWAP = {"+": "-", "-": "+", "*": "/", "/": "*", "//": "/", "%": "//"}
AUG_SWAP = {"+=": "-=", "-=": "+="}
SIMPLE_STATEMENTS = (
    ast.Return, ast.Raise, ast.Assign, ast.AugAssign, ast.Expr, ast.Continue, ast.Break,
)
SUMMARY_LINE = re.compile(r"^(FAILED|ERROR) (.+?)(?: - .*)?$")

Mutant = dict


# --------------------------------------------------------------------------- coverage
def record_coverage(out_dir: Path, k_expr: str | None) -> Path:
    """Run the suite once with per-test coverage contexts; return the coverage data file."""
    data = out_dir / ".coverage"
    cmd = [
        PY, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "addopts=",
        "--ignore=tests/docker", "--cov-context=test", "--cov-report=",
        *(f"--cov={REPO / s}" for s in SOURCES),
    ]
    if k_expr:
        cmd += ["-k", k_expr]
    env = dict(os.environ, COVERAGE_FILE=str(data))
    print("audit: recording per-test coverage (one full run of the suite)", flush=True)
    r = subprocess.run(cmd, cwd=REPO, env=env, capture_output=True, text=True)
    tail = r.stdout.strip().splitlines()[-1:] or ["(no output)"]
    print(f"audit: {tail[0]}", flush=True)
    if r.returncode != 0:
        sys.exit("audit: the suite must pass before it can be audited")
    return data


def collect_tests(k_expr: str | None) -> list[str]:
    cmd = [PY, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
           "-o", "addopts=", "--ignore=tests/docker"]
    if k_expr:
        cmd += ["-k", k_expr]
    r = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True)
    return [ln.strip() for ln in r.stdout.splitlines() if "::" in ln]


def build_covmap(data: Path) -> dict[str, dict[str, list[int]]]:
    """{test nodeid: {repo-relative file: [lines]}} across setup, call and teardown."""
    from coverage.numbits import numbits_to_nums

    db = sqlite3.connect(str(data))
    files = {fid: os.path.relpath(p, REPO) for fid, p in db.execute("select id, path from file")}
    contexts = dict(db.execute("select id, context from context"))
    covmap: dict[str, dict[str, set[int]]] = defaultdict(lambda: defaultdict(set))
    for fid, cid, bits in db.execute("select file_id, context_id, numbits from line_bits"):
        ctx = contexts[cid]
        if "|" not in ctx:
            continue  # the global context: imports and collection
        covmap[ctx.rsplit("|", 1)[0]][files[fid]].update(numbits_to_nums(bits))
    return {t: {f: sorted(ls) for f, ls in fs.items()} for t, fs in covmap.items()}


# --------------------------------------------------------------------------- mutants
def _between(lines: list[bytes], left: ast.AST, right: ast.AST) -> tuple[int, int, bytes] | None:
    """The bytes strictly between two sibling nodes on one line: (lineno, col, text)."""
    if left.end_lineno != right.lineno:
        return None
    col = left.end_col_offset
    return left.end_lineno, col, lines[left.end_lineno - 1][col:right.col_offset]


def file_mutants(rel: str, covered: set[int]) -> list[Mutant]:
    src = (REPO / rel).read_bytes()
    lines = src.split(b"\n")
    tree = ast.parse(src)
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr) \
                and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
            docstrings.add(id(body[0].value))

    edits: list[tuple[int, int, int, bytes, str]] = []  # lineno, col, endcol, new, desc

    def swap(left: ast.AST, right: ast.AST, table: dict[str, str], kind: str) -> None:
        found = _between(lines, left, right)
        if found is None:
            return
        ln, col, seg = found
        tok = seg.decode().strip()
        if tok in table:
            start = col + seg.index(tok.encode())
            edits.append((ln, start, start + len(tok), table[tok].encode(), f"{kind} {tok} -> {table[tok]}"))

    for node in ast.walk(tree):
        ln = getattr(node, "lineno", None)
        if ln is None or ln not in covered:
            continue
        one_line = node.end_lineno == ln
        if isinstance(node, ast.Compare):
            prev = node.left
            for comp in node.comparators:
                swap(prev, comp, CMP_SWAP, "cmp")
                prev = comp
        elif isinstance(node, ast.BoolOp):
            for x, y in zip(node.values, node.values[1:], strict=False):
                swap(x, y, BOOL_SWAP, "bool")
        elif isinstance(node, ast.BinOp):
            swap(node.left, node.right, BIN_SWAP, "bin")
        elif isinstance(node, ast.AugAssign):
            swap(node.target, node.value, AUG_SWAP, "aug")
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not) and one_line:
            inner = lines[ln - 1][node.operand.col_offset:node.operand.end_col_offset]
            edits.append((ln, node.col_offset, node.end_col_offset, inner, "drop not"))
        elif isinstance(node, ast.Constant) and id(node) not in docstrings and one_line:
            v, c, e = node.value, node.col_offset, node.end_col_offset
            if v is True or v is False:
                edits.append((ln, c, e, repr(not v).encode(), f"{v} -> {not v}"))
            elif isinstance(v, int):
                edits.append((ln, c, e, repr(v + 1).encode(), f"{v} -> {v + 1}"))
            elif isinstance(v, float):
                edits.append((ln, c, e, repr(v * 2 + 1).encode(), f"{v} -> {v * 2 + 1}"))
            elif isinstance(v, str) and v:
                seg = lines[ln - 1][c:e]
                if seg[:1] in (b"'", b'"'):  # a plain literal, not an f-string part or prefixed
                    q = seg[:1]
                    edits.append((ln, c, e, q + b"XX" + seg[1:-1] + b"XX" + q, "str XX"))
        elif isinstance(node, ast.Break):
            edits.append((ln, node.col_offset, node.end_col_offset, b"continue", "break -> continue"))
        elif isinstance(node, ast.Continue):
            edits.append((ln, node.col_offset, node.end_col_offset, b"break", "continue -> break"))
        if isinstance(node, ast.Return) and node.value is not None and one_line \
                and not (isinstance(node.value, ast.Constant) and node.value.value is None):
            edits.append((ln, node.value.col_offset, node.value.end_col_offset, b"None", "return None"))
        if isinstance(node, SIMPLE_STATEMENTS) and one_line and id(getattr(node, "value", None)) not in docstrings:
            old = lines[ln - 1]
            indent = old[: len(old) - len(old.lstrip())]
            edits.append((ln, len(indent), len(old), b"pass", "statement -> pass"))

    out: list[Mutant] = []
    seen: set[tuple[int, bytes]] = set()
    for ln, c, e, new, desc in edits:
        old = lines[ln - 1]
        newline = old[:c] + new + old[e:]
        if (ln, newline) in seen:
            continue
        seen.add((ln, newline))
        candidate = lines[:]
        candidate[ln - 1] = newline
        try:
            compile(b"\n".join(candidate), rel, "exec")
        except SyntaxError:
            continue
        out.append({"file": rel, "line": ln, "desc": desc,
                    "old": old.decode(errors="replace"), "new": newline.decode(errors="replace")})
    return out


def all_mutants(covmap: dict[str, dict[str, list[int]]]) -> list[Mutant]:
    covered: dict[str, set[int]] = defaultdict(set)
    for files in covmap.values():
        for f, ls in files.items():
            covered[f].update(ls)
    mutants: list[Mutant] = []
    for rel in sorted(covered):
        if not rel.endswith(".py") or not rel.startswith(SOURCES):
            continue
        mutants.extend(file_mutants(rel, covered[rel]))
    # Statement deletions last: they are the blunt instrument, kept for what the fine ones miss.
    mutants.sort(key=lambda m: m["desc"] == "statement -> pass")
    for i, m in enumerate(mutants):
        m["id"] = i
    return mutants


# --------------------------------------------------------------------------- running
class Runner:
    """Runs mutants against their covering tests in N scratch copies of the checkout."""

    def __init__(self, covmap: dict[str, dict[str, list[int]]], out_dir: Path, workers: int) -> None:
        self.line_tests: dict[tuple[str, int], set[str]] = defaultdict(set)
        for t, files in covmap.items():
            for f, ls in files.items():
                for ln in ls:
                    self.line_tests[(f, ln)].add(t)
        self.validated: dict[str, int] = {}
        self.lock = threading.Lock()
        self.results = (out_dir / "results.jsonl").open("w")
        self.copies = [self._copy(out_dir / f"copy-{i}") for i in range(workers)]
        self.free = list(range(workers))
        self.done = 0

    @staticmethod
    def _copy(dst: Path) -> Path:
        # .git stays: some tests shell out to git in the checkout they run from
        ignore = shutil.ignore_patterns(".venv", ".logs", ".coverage*", ".mypy_cache",
                                        ".ruff_cache", ".pytest_cache", "dist", "__pycache__")
        shutil.copytree(REPO, dst, ignore=ignore, symlinks=True)
        return dst

    def _pytest(self, copy: Path, tests: list[str], timeout: int) -> tuple[str, set[str]]:
        cmd = [PY, "-m", "pytest", "-q", "-rfE", "--tb=no", "--no-header", "-p", "no:cacheprovider",
               "-o", "addopts=", *tests]
        try:
            r = subprocess.run(cmd, cwd=copy, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return "timeout", set()
        failed = set()
        for ln in r.stdout.splitlines():
            m = SUMMARY_LINE.match(ln.strip())
            if m:
                failed.add(m.group(2).strip())
        return ("ok" if r.returncode in (0, 1) else f"exit {r.returncode}"), failed

    def run(self, m: Mutant, total: int, timeout: int) -> None:
        with self.lock:
            tests = sorted(self.line_tests[(m["file"], m["line"])] - set(self.validated))
        rec = dict(m, tests_run=len(tests), killed_by=[])
        if not tests:
            rec["status"] = "skipped"
        else:
            with self.lock:
                slot = self.free.pop()
            copy = self.copies[slot]
            target = copy / m["file"]
            original = target.read_bytes()
            lines = original.split(b"\n")
            assert lines[m["line"] - 1].decode(errors="replace") == m["old"]
            lines[m["line"] - 1] = m["new"].encode()
            target.write_bytes(b"\n".join(lines))
            t0 = time.time()
            try:
                status, failed = self._pytest(copy, tests, timeout)
            finally:
                target.write_bytes(original)
                with self.lock:
                    self.free.append(slot)
            killed = sorted(failed & set(tests))
            rec.update(killed_by=killed, secs=round(time.time() - t0, 1),
                       status=status if status != "ok" else ("killed" if killed else "survived"))
            with self.lock:
                for t in killed:
                    self.validated.setdefault(t, m["id"])
        with self.lock:
            self.done += 1
            self.results.write(json.dumps(rec) + "\n")
            self.results.flush()
            if rec["status"] != "skipped":
                print(f"audit: {self.done}/{total} {m['file']}:{m['line']} {m['desc']!r} -> "
                      f"{rec['status']} ({rec['tests_run']} run, {len(rec['killed_by'])} killed)",
                      flush=True)


# --------------------------------------------------------------------------- report
def write_report(out_dir: Path, all_tests: list[str], covmap: dict, runner: Runner,
                 mutants: list[Mutant]) -> str:
    statuses: dict[str, int] = defaultdict(int)
    for ln in (out_dir / "results.jsonl").read_text().splitlines():
        statuses[json.loads(ln)["status"]] += 1
    validated = [t for t in all_tests if t in runner.validated]
    unvalidated = [t for t in all_tests if t in covmap and t not in runner.validated]
    no_coverage = [t for t in all_tests if t not in covmap]
    report = {
        "tests": len(all_tests), "mutants": len(mutants), "mutant_status": dict(statuses),
        "validated": {t: runner.validated[t] for t in validated},
        "unvalidated": unvalidated, "no_coverage": no_coverage,
    }
    (out_dir / "report.json").write_text(json.dumps(report, indent=1))
    text = [
        f"tests {len(all_tests)}: validated {len(validated)}, unvalidated {len(unvalidated)}, "
        f"no coverage {len(no_coverage)}",
        f"mutants {len(mutants)}: " + ", ".join(f"{k} {v}" for k, v in sorted(statuses.items())),
        "",
        "# unvalidated: executed source lines, survived every mutant on them; read each",
        *(f"  {t}" for t in unvalidated),
        "",
        "# no coverage: never executed a src/ or scripts/ line in their own context; read each",
        *(f"  {t}" for t in no_coverage),
    ]
    (out_dir / "report.txt").write_text("\n".join(text) + "\n")
    return "\n".join(text[:2])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("-k", dest="k_expr", help="pytest -k expression: audit only matching tests")
    ap.add_argument("--workers", type=int, default=4, help="parallel checkout copies (default 4)")
    ap.add_argument("--timeout", type=int, default=240, help="seconds per mutant run (default 240)")
    args = ap.parse_args()

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out_dir = REPO / ".logs" / "test-audit" / stamp
    out_dir.mkdir(parents=True)
    t0 = time.time()

    data = record_coverage(out_dir, args.k_expr)
    all_tests = collect_tests(args.k_expr)
    covmap = build_covmap(data)
    (out_dir / "covmap.json").write_text(json.dumps(covmap))
    mutants = all_mutants(covmap)
    with (out_dir / "mutants.jsonl").open("w") as fh:
        for m in mutants:
            fh.write(json.dumps(m) + "\n")
    print(f"audit: {len(all_tests)} tests, {len(covmap)} with python coverage, "
          f"{len(mutants)} mutants; running with {args.workers} workers", flush=True)

    runner = Runner(covmap, out_dir, args.workers)
    try:
        with ThreadPoolExecutor(args.workers) as pool:
            for m in mutants:
                pool.submit(runner.run, m, len(mutants), args.timeout)
    finally:
        runner.results.close()
        for c in runner.copies:
            shutil.rmtree(c, ignore_errors=True)
    print(write_report(out_dir, all_tests, covmap, runner, mutants))
    print(f"audit: {round(time.time() - t0)}s; lists to read in {out_dir.relative_to(REPO)}/report.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
