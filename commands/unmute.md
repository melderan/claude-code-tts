Unmute the text-to-speech voice output. Run this command:

```bash
if [[ -f ~/.claude-tts/config.json ]]; then
    jq '.muted = false' ~/.claude-tts/config.json > ~/.claude-tts/config.json.tmp && mv ~/.claude-tts/config.json.tmp ~/.claude-tts/config.json
else
    rm -f /tmp/claude_tts_muted
fi
```

Then confirm to the user: "TTS unmuted. Voice output is now enabled."
