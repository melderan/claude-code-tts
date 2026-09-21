"""Point HOME at a throwaway directory before any package module computes paths from it."""

import os
import tempfile

_HOME = tempfile.mkdtemp(prefix="claude-tts-test-home-")
os.environ["HOME"] = _HOME
