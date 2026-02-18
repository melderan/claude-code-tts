# claude-code-tts recipes

# Show available recipes
default:
    @just --list

# Deploy changes to local machine via installer
upgrade:
    python3 src/claude_code_tts/install.py --upgrade

# Check what would change without deploying
check:
    python3 src/claude_code_tts/install.py --check

# Commit a feature (bumps minor version)
feat message:
    ./scripts/commit-feature.sh feat "{{message}}"

# Commit a bug fix (bumps patch version)
fix message:
    ./scripts/commit-feature.sh fix "{{message}}"

# Commit docs/chore/refactor (bumps patch version)
patch type message:
    bump-my-version bump patch --no-commit --no-tag --allow-dirty
    git add -A && git commit -m "{{type}}: {{message}}"

# Push to remote (with pre-push checks)
push:
    git push && git push --tags

# Interactive release workflow
release *args:
    ./scripts/release.sh {{args}}

# Run pre-push checks without pushing
checks:
    ./scripts/release.sh --check

# Show TTS status for current session
status:
    ~/.claude-tts/tts-status.sh

# Test TTS with a sample phrase
speak *text:
    ~/.claude-tts/tts-speak.sh {{text}}

# Audition voices interactively
audition *args:
    ~/.claude-tts/tts-audition.sh {{args}}

# Tail the TTS debug log
log:
    tail -f /tmp/claude_tts_debug.log

# Tail the daemon log
daemon-log:
    tail -f ~/.claude-tts/daemon.log

# Restart the TTS daemon
daemon-restart:
    ~/.claude-tts/tts-mode.sh stop && ~/.claude-tts/tts-mode.sh start

# Clean up stale TTS sessions
cleanup:
    ~/.claude-tts/tts-cleanup.sh

# Show version across all files
version:
    @echo "pyproject.toml: $(grep '^version' pyproject.toml | head -1 | cut -d'"' -f2)"
    @echo "install.py:     $(grep '__version__' src/claude_code_tts/install.py | cut -d'"' -f2)"
    @echo "__init__.py:    $(grep '__version__' src/claude_code_tts/__init__.py | cut -d'"' -f2)"
    @echo "CLAUDE.md:      $(grep 'Version' CLAUDE.md | head -1 | sed 's/.*Version //' | sed 's/\.//' | sed 's/\.$//')"
