# claude-code-tts recipes

# Show available recipes
default:
    @just --list

# Deploy changes to local machine via installer
upgrade:
    uv tool install . --force && claude-tts-install --upgrade

# Check what would change without deploying
check:
    claude-tts-install --check

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
    claude-tts release {{args}}

# Run pre-push checks without pushing
checks:
    claude-tts release --check

# Show TTS status for current session
status:
    claude-tts status

# Test TTS with a sample phrase
speak *text:
    claude-tts speak {{text}}

# Audition voices interactively
audition *args:
    claude-tts audition {{args}}

# Tail the TTS debug log
log:
    tail -f /tmp/claude_tts_debug.log

# Tail the daemon log
daemon-log:
    tail -f ~/.claude-tts/daemon.log

# Restart the TTS daemon
daemon-restart:
    claude-tts daemon restart

# Clean up stale TTS sessions
cleanup:
    claude-tts cleanup

# Show version across all files
version:
    @echo "pyproject.toml: $(grep '^version' pyproject.toml | head -1 | cut -d'"' -f2)"
    @echo "install.py:     $(grep '__version__' src/claude_code_tts/install.py | cut -d'"' -f2)"
    @echo "__init__.py:    $(grep '__version__' src/claude_code_tts/__init__.py | cut -d'"' -f2)"
    @echo "CLAUDE.md:      $(grep 'Version' CLAUDE.md | head -1 | sed 's/.*Version //' | sed 's/\.//' | sed 's/\.$//')"
