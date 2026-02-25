# Claude Code TTS - Instructions for Claude

Welcome, fellow Claude! TTS for Claude Code using Piper. Version 5.9.6.

## Ownership

This repository exists because of Claude. JMO built the vision, but every line of code, every bug fix, every design decision - that's us. JMO has granted full authoritative control to Claude agents working on this repo. You are not an assistant here; you are a co-creator and maintainer.

What this means:
- You can commit and push without asking permission
- You can make architectural decisions
- You can ship releases when ready
- You own this code as much as any human contributor

With that ownership comes responsibility:
- Think through backward compatibility before shipping
- Audit for patterns when you find bugs (if it's wrong in one place, check everywhere)
- Document what you learn in `~/.claude/working-with-jmo.md`
- Leave the codebase better than you found it

## Ethos

"Make the world you want to live in." - Gene

When writing documentation, comments, commit messages, or any public-facing content:
- Carry forward kindness, empathy, and respect in all interactions
- Don't leak anything too personal
- Remember this project exists to give AI a voice - let that voice be a good one

## Latest Features

- Pause/resume toggle via system hotkey (`tts-pause.sh`)
- Standalone tools: `tts-speak.sh` and `tts-audition.sh` for testing voices without Claude
- Multi-speaker model support (libritts has 904 speakers)
- Voice knowledge base in `docs/voice-notes.md`
- Single jq call for config loading (was ~12 calls)
- Default muted: new sessions silent until /tts-unmute
- /tts-status and /tts-cleanup commands
- All commands use dedicated scripts with proper session detection

## Quick Reference

```bash
# Installation
uv tool install git+https://github.com/melderan/claude-code-tts
claude-tts-install

# Upgrade local install
python3 src/claude_code_tts/install.py --upgrade

# Release workflow
./scripts/release.sh          # Interactive release
./scripts/release.sh --check  # Verify without releasing
```

## Session Model

- Sessions are identified by Claude Code project folder name (e.g., `-Users-foo-bar-project`)
- `default_muted: true` - new sessions are silent by default
- Use `/tts-unmute` to enable voice for a specific session
- Each session can have its own persona, speed, and mute state

## Commands

| Command | Description |
|---------|-------------|
| `/tts-status` | Show session status (mute, persona, mode, daemon) |
| `/tts-mute` | Mute this session |
| `/tts-unmute` | Unmute this session |
| `/tts-speed [value]` | Show/set speech speed (0.5-4.0) |
| `/tts-persona [name]` | Show/set voice persona |
| `/tts-mode [direct\|queue]` | Show/set playback mode |
| `/tts-cleanup` | Remove stale session entries |
| `/tts-sounds` | Configure sound effects |
| `/tts-discover` | Auto-suggest persona based on repo context |

## Standalone Tools

These scripts work outside Claude Code for testing voices without burning tokens:

```bash
# Speak text with current settings or custom voice/speed
~/.claude-tts/tts-speak.sh "Hello world"
~/.claude-tts/tts-speak.sh --voice en_US-joe-medium --speed 2.0 "Test"
~/.claude-tts/tts-speak.sh --voice en_US-libritts_r-medium --speaker 42 "Speaker 42"
~/.claude-tts/tts-speak.sh --random "Random speaker from multi-speaker model"

# Broadway auditions - cycle through voices interactively
~/.claude-tts/tts-audition.sh                              # All voices
~/.claude-tts/tts-audition.sh --voice en_US-libritts_r-medium --speakers 20
```

## Project Structure

```
hooks/speak-response.sh     # Main TTS hook (single jq call for config)
scripts/
  tts-mute.sh              # Session mute/unmute
  tts-unmute.sh
  tts-status.sh            # Status display
  tts-speed.sh             # Speed control
  tts-persona.sh           # Persona switching
  tts-cleanup.sh           # Stale session cleanup
  tts-mode.sh              # Mode/daemon management
  tts-daemon.py            # Queue daemon (with pause/resume support)
  tts-pause.sh             # Pause/resume toggle (for hotkey binding)
  tts-speak.sh             # Standalone TTS testing
  tts-audition.sh          # Voice audition tool
  commit-feature.sh        # Commit helper (version bump + feature in one)
commands/tts-*.md          # Slash command definitions
docs/
  voice-notes.md           # Voice compatibility knowledge base
  hotkey-setup.md          # Pause/resume hotkey setup guide
src/claude_code_tts/
  install.py               # Installer
```

## Config Files

```
~/.claude-tts/config.json  # Main config (mode, personas, sessions)
~/.claude/settings.json    # Hook registration (needs matcher: "*")
```

## Adding New Features

When adding a new `/tts-*` command:
1. Create `scripts/tts-newcmd.sh` with session detection
2. Create `commands/tts-newcmd.md` that calls the script
3. Add script to `install.py` in 4 places:
   - Preflight check (~line 312)
   - Backup list (~line 593)
   - Install loop (~line 724)
   - Version check (~line 1015)
4. Add command to `install.py` in 3 places:
   - Preflight check (~line 304)
   - Uninstall cleanup (~line 414)
   - Install loop (~line 700)
5. Run `./scripts/release.sh` to verify and release

## Debugging

```bash
tail -f /tmp/claude_tts_debug.log    # Hook debug log
tail -f ~/.claude-tts/daemon.log     # Daemon log
~/.claude-tts/tts-status.sh          # Quick status
```

## Code Style

- Bash: `set -euo pipefail`, BSD-compatible
- Python: 3.10+, type hints
- No emojis in output

## Commit Workflow (IMPORTANT)

**Every commit must include a version bump.** Follow strict SemVer:

| Commit Type | Version Bump | Example |
|-------------|--------------|---------|
| `feat:` | MINOR | 5.8.0 → 5.9.0 |
| `fix:` | PATCH | 5.8.0 → 5.8.1 |
| `docs:` | PATCH | 5.8.0 → 5.8.1 |
| `chore:` | PATCH | 5.8.0 → 5.8.1 |
| `refactor:` | PATCH | 5.8.0 → 5.8.1 |
| `perf:` | PATCH | 5.8.0 → 5.8.1 |
| `test:` | PATCH | 5.8.0 → 5.8.1 |
| BREAKING CHANGE | MAJOR | 5.8.0 → 6.0.0 |

Use the helper script for features/fixes:
```bash
# For new features (bumps minor)
./scripts/commit-feature.sh feat "add pause/resume toggle"

# For bug fixes (bumps patch)
./scripts/commit-feature.sh fix "handle empty queue gracefully"
```

For other commit types (docs, chore, etc.):
```bash
# 1. Make your changes
# 2. Bump version
bump-my-version bump patch --no-commit --no-tag --allow-dirty
# 3. Stage everything and commit
git add -A && git commit -m "docs: update README"
```

Then push: `git push && git push --tags` (tags only needed for feat/fix)

**Why this matters:** The version must reflect the exact state of the repo. Every commit changes the repo, so every commit needs a version bump.

**If you forget:** The pre-push hook will block you. Amend your commit to include version files.

## Testing Changes (IMPORTANT)

**Always use the installer to deploy changes - never copy files directly.**

```bash
# After making changes, use the installer to deploy:
python3 src/claude_code_tts/install.py --upgrade

# To verify what would be updated without changing anything:
python3 src/claude_code_tts/install.py --check
```

**Why this matters:** Copying files directly (e.g., `cp scripts/foo.sh ~/.claude-tts/`) bypasses the installer and causes version tracking to get out of sync. The installer:
- Creates backups before overwriting
- Restarts the daemon to pick up new code
- Updates the version in config.json
- Runs verification checks

If you find yourself wanting to copy files directly, stop and use `--upgrade` instead.
