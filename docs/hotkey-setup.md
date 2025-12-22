# Hotkey Setup for Pause/Resume

The pause/resume toggle script is designed to be called from a system-level hotkey, so you can pause Claude's speech while it's talking without needing to type a command.

## The Toggle Script

After installation, the script is located at:
```
$HOME/.claude-tts/tts-pause.sh
```

Running it toggles between paused and playing states:
- If audio is playing: pauses it (SIGSTOP)
- If audio is paused: resumes it (SIGCONT)

It also shows a macOS notification to confirm the action.

**Important:** Some apps (like macOS Shortcuts) don't expand `~`. Use the full path or `$HOME`:
- Full path: `/Users/yourusername/.claude-tts/tts-pause.sh`
- With $HOME: `$HOME/.claude-tts/tts-pause.sh`

## Setup Options

### macOS Shortcuts (Recommended)

Built-in, no additional software needed.

1. Open the **Shortcuts** app
2. Click **+** to create a new shortcut
3. Name it "Toggle TTS"
4. Search for "Run Shell Script" and add it
5. Set the script to: `$HOME/.claude-tts/tts-pause.sh`
   - Or use full path: `/Users/yourusername/.claude-tts/tts-pause.sh`
6. Right-click the shortcut in the sidebar > **Add Keyboard Shortcut**
7. Press your desired key combination (e.g., `Cmd+Shift+T`)

### Raycast

If you use Raycast:

1. Open Raycast preferences
2. Go to **Extensions** > **Script Commands**
3. Click **Add Directories** and add `~/.claude-tts/`
4. Find `tts-pause.sh` in the list
5. Assign a hotkey in the command settings

### Alfred

If you have Alfred Powerpack:

1. Open Alfred preferences
2. Go to **Workflows**
3. Create a new workflow
4. Add a **Hotkey** trigger
5. Connect it to a **Run Script** action
6. Set the script to: `/bin/bash ~/.claude-tts/tts-pause.sh`

### Hammerspoon

If you use Hammerspoon, add to your `init.lua`:

```lua
hs.hotkey.bind({"cmd", "shift"}, "t", function()
  hs.execute("~/.claude-tts/tts-pause.sh")
end)
```

### BetterTouchTool

1. Open BetterTouchTool
2. Go to **Keyboard** section
3. Add a new keyboard shortcut
4. Set action to **Execute Terminal Command**
5. Enter: `~/.claude-tts/tts-pause.sh`

## Checking Status

Use `/tts-status` in Claude Code to see the current pause state:

```
Session:  -Users-you-project
Muted:    false (session)
Paused:   true
Playing:  no
Persona:  claude-prime
Mode:     queue
Daemon:   running (PID 12345)
```

## How It Works

The pause feature uses Unix signals:
- **SIGSTOP** pauses the audio player process (afplay/paplay)
- **SIGCONT** resumes it

State is stored in `~/.claude-tts/playback.json`:
```json
{
  "paused": true,
  "audio_pid": 12345,
  "updated_at": 1234567890.123
}
```

While paused:
- Currently-playing audio is frozen mid-playback
- New messages continue to queue up
- When you resume, playback continues from where it stopped
- After the current audio finishes, queued messages play normally
