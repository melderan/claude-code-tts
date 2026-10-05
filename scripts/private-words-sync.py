#!/usr/bin/env python3
"""private-words-sync.py - the private word list learns every non-public repository name.

A public repository must not carry the names of things that are not public: a commit body
once named an internal repository in passing, and the hand-kept word list did not know that
name. This script asks GitHub for every repository the maintainer can see in the organizations
listed in `.private-orgs` (gitignored, one login per line, `#` comments) and writes the names
whose visibility is not PUBLIC into a managed block of `.private-words`, as whole-word,
case-insensitive patterns. Everything outside the block is left as the maintainer wrote it.

The block carries the date it was written. private-check.py reads that date and complains when
the block is stale, so the list cannot quietly fall behind the organization.

Only distinctive names are learned: those with a hyphen, an underscore or a digit. Measured on
this repository, every one of several hundred such names across the maintainer's organizations matched nothing, while 18 of
172 plain single-word names (next, control, runtime, design, ...) matched ordinary code and prose
hundreds of times. A plain-word name that matters goes into the list by hand, above the block.

Names are never printed; only counts. Needs `gh` logged in (or the token the environment
injects), and access to the organizations: a name you cannot list cannot be learned.

    private-words-sync.py [--repo DIR] [--dry-run]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WORDS_FILE = ".private-words"
ORGS_FILE = ".private-orgs"
BEGIN = "# --- managed by scripts/private-words-sync.py: non-public repository names; synced "
END = "# --- end managed block ---"
CHUNK = 40  # names per alternation line, so a hit still reports one readable pattern


def read_orgs(path: Path) -> list[str]:
    orgs: list[str] = []
    for raw in path.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            orgs.append(line)
    return orgs


def list_non_public(org: str) -> list[str]:
    """Repository names in `org` whose visibility is not PUBLIC, via gh."""
    r = subprocess.run(
        ["gh", "repo", "list", org, "--limit", "1000", "--json", "name,visibility"],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"gh repo list {org} failed: {r.stderr.strip()[:200]}")
    return sorted(
        {
            row["name"]
            for row in json.loads(r.stdout or "[]")
            if row.get("visibility", "").upper() != "PUBLIC"
        }
    )


def distinctive(name: str) -> bool:
    """A name that cannot be an ordinary word: it has a hyphen, an underscore or a digit."""
    return any(ch in "-_" or ch.isdigit() for ch in name)


def build_block(names: list[str], today: dt.date) -> list[str]:
    lines = [f"{BEGIN}{today.isoformat()} ---"]
    for i in range(0, len(names), CHUNK):
        chunk = "|".join(re.escape(n) for n in names[i : i + CHUNK])
        lines.append(rf"\b(?:{chunk})\b")
    lines.append(END)
    return lines


def splice(existing: str, block: list[str]) -> str:
    """Replace the managed block in `existing`, or append one; the rest is untouched."""
    lines = existing.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith(BEGIN)), None)
    end = next((i for i, line in enumerate(lines) if line == END), None)
    if start is not None and end is not None and end > start:
        lines[start : end + 1] = block
    else:
        if lines and lines[-1].strip():
            lines.append("")
        lines += block
    return "\n".join(lines) + "\n"


def synced_on(words_text: str) -> dt.date | None:
    """The date in the managed block's header, or None when there is no block."""
    for line in words_text.splitlines():
        if line.startswith(BEGIN):
            try:
                return dt.date.fromisoformat(line[len(BEGIN) :].split(" ", 1)[0])
            except ValueError:
                return None
    return None


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--repo", type=Path, default=REPO)
    parser.add_argument("--dry-run", action="store_true", help="report counts, write nothing")
    args = parser.parse_args(argv)
    orgs_path = args.repo / ORGS_FILE
    words_path = args.repo / WORDS_FILE
    if not orgs_path.exists():
        print(
            f"private-words-sync: no {ORGS_FILE} in {args.repo}; nothing to learn (one org login per line)"
        )
        return 0
    orgs = read_orgs(orgs_path)
    names: set[str] = set()
    for org in orgs:
        found = list_non_public(org)
        kept = [n for n in found if distinctive(n)]
        print(
            f"private-words-sync: {org}: {len(found)} non-public repositories, {len(kept)} distinctive names"
            f" learned, {len(found) - len(kept)} plain words skipped (add any that matter by hand)"
        )
        names.update(kept)
    block = build_block(sorted(names), dt.date.today())
    if args.dry_run:
        print(
            f"private-words-sync: dry run; would write {len(names)} names in {len(block) - 2} pattern line(s)"
        )
        return 0
    existing = words_path.read_text() if words_path.exists() else ""
    words_path.write_text(splice(existing, block))
    print(
        f"private-words-sync: wrote {len(names)} names in {len(block) - 2} pattern line(s) to {WORDS_FILE}"
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except RuntimeError as e:
        print(f"private-words-sync: {e}", file=sys.stderr)
        sys.exit(1)
