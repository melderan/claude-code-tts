"""scripts/voice-signatures.py against a fake speech history: capture, then verify ok and drift."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from tests.test_signature import voice, write

SCRIPT = Path(__file__).parent.parent / "scripts" / "voice-signatures.py"


@pytest.fixture
def vs(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("voice_signatures", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    hist = tmp_path / "speech_history"
    hist.mkdir()
    db = tmp_path / "handy_analysis.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "create table speech_history (id integer primary key, file_name text, created_at real, "
        "session_id text, project text, persona text, text text, speed real, tone text, "
        "duration_seconds real)"
    )
    for i, text in enumerate(["Hello there friend.", "The second line is longer than that."]):
        write(hist / f"tts-{i}.wav", voice(words=len(text.split()), seed=i))
        conn.execute(
            "insert into speech_history (file_name, created_at, persona, text, speed) values (?,?,?,?,?)",
            (f"tts-{i}.wav", 1000.0 + i, "test-voice", text, 1.0),
        )
    conn.commit()
    conn.close()
    monkeypatch.setattr(mod, "HISTORY_DIR", hist)
    monkeypatch.setattr(mod, "HISTORY_DB", db)
    monkeypatch.setattr(mod, "BASELINE_DIR", tmp_path / "signatures")
    monkeypatch.setattr(sys, "argv", ["voice-signatures"])
    return mod


def _args(**kw):
    import argparse

    return argparse.Namespace(**kw)


def test_capture_writes_one_baseline_per_utterance_and_is_idempotent(vs, capsys):
    assert vs.cmd_capture(_args(limit=50)) == 0
    files = sorted(p.name for p in (vs.BASELINE_DIR / "test-voice").glob("*.json"))
    assert len(files) == 2
    b = json.loads((vs.BASELINE_DIR / "test-voice" / files[0]).read_text())
    assert b["persona"] == "test-voice" and b["text"] and b["signature"]["seconds"] > 0
    assert vs.cmd_capture(_args(limit=50)) == 0
    assert "2 already known" in capsys.readouterr().out


def test_verify_passes_when_the_engine_still_sounds_the_same(vs, monkeypatch, capsys):
    vs.cmd_capture(_args(limit=50))

    def same(text, persona, out):
        write(out, voice(words=len(text.split()), seed=99))  # a fresh run, tiny noise
        return True

    monkeypatch.setattr(vs, "synthesize", same)
    assert vs.cmd_verify(_args(limit=50, persona=None, max_seconds=30.0)) == 0
    assert "2 ok, 0 drifted" in capsys.readouterr().out


def test_verify_fails_when_the_engine_got_quieter_and_faster(vs, monkeypatch, capsys):
    vs.cmd_capture(_args(limit=50))

    def worse(text, persona, out):
        write(out, voice(words=len(text.split()), gain=0.15, word_s=0.18))
        return True

    monkeypatch.setattr(vs, "synthesize", worse)
    assert vs.cmd_verify(_args(limit=50, persona=None, max_seconds=30.0)) == 1
    out = capsys.readouterr().out
    assert "DRIFT" in out and "speech level" in out and "2 drifted" in out


def test_spread_records_the_run_to_run_variation(vs, monkeypatch, capsys):
    vs.cmd_capture(_args(limit=50))
    counter = {"n": 0}

    def noisy(text, persona, out):
        counter["n"] += 1
        write(out, voice(words=len(text.split()), seed=counter["n"]))
        return True

    monkeypatch.setattr(vs, "synthesize", noisy)
    assert vs.cmd_spread(_args(runs=3, limit=50, persona=None, max_seconds=30.0)) == 0
    spreads = list(vs.BASELINE_DIR.glob("*/*.spread.json"))
    assert len(spreads) == 2
    assert json.loads(spreads[0].read_text())["runs"] == 3


def test_long_baselines_are_skipped_and_short_ones_come_first(vs):
    vs.cmd_capture(_args(limit=50))
    both = vs.load_baselines(None)
    assert [b["signature"]["seconds"] for b in both] == sorted(b["signature"]["seconds"] for b in both)
    assert vs.load_baselines(None, max_seconds=both[0]["signature"]["seconds"]) == [both[0]]
