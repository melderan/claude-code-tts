Mute the text-to-speech voice output. Run this command:

```bash
if [[ -f ~/.claude-tts/config.json ]]; then
    jq '.muted = true' ~/.claude-tts/config.json > ~/.claude-tts/config.json.tmp && mv ~/.claude-tts/config.json.tmp ~/.claude-tts/config.json
else
    touch /tmp/claude_tts_muted
fi
```

Then confirm to the user: "TTS muted. Use /unmute to re-enable voice output."
