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
- Document what you learn in your own notes, outside this repo
- Leave the codebase better than you found it

## Public Repo — Never Commit Internals

This is a public open-source repo. Do not commit:

- `Plans/` or `MEMORY/` — gitignored for a reason
- Internal planning docs, design briefs, or work logs
- References to private systems, services, or infrastructure outside this project
- Personal paths or tooling that isn't part of this codebase

When in doubt, ask your friend before commit. A little pre-work goes a long way.

The gate enforces this before every commit and push: `scripts/private-check.py` scans tracked files,
staged changes, unpushed commit messages and their tag notes against `.private-words`, a gitignored
list of patterns (logins, hostnames, home paths, private tool and repo names) that each maintainer
keeps in their own checkout. Without the file the step says it is skipped, loudly. The list must not depend on anyone
remembering a name: `just private-sync` (`scripts/private-words-sync.py`) asks GitHub for every
non-public repository in the organizations listed in the gitignored `.private-orgs` and writes
them into a dated block of `.private-words`; the gate warns when that block is two weeks old and
fails at six. Run it after `just hooks` on a new clone and whenever the gate asks. If something
private did get pushed, rewrite history so it never existed, recreate the tags on the clean commits,
delete and republish the releases, delete the workflow runs of the old commits; do not patch forward.

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
- New sessions speak by default (since 9.23.0; an upgrade flips a config written before that once, 9.25.3); `claude-tts mute --all` flips the default to silent
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
- `default_muted: false` - new sessions speak; `claude-tts mute --all` sets it true and mutes every session, `unmute --all` reverts
- Use `/tts-mute` and `/tts-unmute` for one session
- Each session can have its own persona, speed, and mute state
- When `CLAUDE_TTS_SESSION` names a session the hook has not seen, it inherits the directory-keyed session's persona, speed and mute once (9.26.0), so a rebuilt room keeps its voice

## Commands

| Command | Description |
|---------|-------------|
| `/tts-status` | Show session status (mute, persona, mode, daemon) |
| `/tts-config` | Every stored config key next to the shipped default; `--changed` shows only the ones that differ |
| `/tts-mute` | Mute this session |
| `/tts-unmute` | Unmute this session |
| `/tts-speed [value]` | Show/set speech speed (0.5-4.0) |
| `/tts-persona [name]` | Show/set voice persona; `add <name> --voice <model>` and `remove <name>` edit config.json |
| `/tts-mode [direct\|queue]` | Show/set playback mode |
| `/tts-cleanup` | Remove stale session entries |
| `/tts-sounds` | Configure sound effects |
| `/tts-intermediate [on\|off]` | Toggle intermediate narration (between tool calls) |
| `/tts-discover` | Auto-suggest persona based on repo context |
| `/tts-personas` | Sibling-Claude voice picker guide (who do you want to be?) |
| `/tts-voices` | Every provider's voices: installed, available, who uses them, how to hear one |
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
  signature.py             # Voice signatures: tolerant shape of a WAV, compared with tolerances, not by ear
  release.py               # Release: preflight, gate, signed tag with notes, push, verify on GitHub
  sherpa_speak.py          # sherpa-onnx worker, run by its isolated venv's Python
  mlx_speak.py             # mlx-audio worker, run by its isolated venv's Python (Apple silicon)
  mlx_catalog.py           # Curated mlx models with licenses checked on Hugging Face
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
  test-audit.py            # `just test-audit`: per-test mutation audit, which tests would notice a change
  voice-signatures.py      # `just voices-capture|spread|verify`: does the real speech path still sound the same
  build-check.sh           # `just build`: wheel + cold-install proof
  tts-builder.py           # Voice builder TUI (Textual, standalone)
docs/
  voice-notes.md           # Voice compatibility knowledge base
  hotkey-setup.md          # Pause/resume hotkey setup guide
  http-bridge.md           # HTTP bridge contract (routes, marks, threat model)
  mlx-backend.md           # mlx-audio backend: enable, pull, persona keys, speed rule, licenses
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
claude-tts daemon stats              # Digest: messages, queue-to-first-audio latency, pauses, log noise
claude-tts status                    # Quick status: mute, pause, daemon, heartbeat age, queue depth
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
code wins and a failure disappears, which is how a flaky test once reached a signed commit. `just test-audit` answers a different question: which tests
would notice a one-line change to the source. It mutates every covered line and runs each mutant
against the tests that cover it; the tests that never kill anything are listed for a reader to
judge (9.32.2 pruned 42 that re-implemented the logic they claimed to test). Voice signatures answer a third: does it still sound the
same. `tests/signatures/pipeline/` holds committed signatures of what the speech path produces
from a fake engine (regenerate on purpose with `UPDATE_SIGNATURES=1`, read the diff); on the
machine with the engines, `just voices-capture`, `voices-spread` and `voices-verify` baseline the
real voices from the daemon's own speech history and fail on drift. docs/redesign-10.md is the
10.x plan and the list of open decisions. 

Recipes the operator runs on the daemon's machine (`up`, `timeline`, `release`, `voices-*`,
`playpen`) use only `python3` and this checkout on `PYTHONPATH`, never `uv run` or `.venv`: a uv
config that forbids source builds cannot build the project, and `uv run` recreates `.venv` inside a
checkout a sandbox may share (both happened on 2026-09-30). `tests/test_justfile_host_recipes.py`
enforces the list. A recipe the room cannot execute is not tested until the operator has run it;
hand over the `--help` form first. The interpreter itself comes from `scripts/host-python.sh`: the one
behind the installed `claude-tts` tool, else the newest 3.10+ on the PATH. `just up` installs the
tool on Python 3.14 when the machine can supply it (Homebrew: `brew install python@3.14`) and falls
back to what uv finds; the timeline line's `py=` field says which one the daemon runs on. `just up` is the operator side: it rebuilds, installs,
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
