#!/bin/bash
#
# tts-pause.sh - Toggle TTS playback pause/resume
#
# Designed to be invoked from a system hotkey (Raycast, Alfred, Shortcuts, etc.)
# NOT a Claude Code command - works independently while Claude is speaking.
#
# Usage: tts-pause.sh
#

set -euo pipefail

# Ensure homebrew binaries are in PATH (needed for jq when called from Shortcuts)
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

PLAYBACK_FILE="$HOME/.claude-tts/playback.json"

# macOS notification helper
notify() {
    local title="$1"
    local message="$2"
    osascript -e "display notification \"$message\" with title \"$title\"" 2>/dev/null || true
}

# Read current state
read_state() {
    if [[ -f "$PLAYBACK_FILE" ]]; then
        cat "$PLAYBACK_FILE"
    else
        echo '{"paused":false,"audio_pid":null}'
    fi
}

# Get current pause state
is_paused() {
    local state
    state=$(read_state)
    [[ $(echo "$state" | jq -r '.paused // false') == "true" ]]
}

# Get current audio PID
get_audio_pid() {
    local state
    state=$(read_state)
    echo "$state" | jq -r '.audio_pid // empty'
}

# Write new pause state
write_paused() {
    local paused="$1"
    local state
    state=$(read_state)
    echo "$state" | jq --argjson p "$paused" '.paused = $p | .updated_at = now' > "$PLAYBACK_FILE.tmp"
    sync  # Ensure written to disk
    mv "$PLAYBACK_FILE.tmp" "$PLAYBACK_FILE"
    sync  # Ensure rename is persisted
}

# Main toggle logic
main() {
    local current_pid
    current_pid=$(get_audio_pid)

    if is_paused; then
        # Currently paused -> Resume
        write_paused false
        notify "Claude TTS" "Playback resumed"
        echo "Resumed"
    else
        # Currently playing -> Pause
        write_paused true

        # Kill the audio process so daemon detects the pause
        # The message will be saved and replayed on resume
        if [[ -n "$current_pid" ]] && kill -0 "$current_pid" 2>/dev/null; then
            kill -TERM "$current_pid" 2>/dev/null || true
        fi

        notify "Claude TTS" "Playback paused"
        echo "Paused"
    fi
}

main "$@"
