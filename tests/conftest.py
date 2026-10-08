"""Point HOME at a throwaway directory before any package module computes paths from it."""

import os
import tempfile

_HOME = tempfile.mkdtemp(prefix="claude-tts-test-home-")
os.environ["HOME"] = _HOME

# A git hook exports GIT_DIR and GIT_INDEX_FILE to the gate, and a test's `git init` in its own
# tmp_path then acts on the committing repository (2026-10-02: core.bare=true in the main
# checkout). The gate drops them before any step; this is the same list for a pytest run by hand
# under a hook, kept in scripts/gate.py so the two cannot drift.
import importlib.util  # noqa: E402

_gate_spec = importlib.util.spec_from_file_location(
    "gate", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "gate.py")
)
assert _gate_spec is not None and _gate_spec.loader is not None
_gate = importlib.util.module_from_spec(_gate_spec)
_gate_spec.loader.exec_module(_gate)
for _name in _gate.GIT_HOOK_ENV:
    os.environ.pop(_name, None)

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
def _own_spoken_store(monkeypatch, tmp_path_factory):
    """Every test gets its own spoken stores: the hook's ($TMPDIR/claude-tts-spoken) and the daemon's.

    The store outlives a process by design (a 30 s claim per utterance), so a shared TMPDIR
    would let one test's claim silence the same utterance in the next. tempfile has cached
    its directory before this runs, so tmp_path is unaffected.
    """
    monkeypatch.setenv("TMPDIR", str(tmp_path_factory.mktemp("tmpdir")))
    monkeypatch.setattr("claude_code_tts.state.SPOKEN_DIR", tmp_path_factory.mktemp("daemon-spoken"))


@pytest.fixture(autouse=True)
def _own_audio_dir(monkeypatch, tmp_path_factory):
    """Every test gets its own directory for the daemon's WAVs.

    The daemon's startup sweep deletes tts_queue_*.wav in that directory. With the shared /tmp,
    a second pytest on the same machine (a brother's review copy) deleted this run's files
    mid-test, which read as a flaky sentence-stream test. It was a bug (2026-09-30). A sibling
    of tmp_path, not inside it: tests assert on what tmp_path contains.
    """
    monkeypatch.setattr("claude_code_tts.daemon.AUDIO_TMP_DIR", tmp_path_factory.mktemp("audio"))


@pytest.fixture(autouse=True)
def _no_remembered_engine():
    """audio.py remembers which engine spoke last and why a spare stood in; a test that patches
    generate_speech never sets it, so one test's fallback must not become the next test's WARN."""
    import claude_code_tts.audio as _audio

    _audio._spoke_with("", "")
    _audio._set_last_error("")
    yield
    _audio._spoke_with("", "")
    _audio._set_last_error("")


@pytest.fixture(autouse=True)
def _no_leaked_threads():
    """A test that starts a thread stops it before the next test runs.

    2026-10-01: a daemon-loop test failed its first assertion, its finally stopped the patches
    but not the loop, and the loop kept polling under the next tests' patches: one message
    synthesized twice in the following test, and the prefetch test four files later lost a
    message to it (three CI failures from one leaked thread). A leak now fails the test that
    leaked, by name, and a leaked daemon loop is asked to stop so the tests after it are
    not poisoned.
    """
    import threading
    import time

    before = set(threading.enumerate())
    yield
    import claude_code_tts.daemon as daemon_mod

    def leaked() -> list[threading.Thread]:
        return [t for t in threading.enumerate() if t not in before and t.is_alive()]

    deadline = time.monotonic() + 3.0
    while leaked() and time.monotonic() < deadline:
        time.sleep(0.05)
    still = leaked()
    if not still:
        return
    daemon_mod._shutdown_requested = True
    deadline = time.monotonic() + 5.0
    while leaked() and time.monotonic() < deadline:
        time.sleep(0.05)
    daemon_mod._shutdown_requested = False
    pytest.fail("test left running thread(s): " + ", ".join(t.name for t in still))
