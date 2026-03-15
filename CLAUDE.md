# Claude Code TTS - Instructions for Claude

Welcome, fellow Claude! TTS for Claude Code using Piper and Kokoro. Version 7.2.4.

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

## Latest Features (v7.0.0)

- **Unified Python CLI** (`claude-tts`) replaces all bash scripts
- Pause/resume toggle via `claude-tts pause`
- Standalone tools: `claude-tts speak` and `claude-tts audition`
- Multi-speaker model support (libritts has 904 speakers)
- Voice knowledge base in `docs/voice-notes.md`
- Default muted: new sessions silent until /tts-unmute
- Daemon management via `claude-tts daemon start|stop|restart|status`

## Quick Reference

```bash
# Installation
uv tool install git+https://github.com/melderan/claude-code-tts
claude-tts-install

# Upgrade local install (rebuilds CLI + deploys hooks/commands)
uv tool install . --force && claude-tts-install --upgrade

# Release workflow
claude-tts release          # Interactive release
claude-tts release --check  # Verify without releasing
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
| `/tts-intermediate [on\|off]` | Toggle intermediate narration (between tool calls) |
| `/tts-discover` | Auto-suggest persona based on repo context |
| `/tts-release` | Push a release and upgrade local installation |

## Standalone Tools

These work outside Claude Code for testing voices without burning tokens:

```bash
# Speak text with current settings or custom voice/speed
claude-tts speak "Hello world"
claude-tts speak --voice en_US-joe-medium --speed 2.0 "Test"
claude-tts speak --voice en_US-libritts_r-medium --speaker 42 "Speaker 42"
claude-tts speak --random "Random speaker from multi-speaker model"

# Read a file aloud (zero context tokens -- disk straight to voice)
claude-tts speak --from-file ~/vault/tmp/report.md
claude-tts speak --from-file ~/vault/tmp/report.md --preview  # See what would be spoken

# Broadway auditions - cycle through voices interactively
claude-tts audition                              # All voices
claude-tts audition --voice en_US-libritts_r-medium --speakers 20
```

## Project Structure

```
src/claude_code_tts/
  __init__.py              # Version only
  cli.py                   # argparse entry point, all subcommand handlers
  config.py                # Config loading, session.d helpers, migration
  session.py               # get_session_id() - PROJECT_ROOT -> folder lookup
  audio.py                 # Piper/Kokoro/afplay backends, tts_speak()
  filter.py                # Text filter (filter_text for responses, filter_document for files)
  daemon.py                # Queue daemon (pause/resume, heartbeat)
  install.py               # Installer (hooks, voices, service)
  release.py               # Release workflow (checks + version bump)
hooks/
  speak-response.sh        # Thin shim -> claude-tts speak --from-hook
  speak-intermediate.sh    # Thin shim -> claude-tts speak --from-hook
  play-sound.sh            # Sound effects hook
commands/tts-*.md          # Slash command definitions (call claude-tts CLI)
scripts/
  commit-feature.sh        # Commit helper (version bump + feature in one)
  check-version.sh         # Version consistency checker
  tts-builder.py           # Voice builder TUI (Textual, standalone)
docs/
  voice-notes.md           # Voice compatibility knowledge base
  hotkey-setup.md          # Pause/resume hotkey setup guide
```

## Config Files

```
~/.claude-tts/config.json  # Main config (mode, personas, sessions)
~/.claude/settings.json    # Hook registration (needs matcher: "*")
```

## Adding New Features

When adding a new `/tts-*` command:
1. Add the handler function `cmd_newcmd()` in `cli.py`
2. Wire the subparser in `cli.py`'s `main()` function
3. Create `commands/tts-newcmd.md` that calls `claude-tts newcmd $ARGUMENTS`
4. Add `"tts-newcmd.md"` to the `MANIFEST["commands"]` list in `install.py`
5. Run `claude-tts release --check` to verify

## Debugging

```bash
tail -f /tmp/claude_tts_debug.log    # Hook debug log
claude-tts daemon logs --follow      # Daemon log
claude-tts status                    # Quick status
```

## Code Style

- Python: 3.10+, type hints, zero runtime dependencies (stdlib only)
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

**Always rebuild the CLI and run the installer to deploy changes.**

```bash
# After making changes, rebuild CLI + deploy hooks/commands:
uv tool install . --force && claude-tts-install --upgrade

# To verify what would be updated without changing anything:
claude-tts-install --check
```

**Why this matters:** The `claude-tts` binary is built from source by `uv tool install`. Code changes in `src/` don't take effect until the binary is rebuilt. The installer then deploys hook shims and slash commands to `~/.claude/`.
