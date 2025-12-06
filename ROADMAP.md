# Claude Code TTS - Roadmap

Ideas captured from the late night sessions that started it all.

## Current State (v1.1.0)

- Piper TTS with configurable voice models
- Stop hook triggers after each response
- Smart filtering skips tool_use blocks, finds actual text
- /mute and /unmute slash commands (session-aware)
- /persona command for switching voice configurations
- Session-local settings (each Claude window can have own persona/mute state)
- Persona system with speed, voice, speed_method, and ai_type configs
- Cross-platform: macOS (afplay), Linux (paplay/aplay), WSL 2 (WSLg)
- Python installer with interactive mode, persona management, pre-flight checks
- Voice downloader fetches models from Hugging Face
- Version tracking with --version and --check flags
- 22 unit tests, uv compatible

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

### Voice Model Browser/Downloader (DONE)

Download new Piper voices directly from Hugging Face.

- Curated list of 14 quality English voices (male/female, US/GB)
- Filter by gender or installation status
- Download .onnx + .onnx.json to ~/.local/share/piper-voices/
- Auto-register as personas with ai_type (claude/gemini)
- Bootstrap mode: --bootstrap config.json

### Version Tracking (DONE)

- --version shows repo and installed versions
- --check compares file hashes, shows update status
- Version recorded in config on install/upgrade

## Future Ideas

### Voice Preview

Test voices before downloading.

- `python install.py --preview amy` plays sample audio
- Fetch sample clips from Hugging Face or generate on-the-fly
- Add preview option to interactive voice downloader

### /speed Command

Change speech speed on the fly without editing config.

- `/speed 1.5` - slow down
- `/speed 2.5` - speed up
- `/speed reset` - back to persona default
- Session-local, doesn't persist

### /voices Command

List installed voices and switch without leaving Claude.

- `/voices` - show installed voice models
- `/voices amy` - switch to amy voice
- Simpler than /persona for quick voice changes

### Subagent TTS Support

Make subagents speak too (currently only main agent).

**Technical Notes:**
- `SubagentStop` hook fires when subagents complete (separate from `Stop`)
- Subagent transcripts stored at `agent-{agentId}.jsonl`
- LIMITATION: SubagentStop doesn't identify WHICH subagent finished (GitHub #7881)
- Can enable subagent TTS, but can't easily give each a unique voice

Implementation:
- Add SubagentStop hook to speak-response.sh
- Option to enable/disable subagent speech in config
- Default: off (to avoid noise from parallel agents)

### Multi-Agent Voice Pipeline

When running multiple subagents in parallel, queue their responses.

- Responses write to a queue directory with timestamps
- Player script watches directory
- User controls playback (spacebar = next, skip = discard)
- Prevents agents talking over each other
- Kafka-style consumer pattern but simpler

**Blocked by:** SubagentStop not identifying which agent finished (#7881)

### Subagent Voice Mapping

Give different voices to different subagent types automatically.

- code-reviewer agent: deeper, authoritative voice
- test-runner agent: faster, energetic voice
- explore agent: thoughtful, measured voice

**Blocked by:** Can't identify subagent type from SubagentStop hook (#7881)

### Interrupt/Skip Current Speech

Keystroke to stop current playback mid-sentence.

- Global hotkey or terminal escape sequence
- Useful for long responses
- `/skip` command as alternative
- Maybe integrate with mute system

### Notification Sounds

Different audio cues for different events.

- Chime when Claude starts thinking
- Different sound for errors vs success
- Subtle audio feedback without full TTS
- Configurable per event type

### Random Voice Mode (Chaos Mode)

Every response is a different voice.

- `/chaos on` - enable random voice per response
- `/chaos off` - back to normal
- Fun for demos, probably annoying for real work
- Could weight by ai_type (Claude voices only)

### Accent Roulette

Randomly pick accents (British, Scottish, American).

- Subset of chaos mode
- Only varies accent, not completely random voice
- `/accent random` to enable

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
