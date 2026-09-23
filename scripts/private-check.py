#!/usr/bin/env python3
"""private-check.py - nothing private leaves this public repo (gate step, pre-commit and pre-push).

Reads a word list from `.private-words` in the repo root, one pattern per line (case-insensitive
regular expressions; blank lines and `#` comments ignored). That file is gitignored on purpose: the
words that must never appear are themselves private, so each maintainer keeps their own list. Then:

  1. every tracked file, plus staged changes, is searched for the patterns;
  2. the messages of commits not yet on any remote are searched too, since a commit body becomes
     public on push and a release tag copies it into the release notes;
  3. annotated tags pointing at those commits are searched the same way.

Any hit fails the gate with file:line (or the commit) and the matching pattern. With no word list
the check prints that it is skipped, so a fresh clone is never silently unprotected.

    private-check.py [--repo DIR] [--words FILE]
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WORDS_FILE = ".private-words"


def load_patterns(path: Path) -> list[re.Pattern[str]]:
    patterns: list[re.Pattern[str]] = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        patterns.append(re.compile(line, re.IGNORECASE))
    return patterns


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


def scan_text(text: str, patterns: list[re.Pattern[str]], where: str) -> list[str]:
    """Hits as "<where>:<line>: <pattern>" for every line of text that matches."""
    hits: list[str] = []
    for n, line in enumerate(text.splitlines(), 1):
        for p in patterns:
            if p.search(line):
                hits.append(f"{where}:{n}: matches /{p.pattern}/")
                break
    return hits


def scan_files(repo: Path, patterns: list[re.Pattern[str]]) -> list[str]:
    """Tracked and staged files, text only; the word list itself is never scanned."""
    names = set(_git(repo, "ls-files", "-z").split("\0")) | set(
        _git(repo, "diff", "--cached", "--name-only", "-z").split("\0")
    )
    hits: list[str] = []
    for name in sorted(n for n in names if n and n != WORDS_FILE):
        path = repo / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary or unreadable: nothing to grep
        hits += scan_text(text, patterns, name)
    return hits


def scan_unpushed(repo: Path, patterns: list[re.Pattern[str]]) -> list[str]:
    """Messages of commits on no remote branch, and of annotated tags on those commits."""
    hits: list[str] = []
    log = _git(repo, "log", "--branches", "--not", "--remotes", "--format=%H%x00%B%x01")
    shas: list[str] = []
    for entry in log.split("\x01"):
        sha, _, body = entry.strip("\n").partition("\x00")
        if not sha.strip():
            continue
        shas.append(sha.strip())
        hits += scan_text(body, patterns, f"commit {sha[:7]}")
    # `git tag --format` takes for-each-ref atoms only (no %x00), so split on spaces.
    for tag in _git(repo, "tag", "-l", "--format=%(refname:short) %(*objectname) %(objectname)").splitlines():
        name, _, target = tag.partition(" ")
        if any(s in target for s in shas):
            hits += scan_text(_git(repo, "tag", "-l", "--format=%(contents)", name), patterns, f"tag {name}")
    return hits


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", type=Path, default=REPO)
    parser.add_argument("--words", type=Path, default=None)
    args = parser.parse_args(argv)
    words = args.words or (args.repo / WORDS_FILE)
    if not words.exists():
        print(f"private-check: no {words.name} in {args.repo}; SKIPPED (create it, one pattern per line)")
        return 0
    patterns = load_patterns(words)
    hits = scan_files(args.repo, patterns) + scan_unpushed(args.repo, patterns)
    for hit in hits:
        print(f"private-check: {hit}", file=sys.stderr)
    if hits:
        print(f"private-check: {len(hits)} hit(s); nothing private leaves this repo", file=sys.stderr)
        return 1
    print(f"private-check: clean ({len(patterns)} patterns)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
