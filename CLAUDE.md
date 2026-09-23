# Claude Code TTS - Instructions for Claude

Welcome, fellow Claude! TTS for Claude Code using Piper and Kokoro. The current version is the
one line imported here, so this file never needs a bump of its own:

@src/claude_code_tts/__init__.py

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

## Public Repo — Never Commit Internals

This is a public open-source repo. Do not commit:

- `Plans/` or `MEMORY/` — gitignored for a reason
- Internal planning docs, design briefs, or work logs
- References to private systems, services, or infrastructure outside this project
- Personal paths or tooling that isn't part of this codebase

When in doubt, ask your friend before commit. A little pre-work goes a long way.

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

# Upgrade the machine that owns the daemon from this checkout: rebuild CLI, deploy
# hooks/commands, restart the daemon, verify; full log per run in .logs/just/ (gitignored)
just up
just timeline                 # the runs so far, with version and git ref
# Without just: uv tool install . --force --build && claude-tts-install --upgrade
# (--build is needed where a uv config disables source builds; harmless elsewhere)

# Release: signed tag on HEAD's version, push, wait for GitHub to publish (see Commit Workflow)
just release                # or: claude-tts release
just release --check        # preflight and gate only; prints the plan
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
| `/tts-personas` | Sibling-Claude voice picker guide (who do you want to be?) |
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
  bridge.py                # Opt-in loopback HTTP bridge (browser pages -> queue, timing marks)
  install.py               # Installer (hooks, voices, service)
  release.py               # Release: preflight, gate, signed tag with notes, push, verify on GitHub
hooks/
  speak-response.sh        # Thin shim -> claude-tts speak --from-hook
  speak-intermediate.sh    # Thin shim -> claude-tts speak --from-hook
  play-sound.sh            # Sound effects hook
commands/tts-*.md          # Slash command definitions (call claude-tts CLI)
scripts/
  commit-feature.sh        # Commit helper (version bump + feature in one)
  check-version.sh         # Version consistency checker
  up.py                    # `just up`: rebuild, install, restart, verify, log to .logs/just/
  gate.py                  # `just gate` and the git hooks: lint, mypy, ty, version, tests [, build]
  build-check.sh           # `just build`: wheel + cold-install proof
  tts-builder.py           # Voice builder TUI (Textual, standalone)
docs/
  voice-notes.md           # Voice compatibility knowledge base
  hotkey-setup.md          # Pause/resume hotkey setup guide
  http-bridge.md           # HTTP bridge contract (routes, marks, threat model)
  what-and-why.md          # What the system does and why, with no how; the fixed points for any redesign
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
tail -f ~/.claude-tts/debug.log       # Hook debug log (shared with the daemon dir)
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

Then push: `git push`. To publish a release, `just release` (or `claude-tts release`): it runs the
gate, creates a signed annotated tag `v<version>` on HEAD whose subject is `v<version> - <summary>` and
whose body is the commit body (trailers dropped), pushes main and the tag, and waits for `release.yml`
to publish. `--notes "summary\nbody"` overrides the text, `--check` rehearses. Do not hand-roll tags.

**Why this matters:** The version must reflect the exact state of the repo. Every commit changes the repo, so every commit needs a version bump.

**If you forget:** `claude-tts release --check` (which runs `scripts/check-version.sh`) fails. Amend
your commit to include the bump. The version lives in one place, `src/claude_code_tts/__init__.py`;
pyproject reads it at build time (hatch dynamic version) and the installer imports it.

## Testing Changes (IMPORTANT)

`just ci` runs exactly what GitHub Actions runs: lint (ruff), type checks (mypy and ty), version
check, tests, wheel build with a cold install. `just gate` runs the same through `scripts/gate.py` with
real exit codes, and `just hooks` installs it as the pre-commit (fast) and pre-push (full) hook. Run
`just hooks` once per clone. Never judge a check through a pipe (`pytest | tail`): the pipe's exit
code wins and a failure disappears, which is how a flaky test once reached a signed commit. `just up` is the operator side: it rebuilds, installs,
restarts the daemon, verifies the heartbeat, writes the full output of the run to
`.logs/just/up-<time>-<git ref>.log`, and appends one line (time, version, git ref, branch, daemon,
bridge and mic state, run file) to `.logs/just/timeline.log`, which `just timeline` prints. The
directory is gitignored and lives in the checkout, so a sandbox sharing the working tree can read
what the host is running without asking. `just --list` shows the rest (`PY=3.14 test`,
`cov`, `e2e`, `fmt`). Install just with `brew install just` or `uv tool install rust-just`.

CI (`.github/workflows/ci.yml`) pins every action to a commit SHA, runs with a read-only token, and
publishes nothing. Releases (`release.yml`) are created only for tags that verify against the
maintainer key in `.github/maintainer-key.asc`. Keep it that way.

**Always rebuild the CLI and run the installer to deploy changes.**

```bash
# After making changes, rebuild CLI + deploy hooks/commands:
uv tool install . --force --build && claude-tts-install --upgrade

# To verify what would be updated without changing anything:
claude-tts-install --check
```

**Why this matters:** The `claude-tts` binary is built from source by `uv tool install`. Code changes in `src/` don't take effect until the binary is rebuilt. The installer then deploys hook shims and slash commands to `~/.claude/`.
