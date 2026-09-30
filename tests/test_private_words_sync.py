"""private-words-sync: distinctive names only, a dated managed block, the rest of the list untouched."""

from __future__ import annotations

import datetime as dt
import importlib.util
import re
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "private-words-sync.py"
spec = importlib.util.spec_from_file_location("private_words_sync", SCRIPT)
assert spec and spec.loader
pws = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pws)

TODAY = dt.date(2026, 9, 30)


def test_distinctive_needs_a_hyphen_underscore_or_digit() -> None:
    assert pws.distinctive("acme8kit")
    assert pws.distinctive("widget-ee")
    assert pws.distinctive("team_secrets")
    for plain in ("next", "control", "runtime", "design", "symphonia"):
        assert not pws.distinctive(plain), plain


def test_block_is_dated_whole_word_and_escaped() -> None:
    block = pws.build_block(["a-b", "c.d1"], TODAY)
    assert block[0].startswith(pws.BEGIN) and "2026-09-30" in block[0]
    assert block[-1] == pws.END
    pat = re.compile(block[1], re.I)
    assert pat.search("uses A-B here") and pat.search("c.d1")
    assert not pat.search("cxd1"), "the dot is escaped"
    assert not pat.search("a-bc"), "whole word"


def test_block_is_chunked() -> None:
    block = pws.build_block([f"n-{i}" for i in range(pws.CHUNK * 2 + 1)], TODAY)
    assert len(block) == 2 + 3


def test_splice_appends_then_replaces_and_keeps_hand_lines() -> None:
    hand = "# mine\n\\bsecret-host\\b\n"
    first = pws.splice(hand, pws.build_block(["x-1"], TODAY))
    assert first.startswith(hand)
    assert first.count(pws.END) == 1
    later = pws.splice(first, pws.build_block(["y-2", "z-3"], dt.date(2026, 10, 15)))
    assert later.startswith(hand)
    assert later.count(pws.BEGIN) == 1 and later.count(pws.END) == 1
    assert "2026-10-15" in later and "2026-09-30" not in later
    managed = re.compile(later.split(pws.BEGIN, 1)[1].splitlines()[1], re.I)
    assert managed.search("y-2") and managed.search("z-3") and not managed.search("x-1")


def test_synced_on_reads_the_header() -> None:
    text = pws.splice("", pws.build_block(["x-1"], TODAY))
    assert pws.synced_on(text) == TODAY
    assert pws.synced_on("# nothing managed\n") is None


def test_read_orgs_ignores_comments() -> None:
    p = Path(__file__).parent / "_orgs.tmp"
    try:
        p.write_text("# orgs\nexample-org  # ours\n\nother\n")
        assert pws.read_orgs(p) == ["example-org", "other"]
    finally:
        p.unlink(missing_ok=True)
