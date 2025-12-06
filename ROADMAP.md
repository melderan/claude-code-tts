# Claude Code TTS - Roadmap

Ideas captured from the late night session that started it all.

## Current State (v1.0)

- Single voice TTS via Piper (en_US-hfc_male-medium)
- Stop hook triggers after each response
- Smart filtering skips tool_use blocks, finds actual text
- /mute and /unmute slash commands
- Python installer with pre-flight checks and backup system

## Future Ideas

### Voice Identity Per Session

Give each Claude session or subagent its own voice personality.

- Code reviewer: deeper, authoritative voice
- Test writer: faster, energetic voice
- Architect agent: slower, thoughtful voice
- Main session: current HFC male medium

Implementation thoughts:
- Hook could detect session context or agent type
- Environment variable per terminal/session
- Voice config file that maps agent types to voice models

### Multi-Agent Voice Pipeline

When running multiple subagents in parallel, queue their responses.

- Responses write to a queue directory with timestamps
- Player script watches directory
- User controls playback (spacebar = next, skip = discard)
- Prevents agents talking over each other
- Kafka-style consumer pattern but simpler

### Voice Selection Command

Add `/voice` slash command to switch voices on the fly.

- List available voices
- Preview voices
- Set default voice
- Per-session override

### Linux Support

Current installer is macOS only.

- Replace `afplay` with `paplay` (PulseAudio) or `aplay` (ALSA)
- Test on common distros
- Handle audio system differences

### Interrupt/Skip Current Speech

- Keystroke to stop current playback
- Useful for long responses
- Maybe integrate with mute system

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
