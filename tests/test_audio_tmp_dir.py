"""The daemon's WAV directory is one constant, and its startup sweep stays inside it.

Through 9.36.3 every WAV path in daemon.py said /tmp and the sweep at daemon start deleted
tts_queue_*.wav there, so two pytest runs on one machine deleted each other's files mid-test.
A reviewer reproduced it on 2026-09-30 by deleting /tmp/tts_queue_*_s0.wav in a loop while
the sentence-stream tests ran: three failures. The conftest fixture now points the constant
at a per-test directory; these tests pin the shape that makes that fixture enough.
"""

from __future__ import annotations

import re
from pathlib import Path

import claude_code_tts.daemon as d

SOURCE = Path(d.__file__).read_text()


def test_only_the_constant_names_tmp() -> None:
    assert SOURCE.count('"/tmp') == 1, "every WAV path must go through AUDIO_TMP_DIR"
    assert 'AUDIO_TMP_DIR = Path("/tmp")' in SOURCE


def test_sweep_removes_only_queue_wavs_in_its_own_directory(tmp_path: Path) -> None:
    ours = d.AUDIO_TMP_DIR  # the conftest fixture's directory
    (ours / "tts_queue_a.wav").write_bytes(b"x")
    (ours / "tts_queue_b_tok_s0.wav").write_bytes(b"x")
    (ours / "tts_announce.wav").write_bytes(b"x")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "tts_queue_c.wav").write_bytes(b"x")

    assert d.clear_leftover_wavs() == 2
    assert sorted(p.name for p in ours.iterdir()) == ["tts_announce.wav"]
    assert (elsewhere / "tts_queue_c.wav").exists()


def test_prepared_message_wav_lives_in_the_constant_directory() -> None:
    p = d.prepare_message({"_file": Path("q.json"), "id": "abc123", "session_id": "s", "text": "hi"}, {})
    assert p.audio_file.parent == d.AUDIO_TMP_DIR
    assert re.fullmatch(r"tts_queue_s_abc123\.wav", p.audio_file.name)
