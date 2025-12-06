Manage TTS voice personas. This command handles persona switching for Claude Code TTS.

First, check if the user provided an argument. The argument will be appended to this prompt.

**If no argument or argument is "list":** Show available personas by running:

```bash
if [[ -f ~/.claude-tts/config.json ]]; then
    echo "Available personas:"
    jq -r '.personas | to_entries[] | "  - \(.key): \(.value.description // "No description")"' ~/.claude-tts/config.json
    echo ""
    echo "Active persona: $(jq -r '.active_persona' ~/.claude-tts/config.json)"
else
    echo "No config file found at ~/.claude-tts/config.json"
fi
```

**If argument is a persona name:** Switch to that persona by running:

```bash
PERSONA_NAME="<the argument>"
if [[ -f ~/.claude-tts/config.json ]]; then
    if jq -e ".personas[\"$PERSONA_NAME\"]" ~/.claude-tts/config.json > /dev/null 2>&1; then
        jq ".active_persona = \"$PERSONA_NAME\"" ~/.claude-tts/config.json > ~/.claude-tts/config.json.tmp && mv ~/.claude-tts/config.json.tmp ~/.claude-tts/config.json
        echo "Switched to persona: $PERSONA_NAME"
    else
        echo "Persona '$PERSONA_NAME' not found. Use '/persona list' to see available personas."
    fi
else
    echo "No config file found at ~/.claude-tts/config.json"
fi
```

After running the appropriate command, summarize the result to the user.
