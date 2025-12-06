Mute the text-to-speech voice output for this session.

Sessions are auto-detected from the project. Run this command:

```bash
CONFIG_FILE="$HOME/.claude-tts/config.json"

# Auto-detect session from working directory (matches hook behavior)
if [[ -z "${CLAUDE_TTS_SESSION:-}" ]]; then
    # Create a hash-like session ID from PWD
    SESSION=$(echo -n "$PWD" | md5sum 2>/dev/null | cut -c1-16 || echo -n "$PWD" | md5 | cut -c1-16)
else
    SESSION="$CLAUDE_TTS_SESSION"
fi

if [[ -f "$CONFIG_FILE" ]]; then
    # Ensure sessions object exists and set this session's muted state
    jq --arg s "$SESSION" '.sessions[$s].muted = true' "$CONFIG_FILE" > "$CONFIG_FILE.tmp" && mv "$CONFIG_FILE.tmp" "$CONFIG_FILE"
    echo "Session muted (ID: ${SESSION:0:8}...)"
else
    touch /tmp/claude_tts_muted
    echo "TTS muted (legacy mode)"
fi
```

Confirm to the user that this specific session is now muted. Other Claude sessions will continue to speak.
