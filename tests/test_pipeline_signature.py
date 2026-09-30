"""Signatures of what the speech pipeline produces, committed, so a refactor cannot change
the sound without changing a file in this directory.

The engine is a fake that turns text into tone bursts (one per word), so the pipeline under
test is everything after synthesis: sentence splitting, the loudness leveller, the per-part
files play_sentences hands the player. Fixtures live in tests/signatures/pipeline/. Regenerate
on purpose with UPDATE_SIGNATURES=1 and read the diff before committing it.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

import claude_code_tts.daemon as d
from claude_code_tts.bridge import split_sentences
from claude_code_tts.daemon import play_sentences, sentence_generator, write_playback_state
from claude_code_tts.signature import Signature, compare, sign
from tests.test_signature import voice, write

FIXTURES = Path(__file__).parent / "signatures" / "pipeline"
TEXT = "The daemon speaks each sentence as it is ready. Pauses between them are kept. Then it stops."


def fake_engine(gain: float = 0.3):
    """text -> tone bursts, one per word, pitch from the persona; `gain` is the engine's level."""

    def synth(text: str, persona: str, output_file: Path, *a, **k) -> bool:
        pitch = 150.0 + (sum(map(ord, persona)) % 7) * 20
        write(output_file, voice(words=len(text.split()), gain=gain, pitch=pitch, seed=len(text)))
        return True

    return synth


@pytest.fixture
def env(tmp_path, monkeypatch):
    state = tmp_path / ".claude-tts"
    state.mkdir()
    monkeypatch.setattr(d, "_shutdown_requested", False)
    player = tmp_path / "fake-player"
    player.write_text("#!/bin/bash\nsleep 0.01\n")
    player.chmod(player.stat().st_mode | stat.S_IEXEC)
    with (
        patch.object(d, "PLAYBACK_STATE_FILE", state / "playback.json"),
        patch.object(d, "HEARTBEAT_FILE", state / "daemon.heartbeat"),
        patch.object(d, "LOG_FILE", state / "daemon.log"),
        patch.object(d, "detect_player", return_value=[str(player)]),
        patch.object(d, "normalize_target", return_value=-16.0),
        patch.object(d, "persona_gain_db", return_value=0.0),
    ):
        write_playback_state(paused=False, paused_by=None, current_message=None, audio_pid=None)
        yield tmp_path


def run_pipeline(tmp: Path, text: str, engine, persona: str = "test-voice") -> list[Signature]:
    with patch.object(d, "_generate_speech_unleveled", engine):
        r = play_sentences(split_sentences(text), tmp / "m.wav", sentence_generator(persona))
    assert r.outcome == "done"
    sigs = []
    for sentence, part in zip(split_sentences(text), r.parts, strict=True):
        s = sign(part, text=sentence, voice=persona)
        assert s is not None
        sigs.append(s)
    return sigs


def check_against_fixture(name: str, sigs: list[Signature]) -> None:
    path = FIXTURES / f"{name}.json"
    if os.environ.get("UPDATE_SIGNATURES") or not path.exists():
        path.write_text(json.dumps([json.loads(s.to_json()) for s in sigs], indent=1) + "\n")
        pytest.skip(f"wrote {path.relative_to(Path.cwd())}; rerun to verify")
    expected = [Signature.from_json(json.dumps(e)) for e in json.loads(path.read_text())]
    assert len(expected) == len(sigs), "sentence count changed"
    for i, (e, a) in enumerate(zip(expected, sigs, strict=True)):
        assert compare(e, a) == [], f"sentence {i} drifted from {path.name}"


def test_three_sentences_match_the_committed_signatures(env):
    check_against_fixture("three_sentences", run_pipeline(env, TEXT, fake_engine()))


def test_a_quieter_engine_sounds_the_same_after_the_leveller(env):
    loud = run_pipeline(env, TEXT, fake_engine(gain=0.3))
    quiet = run_pipeline(env, TEXT, fake_engine(gain=0.15))
    for a, b in zip(loud, quiet, strict=True):
        assert compare(a, b) == []
        assert abs(a.speech_dbfs - (-16.0)) < 1.0


def test_without_the_leveller_the_quieter_engine_is_heard(env):
    """The control for the test above: it is the leveller that makes them equal."""
    with patch.object(d, "normalize_target", return_value=None):
        loud = run_pipeline(env, TEXT, fake_engine(gain=0.3))
        quiet = run_pipeline(env, TEXT, fake_engine(gain=0.15))
    diffs = compare(loud[0], quiet[0])
    assert any(x.startswith("speech level") for x in diffs), diffs


def test_a_changed_target_level_is_caught_by_the_fixture(env):
    """A planted regression: someone edits the loudness target."""
    path = FIXTURES / "three_sentences.json"
    if not path.exists():
        pytest.skip("fixture not written yet")
    expected = [Signature.from_json(json.dumps(e)) for e in json.loads(path.read_text())]
    with patch.object(d, "normalize_target", return_value=-22.0):
        actual = run_pipeline(env, TEXT, fake_engine())
    assert any(compare(e, a) for e, a in zip(expected, actual, strict=True))
