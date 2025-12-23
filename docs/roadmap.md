# Claude Code TTS Roadmap

Ideas for future development. Not prioritized - just a brainstorm list.

## From Today's Session

### --check output clarity
The `--check` command shows "current" for all files but can show a version mismatch (e.g., "Installed: 5.8.0, Repo: 5.8.1"). This is confusing because if files are current, the versions should match. Options:
- If all files match, report installed = repo version
- Don't show version comparison in --check (it's about file contents, not versions)
- Update config.json version during --check if all files match

## Pause/Resume Enhancements

### Skip current message
Hotkey to skip the currently playing message and move to next in queue. Useful when Claude starts a long response you don't need to hear.

### Pause indicator in prompt
Show a visual indicator in the terminal when TTS is paused (like `[PAUSED]` in the status line).

## Queue Management

### Queue preview
Command to see what's queued without playing: `/tts-queue` to list pending messages.

### Priority messages
Allow certain sessions/projects to have priority in the queue (e.g., urgent notifications jump ahead).

### Clear queue
`/tts-clear` to drop all pending messages.

## Voice & Audio

### Per-project voices
Automatically use different voices for different repos (extend the /tts-discover concept).

### Voice preview in persona selection
When switching personas, play a short sample so you know what it sounds like.

### Volume control
Adjust TTS volume independent of system volume.

## Cross-Platform

### Linux improvements
Test and document Linux setup (paplay, systemd, etc.)

### WSL support
Ensure everything works in Windows Subsystem for Linux.

## Developer Experience

### Installer dry-run
`--dry-run` flag to show what would be installed without making changes.

### Uninstall command
Clean uninstall that removes all TTS files and settings.

### Health check command
`/tts-health` to verify piper, voices, daemon, hooks all working.

## Integration

### Notification sounds library
Expand sound effects beyond just the chime - different sounds for different events.

### Integration with system notifications
Option to show macOS notifications for TTS events (beyond just pause/resume).

---

*Add ideas here as they come up. Check off or remove when implemented.*
