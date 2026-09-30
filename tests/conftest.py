"""Point HOME at a throwaway directory before any package module computes paths from it."""

import os
import tempfile

_HOME = tempfile.mkdtemp(prefix="claude-tts-test-home-")
os.environ["HOME"] = _HOME

import pytest  # noqa: E402  (HOME must be set before anything else is imported)


@pytest.fixture(autouse=True)
def _no_inherited_tts_session(monkeypatch):
    """The suite decides per test whether CLAUDE_TTS_SESSION is set; the shell running it does not.

    The claude-house kit exports it in every room, and 9.25.1's hook crash only showed with it set,
    so a suite that inherited the shell's environment tested one path on the Mac and the other in a
    room. Tests that want it call monkeypatch.setenv themselves.
    """
    monkeypatch.delenv("CLAUDE_TTS_SESSION", raising=False)


@pytest.fixture(autouse=True)
def _own_audio_dir(monkeypatch, tmp_path_factory):
    """Every test gets its own directory for the daemon's WAVs.

    The daemon's startup sweep deletes tts_queue_*.wav in that directory. With the shared /tmp,
    a second pytest on the same machine (a brother's review copy) deleted this run's files
    mid-test, which read as a flaky sentence-stream test. It was a bug (2026-09-30). A sibling
    of tmp_path, not inside it: tests assert on what tmp_path contains.
    """
    monkeypatch.setattr("claude_code_tts.daemon.AUDIO_TMP_DIR", tmp_path_factory.mktemp("audio"))
