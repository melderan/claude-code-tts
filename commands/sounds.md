Toggle or configure notification sounds.

Check if the user provided an argument. The argument will be appended to this prompt.

**If no argument:** Show current sound settings:

```bash
CONFIG_FILE="$HOME/.claude-tts/config.json"

if [[ -f "$CONFIG_FILE" ]]; then
    ENABLED=$(jq -r '.sounds.enabled // false' "$CONFIG_FILE")
    VOLUME=$(jq -r '.sounds.volume // 0.5' "$CONFIG_FILE")

    if [[ "$ENABLED" == "true" ]]; then
        echo "Sounds: ENABLED (volume: ${VOLUME})"
    else
        echo "Sounds: DISABLED"
    fi
    echo ""
    echo "Event sounds:"
    jq -r '.sounds.events | to_entries[] | "  \(.key): \(.value // "none")"' "$CONFIG_FILE"
    echo ""
    echo "Commands:"
    echo "  /sounds on    - Enable notification sounds"
    echo "  /sounds off   - Disable notification sounds"
    echo "  /sounds test  - Play a test beep"
else
    echo "Sounds: DISABLED (no config file)"
fi
```

**If argument is "on":** Enable sounds:

```bash
CONFIG_FILE="$HOME/.claude-tts/config.json"

if [[ -f "$CONFIG_FILE" ]]; then
    jq '.sounds.enabled = true' "$CONFIG_FILE" > "$CONFIG_FILE.tmp" && mv "$CONFIG_FILE.tmp" "$CONFIG_FILE"
    echo "Notification sounds enabled"

    # Play a confirmation beep
    PLAY_SOUND="$HOME/.claude/hooks/play-sound.sh"
    [[ -x "$PLAY_SOUND" ]] && "$PLAY_SOUND" unmuted &
else
    echo "No config file found at $CONFIG_FILE"
fi
```

**If argument is "off":** Disable sounds:

```bash
CONFIG_FILE="$HOME/.claude-tts/config.json"

if [[ -f "$CONFIG_FILE" ]]; then
    jq '.sounds.enabled = false' "$CONFIG_FILE" > "$CONFIG_FILE.tmp" && mv "$CONFIG_FILE.tmp" "$CONFIG_FILE"
    echo "Notification sounds disabled"
else
    echo "No config file found at $CONFIG_FILE"
fi
```

**If argument is "test":** Play a test sound:

```bash
PLAY_SOUND="$HOME/.claude/hooks/play-sound.sh"

if [[ -x "$PLAY_SOUND" ]]; then
    # Temporarily enable sounds for test
    "$PLAY_SOUND" unmuted
    echo "Test sound played"
else
    echo "Sound player not found at $PLAY_SOUND"
fi
```

After running the appropriate command, summarize the result to the user.
