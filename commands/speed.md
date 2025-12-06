Adjust TTS speech speed for this session.

Check if the user provided an argument. The argument will be appended to this prompt.

**If no argument:** Show current speed and usage:

```bash
CONFIG_FILE="$HOME/.claude-tts/config.json"

# Auto-detect session from working directory
if [[ -z "${CLAUDE_TTS_SESSION:-}" ]]; then
    SESSION=$(echo -n "$PWD" | md5sum 2>/dev/null | cut -c1-16 || echo -n "$PWD" | md5 | cut -c1-16)
else
    SESSION="$CLAUDE_TTS_SESSION"
fi

if [[ -f "$CONFIG_FILE" ]]; then
    # Get persona speed as default
    ACTIVE_PERSONA=$(jq -r '.active_persona // "default"' "$CONFIG_FILE")
    SESSION_PERSONA=$(jq -r ".sessions[\"$SESSION\"].persona // empty" "$CONFIG_FILE")
    [[ -n "$SESSION_PERSONA" && "$SESSION_PERSONA" != "null" ]] && ACTIVE_PERSONA="$SESSION_PERSONA"

    PERSONA_SPEED=$(jq -r ".personas[\"$ACTIVE_PERSONA\"].speed // 2.0" "$CONFIG_FILE")
    SESSION_SPEED=$(jq -r ".sessions[\"$SESSION\"].speed // empty" "$CONFIG_FILE")

    if [[ -n "$SESSION_SPEED" && "$SESSION_SPEED" != "null" ]]; then
        echo "Current speed: ${SESSION_SPEED}x (session override)"
        echo "Persona default: ${PERSONA_SPEED}x"
    else
        echo "Current speed: ${PERSONA_SPEED}x (from persona: $ACTIVE_PERSONA)"
    fi
    echo ""
    echo "Usage:"
    echo "  /speed 1.5   - Slow down"
    echo "  /speed 2.5   - Speed up"
    echo "  /speed reset - Use persona default"
else
    echo "No config file found. Using default speed: 2.0x"
fi
```

**If argument is "reset":** Clear session speed override:

```bash
CONFIG_FILE="$HOME/.claude-tts/config.json"

if [[ -z "${CLAUDE_TTS_SESSION:-}" ]]; then
    SESSION=$(echo -n "$PWD" | md5sum 2>/dev/null | cut -c1-16 || echo -n "$PWD" | md5 | cut -c1-16)
else
    SESSION="$CLAUDE_TTS_SESSION"
fi

if [[ -f "$CONFIG_FILE" ]]; then
    # Remove session speed override
    jq --arg s "$SESSION" 'if .sessions[$s] then .sessions[$s] |= del(.speed) else . end' "$CONFIG_FILE" > "$CONFIG_FILE.tmp" && mv "$CONFIG_FILE.tmp" "$CONFIG_FILE"

    ACTIVE_PERSONA=$(jq -r '.active_persona // "default"' "$CONFIG_FILE")
    PERSONA_SPEED=$(jq -r ".personas[\"$ACTIVE_PERSONA\"].speed // 2.0" "$CONFIG_FILE")
    echo "Speed reset to persona default: ${PERSONA_SPEED}x"
fi
```

**If argument is a number:** Set session speed override:

```bash
CONFIG_FILE="$HOME/.claude-tts/config.json"
SPEED="<the argument>"

if [[ -z "${CLAUDE_TTS_SESSION:-}" ]]; then
    SESSION=$(echo -n "$PWD" | md5sum 2>/dev/null | cut -c1-16 || echo -n "$PWD" | md5 | cut -c1-16)
else
    SESSION="$CLAUDE_TTS_SESSION"
fi

# Validate speed is a reasonable number (0.5 to 4.0)
if ! echo "$SPEED" | grep -qE '^[0-9]+\.?[0-9]*$'; then
    echo "Invalid speed: $SPEED (must be a number like 1.5 or 2.0)"
    exit 1
fi

# Check range
if (( $(echo "$SPEED < 0.5" | bc -l) )) || (( $(echo "$SPEED > 4.0" | bc -l) )); then
    echo "Speed out of range: $SPEED (must be between 0.5 and 4.0)"
    exit 1
fi

if [[ -f "$CONFIG_FILE" ]]; then
    jq --arg s "$SESSION" --argjson speed "$SPEED" '.sessions[$s].speed = $speed' "$CONFIG_FILE" > "$CONFIG_FILE.tmp" && mv "$CONFIG_FILE.tmp" "$CONFIG_FILE"
    echo "Speed set to ${SPEED}x for this session"
else
    echo "No config file found at $CONFIG_FILE"
fi
```

After running the appropriate command, summarize the result. Remind them that speed changes are session-specific and won't affect other Claude windows.
