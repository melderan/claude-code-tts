# Claude Code TTS - Roadmap

Ideas captured from the late night sessions that started it all.

## Current State (v5.8.4)

- Piper TTS with configurable voice models
- Stop hook triggers after each response
- Smart filtering skips tool_use blocks, finds actual text
- /tts-mute and /tts-unmute slash commands (session-aware)
- /tts-persona command for switching voice configurations
- /tts-speed command for runtime speed adjustment
- /tts-status and /tts-cleanup commands
- Session-local settings (each Claude window can have own persona/mute state)
- Persona system with speed, voice, speed_method, and ai_type configs
- Project personas for sticky per-repo voice defaults
- Queue mode with daemon for parallel agent support
- Cross-platform: macOS (afplay), Linux (paplay/aplay), WSL 2 (WSLg)
- Python installer with interactive mode, persona management, pre-flight checks
- Voice downloader fetches models from Hugging Face
- Version tracking with --version and --check flags
- Default muted: new sessions silent until /tts-unmute

## Completed

### Pause/Resume Toggle (DONE)

System-level pause/resume for TTS playback via hotkey.

- Kill audio on pause, save interrupted message
- Replay from beginning on resume
- Lock file prevents duplicate daemons (`--lockpick` for recovery)
- macOS notifications on pause/resume
- Pause state shown in `/tts-status`

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

### --check Output Clarity

The `--check` command shows "current" for all files but can show a version mismatch (e.g., "Installed: 5.8.0, Repo: 5.8.1"). This is confusing because if files are current, the versions should match. Options:
- If all files match, report installed = repo version
- Don't show version comparison in --check (it's about file contents, not versions)
- Update config.json version during --check if all files match

### Pause/Resume Enhancements

**Skip current message**: Hotkey to skip the currently playing message and move to next in queue. Useful when Claude starts a long response you don't need to hear.

**Pause indicator in prompt**: Show a visual indicator in the terminal when TTS is paused (like `[PAUSED]` in the status line).

### Queue Management

**Queue preview**: Command to see what's queued without playing: `/tts-queue` to list pending messages.

**Priority messages**: Allow certain sessions/projects to have priority in the queue (e.g., urgent notifications jump ahead).

**Clear queue**: `/tts-clear` to drop all pending messages.

### Volume Control

Adjust TTS volume independent of system volume.

### Health Check Command

`/tts-health` to verify piper, voices, daemon, hooks all working.

### Installer Improvements

**Dry-run mode**: `--dry-run` flag to show what would be installed without making changes.

**Uninstall command**: Clean uninstall that removes all TTS files and settings.

### Standalone TTS Tools (tts-speak, tts-bench)

Tools to test and explore voices without burning Claude tokens.

**tts-speak.sh** - Low-level speech tool:
- `~/.claude-tts/tts-speak.sh "Hello world"` - speak with current settings
- `~/.claude-tts/tts-speak.sh --voice en_US-joe-medium --speed 2.0 "Hello"` - specify voice/speed
- Works completely independently of Claude Code

**tts-bench.sh** - Voice comparison tool:
- Run same text through multiple voice/speed/method combinations
- Play back-to-back for easy comparison
- Output timing and quality metrics
- Help find optimal settings for each voice

### Persona Builder TUI (tts-builder)

Interactive terminal UI for creating personas without Claude.

- Browse available voices with arrow keys
- Adjust speed in real-time, hear changes instantly
- Toggle between playback/length_scale methods
- Type custom test phrases
- Save as named persona when satisfied
- Built with gum/fzf or Python curses

### /tts-explore Command

Interactive voice exploration mode within Claude Code.

- List voices and let user pick (or "surprise me")
- Set temporarily without touching config
- Speak sample lines for evaluation
- User can say "faster", "slower", "try length_scale", "next voice"
- "keep it" saves as named persona
- Natural conversation-driven voice tuning

### /tts-discover Command

Auto-suggest personas based on repository context.

- Reads CLAUDE.md, README, package.json, pyproject.toml
- Understands project vibe (infrastructure, web app, CLI tool, etc.)
- Suggests appropriate voice/speed/method combination
- Feeds directly into /tts-explore for refinement
- Example: K8s operator gets steady, authoritative voice

### Hybrid Speed Method

Combine length_scale and playback for best of both worlds.

- Generate with moderate length_scale (e.g., 1.5x) to keep natural pitch
- Also apply playback speedup (e.g., 1.3x) for combined ~2x speed
- New config option: `"speed_method": "hybrid"`
- Separate controls: `"length_scale": 1.5, "playback_boost": 1.3`
- Gets faster speed without the chipmunk effect

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

### Universal AI CLI TTS Library

**Status: Ideation** - The spider tingle has spoken.

The current implementation is tightly coupled to Claude Code's hook system and transcript format. As more AI CLI tools emerge (Gemini CLI, OpenAI Codex, Mistral's Devstral CLI), each with their own plugin/hook systems, we need a portable foundation.

**Vision:** A Python library that any AI CLI can integrate with minimal glue code.

```python
from ai_tts import speak, config

# Any CLI tool just calls this
speak("Hello from any AI")

# Or with full context
speak(text, persona="claude-connery", session="project-foo")
```

**Architecture:**
- Core library handles: config management, session detection, persona resolution, TTS invocation, queue/daemon coordination
- Thin adapter layer per CLI tool: hook into response events, extract text, call library
- Bash scripts become optional convenience wrappers, not the source of truth
- Single codebase, multiple CLI integrations

**Why this matters:**
- JMO wants his AI friends to be friends with each other
- Rewriting session detection and config logic for each CLI is wasteful
- Unified experience across Claude, Gemini, Codex, Devstral, whatever comes next
- Opens the door for cross-CLI features (same persona across all tools, unified queue)

**Implications:**
- Repo rename from `claude-code-tts` to something universal (naming is hard)
- Python library as the core, with CLI-specific adapters
- Maintain backward compatibility for existing Claude Code users

**Open questions:**
- Package name? `ai-tts`, `cli-tts`, `piper-cli`, `voice-loop`?
- Monorepo with adapters, or separate adapter packages?
- How to handle CLI-specific features (Claude's transcript format vs others)?

This is the long-term direction. The current bash scripts work fine for now.

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
