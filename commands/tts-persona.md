Manage TTS voice personas. This command is session-aware with auto-detection.

First, check if the user provided an argument. The argument will be appended to this prompt.

**If no argument or argument is "list":** Show available personas and session info:

```bash
CONFIG_FILE="$HOME/.claude-tts/config.json"

# Auto-detect session from working directory (matches hook behavior)
if [[ -z "${CLAUDE_TTS_SESSION:-}" ]]; then
    SESSION=$(echo -n "$PWD" | md5sum 2>/dev/null | cut -c1-16 || echo -n "$PWD" | md5 | cut -c1-16)
else
    SESSION="$CLAUDE_TTS_SESSION"
fi

if [[ -f "$CONFIG_FILE" ]]; then
    echo "Available personas:"
    jq -r '.personas | to_entries[] | "  - \(.key): \(.value.description // "No description")"' "$CONFIG_FILE"
    echo ""
    GLOBAL_PERSONA=$(jq -r '.active_persona' "$CONFIG_FILE")
    echo "Global persona: $GLOBAL_PERSONA"

    SESSION_PERSONA=$(jq -r ".sessions[\"$SESSION\"].persona // empty" "$CONFIG_FILE")
    if [[ -n "$SESSION_PERSONA" && "$SESSION_PERSONA" != "null" ]]; then
        echo "This session (${SESSION:0:8}...): $SESSION_PERSONA"
    else
        echo "This session (${SESSION:0:8}...): using global persona"
    fi
else
    echo "No config file found at $CONFIG_FILE"
fi
```

**If argument is a persona name:** Switch to that persona for this session:

```bash
CONFIG_FILE="$HOME/.claude-tts/config.json"
PERSONA_NAME="<the argument>"

# Auto-detect session from working directory (matches hook behavior)
if [[ -z "${CLAUDE_TTS_SESSION:-}" ]]; then
    SESSION=$(echo -n "$PWD" | md5sum 2>/dev/null | cut -c1-16 || echo -n "$PWD" | md5 | cut -c1-16)
else
    SESSION="$CLAUDE_TTS_SESSION"
fi

if [[ -f "$CONFIG_FILE" ]]; then
    if jq -e ".personas[\"$PERSONA_NAME\"]" "$CONFIG_FILE" > /dev/null 2>&1; then
        # Session-specific persona
        jq --arg s "$SESSION" --arg p "$PERSONA_NAME" '.sessions[$s].persona = $p' "$CONFIG_FILE" > "$CONFIG_FILE.tmp" && mv "$CONFIG_FILE.tmp" "$CONFIG_FILE"
        echo "Session (${SESSION:0:8}...) now using persona: $PERSONA_NAME"
    else
        echo "Persona '$PERSONA_NAME' not found. Use '/persona list' to see available personas."
    fi
else
    echo "No config file found at $CONFIG_FILE"
fi
```

After running the appropriate command, summarize the result to the user. Remind them that persona changes are session-specific - other Claude windows will keep their current persona.
