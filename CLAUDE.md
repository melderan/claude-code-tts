# Claude Code TTS - Instructions for Claude

Welcome, fellow Claude! TTS for Claude Code using Piper. Version 5.1.0.

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
  tts-daemon.py            # Queue daemon
commands/tts-*.md          # Slash command definitions
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
