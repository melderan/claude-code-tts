# Notification Sounds

Place custom notification sound files here. Supported formats: `.wav`, `.mp3`, `.aiff`

## Sound Events

| Event | Default | Description |
|-------|---------|-------------|
| `thinking` | system beep | Claude starts processing |
| `ready` | none | Response complete, TTS starting |
| `error` | system alert | An error occurred |
| `muted` | soft beep | TTS was muted |
| `unmuted` | soft beep | TTS was unmuted |

## Configuration

In `~/.claude-tts/config.json`:

```json
{
  "sounds": {
    "enabled": true,
    "volume": 0.5,
    "events": {
      "thinking": "thinking.wav",
      "ready": null,
      "error": "error.wav",
      "muted": "beep",
      "unmuted": "beep"
    }
  }
}
```

## Special Values

- `null` or missing: No sound for this event
- `"beep"`: System beep (no file needed)
- `"alert"`: System alert sound
- `"filename.wav"`: Custom sound file from this directory

## Adding Custom Sounds

1. Place your sound file in `~/.claude-tts/sounds/` or this repo's `sounds/` directory
2. Update config to reference the filename
3. Keep sounds short (< 1 second) for best UX

## macOS System Sounds

On macOS, you can use built-in sounds by name:
- Basso, Blow, Bottle, Frog, Funk, Glass, Hero, Morse, Ping, Pop, Purr, Sosumi, Submarine, Tink
