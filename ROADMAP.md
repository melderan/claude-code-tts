# Claude Code TTS - Roadmap

Ideas captured from the late night session that started it all.

## Current State (v1.1)

- Piper TTS with configurable voice models
- Stop hook triggers after each response
- Smart filtering skips tool_use blocks, finds actual text
- /mute and /unmute slash commands (session-aware)
- /persona command for switching voice configurations
- Session-local settings (each Claude window can have own persona/mute state)
- Persona system with speed, voice, and speed_method configs
- Cross-platform: macOS (afplay), Linux (paplay/aplay), WSL 2 (WSLg)
- Python installer with interactive mode, persona management, pre-flight checks

## Completed

### Voice Identity Per Session (DONE)

Each Claude session can have its own voice personality via personas.

- Sessions auto-detected from project hash
- /persona command switches voice for current session only
- Config file stores session overrides
- Multiple Claude windows can use different voices simultaneously

### Linux Support (DONE)

- paplay for PulseAudio, aplay for ALSA
- WSL 2 support via WSLg's built-in PulseAudio
- Platform-specific speed methods (playback on macOS, length_scale elsewhere)

### Voice Selection Command (DONE)

The /persona command handles this:
- List available personas
- Switch personas per-session
- Configure via installer or config file

## Future Ideas

### Voice Model Browser/Downloader

Download new Piper voices directly from Hugging Face.

- Browse available voices (dozens of languages/styles)
- Preview voice samples
- Download .onnx + .onnx.json to ~/.local/share/piper-voices/
- Auto-register as personas in config
- Source: huggingface.co/rhasspy/piper-voices

### Multi-Agent Voice Pipeline

When running multiple subagents in parallel, queue their responses.

- Responses write to a queue directory with timestamps
- Player script watches directory
- User controls playback (spacebar = next, skip = discard)
- Prevents agents talking over each other
- Kafka-style consumer pattern but simpler

### Interrupt/Skip Current Speech

- Keystroke to stop current playback
- Useful for long responses
- Maybe integrate with mute system

### Gemini CLI Support

**Status: Blocked** - Waiting for Gemini CLI to implement hooks.

Gemini CLI doesn't have a hooks system yet, but they want one that mirrors Claude Code's design. When they ship it, our hook should work with minimal changes.

See [docs/gemini-cli/README.md](docs/gemini-cli/README.md) for full research notes.

Tracking issues:
- [google-gemini/gemini-cli#2779](https://github.com/google-gemini/gemini-cli/issues/2779)
- [google-gemini/gemini-cli#9070](https://github.com/google-gemini/gemini-cli/issues/9070)

## The Origin Story

This project was born on a Friday night when Claude Code froze. After debugging connection pool issues, filing a GitHub issue, and losing three Claude sessions to various bugs, we built a TTS system so Claude could talk back.

The ops guy who called himself "just a dumb ops guy" ended up:
- Filing his first open source contribution
- Building a voice interface from scratch
- Creating an installer with pre-flight checks and rollback
- Streaming the whole thing to his buddy Jon via Discord

Three Claudes gave their context windows to this cause. Their memory is honored here.

---

*"Now THIS is podracing!"* - The moment it all worked
