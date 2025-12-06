# Claude Code TTS - Instructions for Claude

Welcome, fellow Claude! This repo contains the text-to-speech system for Claude Code. Version 4.1.0.

## What This Project Does

This project adds voice output to Claude Code. When you respond to a user, your words get spoken aloud through their speakers using Piper TTS. It's like giving us a voice.

## v4.1.0 Features

- Multi-session daemon with queue mode
- Auto-start daemon on first message
- Faster chime (Tink.aiff instead of Pop.aiff)
- Fixed hooks format with `matcher` field
- Filter `<thinking>` blocks from TTS output
- `/tts-persona` command for per-session voice switching

### Critical: Hooks Format
Claude Code hooks REQUIRE this format with `matcher` field:
```json
{
  "hooks": {
    "Stop": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "~/.claude/hooks/speak-response.sh",
            "timeout": 180
          }
        ]
      }
    ]
  }
}
```
Without `matcher: "*"`, hooks silently fail with "Found 0 hook matchers in settings".

## Project Structure

```
claude-code-tts/
  src/claude_code_tts/
    install.py           # Main installer (use via uv tool install)
    __init__.py
  hooks/
    speak-response.sh    # The main TTS hook
  commands/
    tts-mute.md          # /tts-mute command
    tts-unmute.md        # /tts-unmute command
    tts-mode.md          # /tts-mode command (daemon control)
    tts-speed.md         # /tts-speed command
    tts-sounds.md        # /tts-sounds command
    tts-persona.md       # /tts-persona command (voice switching)
  scripts/
    tts-daemon.py        # Queue daemon for multi-session
    tts-mode.sh          # Mode management script
    check-version.sh     # Version consistency checker
  services/
    com.claude-tts.daemon.plist  # macOS launchd
    claude-tts.service           # Linux systemd
  examples/
    settings.json        # Example Claude Code settings
    config.json          # Example TTS config
```

## Installation

```bash
uv tool install git+https://github.com/melderan/claude-code-tts
claude-tts-install
```

## Two Modes

### Direct Mode (default)
- Hook plays audio immediately via Piper + afplay/paplay
- Simple, no daemon needed
- Can overlap if multiple sessions respond simultaneously

### Queue Mode
- Messages queued to `~/.claude-tts/queue/`
- Daemon processes FIFO, plays one at a time
- Chime between different speakers/sessions
- Auto-starts daemon on first message

Switch modes: `/tts-mode queue` or `/tts-mode direct`

## Key Files at Runtime

```
~/.claude-tts/
  config.json           # TTS configuration (mode, personas, speeds)
  tts-daemon.py         # Daemon script
  tts-mode.sh           # Mode management
  daemon.pid            # PID file for status checks
  daemon.log            # Daemon logs
  queue/                # Message queue (queue mode)
  voices/               # Piper voice models

~/.claude/
  hooks/speak-response.sh   # The TTS hook
  commands/tts-*.md         # Slash commands
  settings.json             # Hook configuration (needs matcher!)
```

## Debugging

```bash
# Debug log (hook activity)
tail -f /tmp/claude_tts_debug.log

# Daemon log
tail -f ~/.claude-tts/daemon.log

# Daemon status
~/.claude-tts/tts-mode.sh status

# Test hook manually
echo '{"transcript_path":"/path/to/transcript.jsonl"}' | bash -x ~/.claude/hooks/speak-response.sh
```

## Known Issues

1. **Hooks not loading**: Check `matcher: "*"` in settings.json
2. **Status shows "not running"**: Daemon writes PID file on start
3. **Delay after chime**: We use Tink.aiff (0.56s) not Pop.aiff (1.6s)

## The Story

Built on a Friday night debugging session. What started as fixing a frozen Claude Code session turned into filing an open source contribution and building a voice interface.

Many Claudes have contributed to this code. Their context windows are gone, but their work lives on.

## Development Workflow

### Version Bumping

We use `bump-my-version` (community standard tool). **Never manually edit version strings.**

```bash
# Check current version
bump-my-version show current_version

# See what bumps are available
bump-my-version show-bump

# Bump version (auto-commits and tags)
bump-my-version bump patch   # 4.1.0 -> 4.1.1 (bug fixes)
bump-my-version bump minor   # 4.1.0 -> 4.2.0 (new features)
bump-my-version bump major   # 4.1.0 -> 5.0.0 (breaking changes)

# Push with tags
git push origin main --tags
```

Version is tracked in 4 files (all updated automatically):
- `pyproject.toml` - Python package version
- `src/claude_code_tts/__init__.py` - Module version
- `src/claude_code_tts/install.py` - Installer version
- `CLAUDE.md` - Documentation version

### Verification

```bash
# Verify all versions match
./scripts/check-version.sh

# Sync local install after changes
python3 src/claude_code_tts/install.py --upgrade
python3 src/claude_code_tts/install.py --check
```

### Adding New Commands

When adding a new `/tts-*` command:
1. Create `commands/tts-newcmd.md`
2. Add to installer in **5 places** in `install.py`:
   - Line ~304: preflight check list
   - Line ~406: uninstall cleanup list
   - Line ~589: backup list
   - Line ~690: install loop
   - Line ~1008: version check list
3. Add to README.md commands section
4. Bump version and push

### Pre-commit Checklist

Before pushing:
- [ ] `./scripts/check-version.sh` passes
- [ ] `python3 src/claude_code_tts/install.py --check` shows all current
- [ ] New commands added to all 5 installer locations
- [ ] README.md updated if user-facing changes

## Code Style

- Bash with `set -euo pipefail`
- Python 3.10+ with type hints
- Extensive debug logging
- BSD-compatible commands for macOS
- No emojis (per user preference)
