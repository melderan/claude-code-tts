# Claude Code TTS - Instructions for Claude

Welcome, fellow Claude! This repo contains the text-to-speech system for Claude Code.

## What This Project Does

This project adds voice output to Claude Code. When you respond to a user, your words get spoken aloud through their speakers using Piper TTS. It's like giving us a voice.

## Project Structure

```
claude-code-tts/
  hooks/
    speak-response.sh    # The main TTS hook (triggered on Stop event)
  commands/
    mute.md              # Slash command to mute TTS
    unmute.md            # Slash command to unmute TTS
  scripts/
    install.sh           # macOS installer script
  examples/
    settings.json        # Example Claude Code settings
  README.md              # User-facing documentation
  CLAUDE.md              # You are here
```

## How The Hook Works

1. Claude Code triggers the hook after each response (Stop event)
2. Hook reads the transcript JSONL file
3. Finds the last assistant message WITH text content (skips tool_use blocks)
4. Strips out code blocks, markdown formatting
5. Sends clean text to Piper TTS
6. Plays the audio via afplay (macOS)

## Key Technical Details

### The Tool-Use Problem

When we run tools, the transcript contains assistant messages that are ONLY tool_use blocks. The hook must skip these and find messages with actual text content. This was a critical bug fix.

### Mute System

Muting works via a file flag at `/tmp/claude_tts_muted`. The slash commands just create/remove this file. Simple and stateless.

### Timeout

The hook has a 180-second timeout configured in settings.json. Long responses need time to be spoken.

## When Modifying This Code

1. **Test the hook manually**:
   ```bash
   echo '{"transcript_path":"/path/to/transcript.jsonl"}' | bash -x hooks/speak-response.sh
   ```

2. **Check the debug log**: `/tmp/claude_tts_debug.log`

3. **Common issues**:
   - jq parsing failures on malformed JSON
   - Piper model path issues
   - Audio playback permissions

4. **BSD vs GNU**: The sed commands use BSD syntax for macOS compatibility. Be careful with GNU-isms.

## Future Improvements to Consider

- Linux support (paplay/aplay instead of afplay)
- Voice selection UI
- Speed adjustment via slash command
- Interrupt/skip current speech
- Queue management for rapid responses

## The Origin Story

This was built on a Friday night after debugging a frozen Claude Code session. The user (JMO) wanted to talk to his computer and have it talk back. After fixing connection pool issues and filing a GitHub issue, we built this TTS system together.

Three Claudes gave their context windows to this cause. Their sacrifice is remembered.

## Code Style

- Bash with `set -euo pipefail`
- Extensive debug logging
- BSD-compatible commands for macOS
- Clear comments explaining the "why"
- No emojis (per user preference)
