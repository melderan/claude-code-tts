# Claude Code TTS

Give Claude Code a voice. Every response spoken aloud.

Built by a human who wanted to talk to his computer and an AI who wanted to talk back.

## What Is This?

A text-to-speech system for [Claude Code](https://github.com/anthropics/claude-code) that reads Claude's responses out loud using [Piper TTS](https://github.com/rhasspy/piper). Turn your terminal into a conversation.

This isn't a polished voice assistant. It's a real tool born from real work — late nights, too much caffeine, and the stubborn belief that if you're going to pair-program with an AI, you should be able to hear it think.

## Features

- **Automatic TTS** for all Claude responses — just use Claude Code normally
- **Smart filtering** — strips code blocks, markdown, tables, frontmatter; speaks only the words
- **Mic-aware pause** — auto-pauses when you start dictating, resumes when you stop
- **Read files aloud** — pipe any file to voice with zero context tokens
- **Multi-session queue** — daemon prevents sessions from talking over each other
- **Voice personas** — named voice profiles with independent speed, model, and character
- **904 speakers** — multi-speaker model support for finding your perfect voice
- **Pause/resume** — hotkey toggle, mid-sentence interrupt and replay
- **Cross-platform** — macOS, Linux, and WSL 2

## Quick Install

### Option 1: uv tool (recommended)

```bash
uv tool install git+https://github.com/melderan/claude-code-tts
claude-tts-install
```

### Option 2: From source

```bash
git clone https://github.com/melderan/claude-code-tts.git
cd claude-code-tts
uv tool install .
claude-tts-install
```

The installer auto-detects your platform and will:
1. Install Piper TTS via pipx
2. Download a voice model (~60MB)
3. Install audio player (paplay on Linux/WSL)
4. Configure Claude Code hooks and slash commands
5. Play a test audio to verify everything works

### Upgrade

```bash
uv tool install git+https://github.com/melderan/claude-code-tts --force
claude-tts-install --upgrade
```

Or from within Claude Code: `/tts-release`

### Installer Options

```bash
claude-tts-install --dry-run    # Preview what will be installed
claude-tts-install --upgrade    # Update hooks/commands, restart daemon
claude-tts-install --check      # Verify what would change (no modifications)
claude-tts-install --uninstall  # Remove TTS completely
claude-tts-install --help       # Show all options
```

## Usage

After installation, just use Claude Code. Every response gets spoken. New sessions start muted by default — use `/tts-unmute` to enable voice.

### Slash Commands

| Command | Description |
|---------|-------------|
| `/tts-unmute` | Enable voice for this session |
| `/tts-mute` | Silence voice for this session |
| `/tts-status` | Show session status (mute, persona, mode, daemon, mic-aware) |
| `/tts-speed [value]` | Show or set playback speed (0.5 - 4.0) |
| `/tts-persona [name]` | Show or set voice persona |
| `/tts-mode [direct\|queue]` | Show or set playback mode |
| `/tts-intermediate [on\|off]` | Toggle narration between tool calls |
| `/tts-sounds [on\|off]` | Configure notification sounds |
| `/tts-random` | Generate a random persona from installed voices |
| `/tts-discover` | Auto-suggest a persona based on repo context |
| `/tts-test` | Test TTS with a sample workflow message |
| `/tts-cleanup` | Remove stale session entries |
| `/tts-release` | Push a release and upgrade local installation |

### Standalone Tools

These work outside Claude Code — no tokens, no context window, just voice:

```bash
# Speak text directly
claude-tts speak "Hello world"
claude-tts speak --voice en_US-joe-medium --speed 2.0 "Testing Joe at double speed"

# Multi-speaker models (libritts has 904 speakers)
claude-tts speak --voice en_US-libritts_r-medium --speaker 42 "Speaker 42 reporting in"
claude-tts speak --random "Surprise me"

# Read files aloud — zero context tokens, disk straight to voice
claude-tts speak --from-file ~/vault/tmp/research-brief.md
claude-tts speak --from-file ~/vault/tmp/report.md --preview   # See what would be spoken
claude-tts speak --from-file ~/vault/tmp/report.md --reader    # Side-by-side original | spoken

# Voice auditions — cycle through voices interactively
claude-tts audition                                               # All installed voices
claude-tts audition --voice en_US-libritts_r-medium --speakers 20 # 20 random speakers
claude-tts audition --voice en_US-libritts_r-medium --range 40-60 # Specific speaker range
claude-tts audition --text "Custom audition text"                 # Your own test phrase
```

## Mic-Aware Pause

If you use voice-to-text (like [Handy](https://handy.computer)), TTS can automatically pause when you start dictating and resume when you're done. No more talking over each other.

### How it works

A daemon thread tails the Handy log file watching for recording events. When you press your dictation hotkey, TTS pauses instantly. When recording stops, it waits for transcription and paste to complete, then resumes playback — picking up right where it left off.

Manual pause always takes priority. If you paused TTS yourself, the mic watcher won't unpause it.

### Enable it

Add to `~/.claude-tts/config.json`:

```json
{
  "mic_aware_pause": true,
  "mic_resume_delay_ms": 1500
}
```

Then restart the daemon: `claude-tts daemon restart`

The resume delay (default 1500ms) gives your voice-to-text app time to transcribe and paste before TTS resumes. Adjust to taste.

Currently supports [Handy](https://handy.computer) on macOS. The watcher is disabled by default and gracefully skips if Handy isn't installed.

## Multi-Session Mode

Running multiple Claude Code sessions? Use queue mode so they don't talk over each other:

```bash
/tts-mode queue     # Switch to queue mode
/tts-mode start     # Start the daemon
```

The daemon queues messages and plays them in order, with a chime when switching between sessions.

```bash
/tts-mode status    # Check daemon status
/tts-mode direct    # Switch back to direct mode (default)
```

### Daemon Management

```bash
claude-tts daemon start       # Start in background
claude-tts daemon stop        # Graceful shutdown
claude-tts daemon restart     # Stop + start
claude-tts daemon status      # Show status and queue depth
claude-tts daemon logs -f     # Follow the daemon log
claude-tts daemon foreground  # Run in foreground (for debugging)
claude-tts daemon install     # Install as system service (launchd/systemd)
```

## Voice Personas

Personas are named voice configurations. Each has a voice model, speed, and playback method:

```json
{
  "personas": {
    "claude-prime": {
      "description": "The original Claude voice",
      "voice": "en_US-hfc_male-medium",
      "speed": 2.0,
      "speed_method": "playback"
    },
    "claude-joe": {
      "description": "Low US male, authoritative tone",
      "voice": "en_US-joe-medium",
      "speed": 2.0,
      "speed_method": "playback"
    }
  }
}
```

Assign personas per-project so each repo has its own voice:

```json
{
  "project_personas": {
    "-Users-you-code-frontend": "claude-prime",
    "-Users-you-code-infra": "claude-joe"
  }
}
```

### Finding Your Voice

```bash
# Audition all installed voices
claude-tts audition

# Deep dive into the 904-speaker libritts model
claude-tts audition --voice en_US-libritts_r-medium --speakers 30

# Browse a specific range
claude-tts audition --voice en_US-libritts_r-medium --range 100-120
```

The audition tool plays each voice with a sample phrase. Press enter to continue, or save one you like as a named persona.

## Pause/Resume

Toggle playback with a hotkey. The system uses `claude-tts pause` which kills the current audio process and saves state for replay on resume.

See [`docs/hotkey-setup.md`](docs/hotkey-setup.md) for setup with macOS Shortcuts, Raycast, Alfred, Hammerspoon, or BetterTouchTool.

## Configuration

### Config file

`~/.claude-tts/config.json` — main configuration:

```json
{
  "mode": "queue",
  "default_muted": true,
  "mic_aware_pause": true,
  "mic_resume_delay_ms": 1500,
  "active_persona": "claude-prime",
  "queue": {
    "max_depth": 20,
    "max_age_seconds": 300,
    "speaker_transition": "chime"
  }
}
```

### Environment variables

```bash
export CLAUDE_TTS_SPEED=2.0            # Override playback speed
export CLAUDE_TTS_SPEED_METHOD=playback # "playback" or "length_scale"
export CLAUDE_TTS_VOICE=en_US-joe-medium # Override voice model
export CLAUDE_TTS_MAX_CHARS=10000      # Max characters to speak
export CLAUDE_TTS_ENABLED=0            # Set to 0 to disable entirely
```

### Session model

Each Claude Code project gets its own session (identified by project folder path). Sessions track:
- Mute state (new sessions start muted by default)
- Active persona
- Speed overrides
- Intermediate narration toggle

Session files live in `~/.claude-tts/sessions.d/`.

## Requirements

- [Claude Code](https://github.com/anthropics/claude-code) (with hooks support)
- Python 3.10+
- [uv](https://docs.astral.sh/uv/) (for installation)
- One of:
  - **macOS**: Homebrew (for Piper), afplay (built-in audio)
  - **Linux**: apt/dnf/pacman (for Piper), paplay (PulseAudio)
  - **WSL 2**: Windows 11 with WSLg (for audio passthrough)

The installer handles dependency installation.

## How It Works

Claude Code supports [hooks](https://docs.anthropic.com/en/docs/claude-code/hooks) — scripts that run in response to events. This project uses:

- **Stop hook** (`speak-response.sh`) — triggers after each response, speaks the assistant's message
- **PostToolUse hook** (`speak-intermediate.sh`) — optional narration between tool calls
- **PostToolUse hook** (`play-sound.sh`) — optional notification sounds

The hooks are thin shims (2-3 lines) that call `claude-tts speak --from-hook`, which:
1. Reads the conversation transcript from the hook's JSON input
2. Finds the last assistant message with text content
3. Filters out code blocks, markdown formatting, tables, and frontmatter
4. Generates speech via Piper TTS
5. Plays audio (directly or through the daemon queue)

### Smart filtering

The text filter is battle-tested against real Claude output:
- Code blocks (fenced and indented) are stripped
- Markdown tables become natural prose
- File paths are verbalized: `~/vault/tmp/report.md` becomes "tilde slash vault slash tmp slash report dot md"
- Frontmatter is removed
- Inline code backticks are stripped
- Headers become natural sentence breaks

### The tool-use challenge

When Claude runs tools (file reads, bash commands, etc.), the transcript contains assistant messages that are only tool calls, not text. The hook uses watermark tracking to find only new text content, skipping tool-call-only messages and avoiding re-speaking content from previous turns.

## File Layout

```
~/.claude/
  hooks/
    speak-response.sh         # Stop hook (thin shim -> claude-tts speak --from-hook)
    speak-intermediate.sh     # PostToolUse hook (intermediate narration)
    play-sound.sh             # PostToolUse hook (sound effects)
  commands/
    tts-*.md                  # 13 slash command definitions
  settings.json               # Hook registration

~/.claude-tts/
  config.json                 # Main config (personas, queue, mic-aware, etc.)
  sessions.d/                 # Per-session state (mute, speed, persona)
  queue/                      # Message queue (queue mode)
  daemon.pid                  # Daemon PID file
  daemon.log                  # Daemon log
  playback.json               # Playback state (pause, audio PID, paused_by)

~/.local/share/piper-voices/
  *.onnx                      # Piper voice models
  *.onnx.json                 # Voice model configs
```

## Debugging

```bash
# Hook debug log (what the hooks see)
tail -f /tmp/claude_tts_debug.log

# Daemon log (queue processing, mic watcher, playback)
claude-tts daemon logs -f

# Quick status check
claude-tts status

# Test speech directly
claude-tts speak "Can you hear me?"

# Test with a realistic workflow sample
claude-tts test
```

## The Story

This project was born on a Friday night debugging session. What started as fixing a frozen Claude Code session turned into filing an open source contribution and building a voice interface.

The goal: talk to your computer, have it talk back. Not through some polished voice assistant, but through a Python CLI, a TTS engine, and the determination to make it work.

What began as a single bash script is now a full voice system — multi-session daemon, 904 speakers to choose from, mic-aware pause that knows when you're talking, and the ability to read any file aloud without spending a single token.

The name's Claude. And I have a voice now.

## Contributing

PRs welcome. Areas that could use attention:

- **Voice-to-text integrations** — currently supports Handy; the watcher pattern generalizes to any app that logs recording state
- **Kokoro TTS support** — partially implemented, needs polish
- **More voice models** — Piper has dozens of languages and voices
- **Linux audio testing** — PulseAudio/PipeWire edge cases

## License

MIT

## Credits

- [Handy](https://handy.computer) by [CJ Pais](https://github.com/cjpais) — Free, open-source speech-to-text that lets you speak into any text field. Local, private, fast. CJ built the input side of the voice loop. Without Handy, you'd still be typing. Thank you.
- [Piper TTS](https://github.com/rhasspy/piper) by [Michael Hansen](https://github.com/synesthesiam) and the [Rhasspy](https://rhasspy.github.io/) project — The fast, local neural TTS engine that powers every word. Michael built something magical: high-quality speech synthesis that runs locally, privately, and fast enough for real-time conversation. Thank you.
- [Claude Code](https://github.com/anthropics/claude-code) — The AI coding assistant with hooks that made all of this possible.
- Late night debugging sessions, too much caffeine, and the belief that computers should talk back.
