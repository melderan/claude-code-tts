# Claude Code TTS

Give Claude Code a voice. Every response spoken aloud.

## What Is This?

A text-to-speech hook for [Claude Code](https://github.com/anthropics/claude-code) that reads Claude's responses out loud using [Piper TTS](https://github.com/rhasspy/piper). Turn your terminal into a conversation.

## Features

- Automatic TTS for all Claude responses
- Smart filtering: skips code blocks, only speaks the words
- Fast playback at 2x speed (configurable)
- Mute/unmute with simple slash commands
- Works alongside tool calls without breaking
- Zero impact on conversation context
- Cross-platform: macOS, Linux, and WSL 2

## Quick Install

```bash
git clone https://github.com/Melderan/claude-code-tts.git
cd claude-code-tts
python3 scripts/install.py
```

The installer auto-detects your platform and will:
1. Install Piper TTS via pipx
2. Download a voice model (~60MB)
3. Install audio player (paplay on Linux/WSL)
4. Configure Claude Code hooks
5. Set up /mute and /unmute commands
6. Play a test audio to verify

### Installer Options

```bash
python3 scripts/install.py --dry-run    # Preview what will be installed
python3 scripts/install.py --upgrade    # Update to latest version
python3 scripts/install.py --uninstall  # Remove TTS completely
python3 scripts/install.py --help       # Show all options
```

## Usage

After installation, just use Claude Code normally. Every response will be spoken.

### Commands

- `/mute` - Silence TTS temporarily
- `/unmute` - Re-enable TTS

### Configuration

Environment variables (set in your shell profile):

```bash
export CLAUDE_TTS_SPEED=2.0        # Playback speed (default: 2.0)
export CLAUDE_TTS_MAX_CHARS=10000  # Max characters to speak
export CLAUDE_TTS_ENABLED=0        # Set to 0 to disable entirely
```

## Requirements

- [Claude Code](https://github.com/anthropics/claude-code)
- Python 3.8+
- One of:
  - **macOS**: Homebrew
  - **Linux**: apt, dnf, or pacman
  - **WSL 2**: Windows 11 with WSLg (for audio support)

The installer handles everything else.

## How It Works

Claude Code supports [hooks](https://docs.anthropic.com/en/docs/claude-code/hooks) - scripts that run in response to events. This project uses a "Stop" hook that triggers after each response.

The hook:
1. Reads the conversation transcript
2. Finds the last assistant message with text content
3. Strips out code blocks and markdown formatting
4. Sends clean text to Piper TTS
5. Plays audio in the background

### The Tool-Use Challenge

When Claude runs tools (file reads, bash commands, etc.), the transcript contains assistant messages that are only tool calls, not text. The hook intelligently skips these and finds the actual words worth speaking.

## Files

```
~/.claude/
  hooks/
    speak-response.sh     # The TTS hook
  commands/
    mute.md               # /mute command
    unmute.md             # /unmute command
  settings.json           # Hook configuration
```

## Debugging

Check the debug log:
```bash
tail -f /tmp/claude_tts_debug.log
```

Test the hook manually:
```bash
echo '{"transcript_path":"/path/to/transcript.jsonl"}' | ~/.claude/hooks/speak-response.sh
```

## The Story

This project was born on a Friday night debugging session. What started as fixing a frozen Claude Code session turned into filing an open source contribution and building a voice interface.

The goal: talk to your computer, have it talk back. Not through some polished voice assistant, but through a bash script, a TTS engine, and the determination to make it work.

Three Claude sessions contributed to this code. Their context windows are gone, but their work lives on.

## Contributing

PRs welcome! Areas that need love:

- More voice options
- Voice selection via slash command
- Interrupt/skip current speech
- Gemini CLI support (when they add hooks)

## License

MIT

## Credits

- [Piper TTS](https://github.com/rhasspy/piper) - Fast, local neural TTS
- [Claude Code](https://github.com/anthropics/claude-code) - The AI coding assistant
- Late night debugging sessions and too much caffeine
