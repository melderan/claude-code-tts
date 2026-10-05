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

A hit that is known and accepted is an approved lapse, written to `.private-allow` (gitignored
too, it quotes the patterns): three tab-separated fields per line, `where`, `pattern`, `reason`.
`where` is a glob over the hit's place (`tests/docker/*`, `commit 71c8d83`, `tag v9.*`), `pattern`
is the pattern text as it stands in the word list or `*` for any, and `reason` says who approved
it, when and why. An approved hit is printed by name with its reason and counted apart; it does not
fail the gate. An allow line that matched nothing is reported, so a stale approval is seen. A line
without a reason fails the gate: nothing is waved through without a name on it.

The list can carry a block written by scripts/private-words-sync.py (every non-public repository
name the maintainer can see, dated). When that block is older than STALE_WARN_DAYS the check
warns; older than STALE_FAIL_DAYS it fails, because a list that has not learned this month's
new names is not protecting anything. Run `just private-sync` to refresh it.

    private-check.py [--repo DIR] [--words FILE] [--allow FILE]
"""

from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WORDS_FILE = ".private-words"
ALLOW_FILE = ".private-allow"
SYNC_HEADER = "# --- managed by scripts/private-words-sync.py: non-public repository names; synced "
STALE_WARN_DAYS = 14
STALE_FAIL_DAYS = 45


class Allow:
    """One approved lapse: a place, the pattern it may match there, and who said so, when and why.

    A plain class, not a dataclass: the tests load this script by file path, and a dataclass
    with postponed annotations looks its module up in sys.modules, where it is not.
    """

    __slots__ = ("where", "pattern", "reason", "line_no", "used")

    def __init__(self, where: str, pattern: str, reason: str, line_no: int) -> None:
        self.where = where
        self.pattern = pattern
        self.reason = reason
        self.line_no = line_no
        self.used = 0

    def covers(self, where: str, pattern: str) -> bool:
        return fnmatch.fnmatchcase(where, self.where) and self.pattern in ("*", pattern)


def synced_block_age(words_text: str, today: dt.date | None = None) -> int | None:
    """Days since the managed block was written; None when the list has no block."""
    for line in words_text.splitlines():
        if line.startswith(SYNC_HEADER):
            try:
                written = dt.date.fromisoformat(line[len(SYNC_HEADER) :].split(" ", 1)[0])
            except ValueError:
                return None
            return ((today or dt.date.today()) - written).days
    return None


def load_patterns(path: Path) -> list[re.Pattern[str]]:
    patterns: list[re.Pattern[str]] = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        patterns.append(re.compile(line, re.IGNORECASE))
    return patterns


def load_allows(path: Path) -> tuple[list[Allow], list[str]]:
    """The approved lapses and the lines that are not one (no reason, too few fields)."""
    allows: list[Allow] = []
    errors: list[str] = []
    for n, raw in enumerate(path.read_text().splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        fields = [f.strip() for f in raw.split("\t")]
        if len(fields) < 3 or not all(fields[:3]):
            errors.append(f"allow line {n} needs where, pattern and a reason, tab-separated: {raw.strip()!r}")
            continue
        allows.append(Allow(fields[0], fields[1], "\t".join(fields[2:]), n))
    return allows, errors


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


def scan_text(text: str, patterns: list[re.Pattern[str]], where: str) -> list[str]:
    """Hits as "<where>:<line>: matches /<pattern>/" for every line of text that matches."""
    hits: list[str] = []
    for n, line in enumerate(text.splitlines(), 1):
        for p in patterns:
            if p.search(line):
                hits.append(f"{where}:{n}: matches /{p.pattern}/")
                break
    return hits


def split_hit(hit: str) -> tuple[str, str, str]:
    """(where, line, pattern) of a scan_text hit."""
    head, _, pattern = hit.rpartition(": matches /")
    where, _, line = head.rpartition(":")
    return where, line, pattern[:-1]


def split_approved(hits: list[str], allows: list[Allow]) -> tuple[list[str], list[str]]:
    """The hits that still block, and the approved ones as "<where>:<line> /<pattern>/ (<reason>)"."""
    blocking: list[str] = []
    approved: list[str] = []
    for hit in hits:
        where, line, pattern = split_hit(hit)
        for allow in allows:
            if allow.covers(where, pattern):
                allow.used += 1
                approved.append(f"{where}:{line} /{pattern}/ ({allow.reason})")
                break
        else:
            blocking.append(hit)
    return blocking, approved


def scan_files(repo: Path, patterns: list[re.Pattern[str]]) -> list[str]:
    """Tracked and staged files, text only; the word and allow lists themselves are never scanned."""
    names = set(_git(repo, "ls-files", "-z").split("\0")) | set(
        _git(repo, "diff", "--cached", "--name-only", "-z").split("\0")
    )
    hits: list[str] = []
    for name in sorted(n for n in names if n and n not in (WORDS_FILE, ALLOW_FILE)):
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
    for tag in _git(
        repo, "tag", "-l", "--format=%(refname:short) %(*objectname) %(objectname)"
    ).splitlines():
        name, _, target = tag.partition(" ")
        if any(s in target for s in shas):
            hits += scan_text(
                _git(repo, "tag", "-l", "--format=%(contents)", name), patterns, f"tag {name}"
            )
    return hits


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--repo", type=Path, default=REPO)
    parser.add_argument("--words", type=Path, default=None)
    parser.add_argument("--allow", type=Path, default=None, help=f"approved lapses (default {ALLOW_FILE})")
    args = parser.parse_args(argv)
    words = args.words or (args.repo / WORDS_FILE)
    if not words.exists():
        print(
            f"private-check: no {words.name} in {args.repo}; SKIPPED (create it, one pattern per line)"
        )
        return 0
    patterns = load_patterns(words)
    allow_path = args.allow or (args.repo / ALLOW_FILE)
    allows: list[Allow] = []
    if allow_path.exists():
        allows, errors = load_allows(allow_path)
        for error in errors:
            print(f"private-check: {error}", file=sys.stderr)
        if errors:
            print(f"private-check: {len(errors)} bad allow line(s); nothing is waved through unnamed", file=sys.stderr)
            return 1
    hits = scan_files(args.repo, patterns) + scan_unpushed(args.repo, patterns)
    hits, approved = split_approved(hits, allows)
    for line in approved:
        print(f"private-check: approved {line}")
    for allow in allows:
        if not allow.used:
            print(
                f"private-check: allow line unused: {allow.where} /{allow.pattern}/ ({allow.reason}); "
                f"drop it from {allow_path.name} line {allow.line_no} if the lapse is gone",
                file=sys.stderr,
            )
    for hit in hits:
        print(f"private-check: {hit}", file=sys.stderr)
    if hits:
        print(
            f"private-check: {len(hits)} hit(s); nothing private leaves this repo", file=sys.stderr
        )
        return 1
    age = synced_block_age(words.read_text())
    if age is None:
        print(
            "private-check: word list has no synced repository names; run `just private-sync`",
            file=sys.stderr,
        )
    elif age > STALE_FAIL_DAYS:
        print(
            f"private-check: synced repository names are {age} days old (limit {STALE_FAIL_DAYS}); run `just private-sync`",
            file=sys.stderr,
        )
        return 1
    elif age > STALE_WARN_DAYS:
        print(
            f"private-check: synced repository names are {age} days old; run `just private-sync` soon",
            file=sys.stderr,
        )
    tail = ""
    if approved:
        tail = f", {len(approved)} approved lapse{'s' if len(approved) != 1 else ''}"
    print(f"private-check: clean ({len(patterns)} patterns{tail})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
