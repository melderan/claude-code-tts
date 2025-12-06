#!/bin/bash
#
# play-sound.sh - Play notification sounds
#
# Usage: play-sound.sh <event_name>
# Events: thinking, ready, error, muted, unmuted
#
# Checks config for sound settings, plays appropriate sound.
#

set -euo pipefail

EVENT="${1:-}"
if [[ -z "$EVENT" ]]; then
    echo "Usage: play-sound.sh <event>" >&2
    exit 1
fi

CONFIG_FILE="$HOME/.claude-tts/config.json"
SOUNDS_DIR="$HOME/.claude-tts/sounds"
REPO_SOUNDS_DIR="$(dirname "$0")/../sounds"

# Check if sounds are enabled
if [[ -f "$CONFIG_FILE" ]]; then
    SOUNDS_ENABLED=$(jq -r '.sounds.enabled // true' "$CONFIG_FILE" 2>/dev/null)
    if [[ "$SOUNDS_ENABLED" == "false" ]]; then
        exit 0
    fi

    VOLUME=$(jq -r '.sounds.volume // 0.5' "$CONFIG_FILE" 2>/dev/null)
    SOUND_FILE=$(jq -r ".sounds.events[\"$EVENT\"] // empty" "$CONFIG_FILE" 2>/dev/null)
else
    VOLUME="0.5"
    SOUND_FILE=""
fi

# If no specific sound configured, use defaults
if [[ -z "$SOUND_FILE" || "$SOUND_FILE" == "null" ]]; then
    case "$EVENT" in
        thinking) SOUND_FILE="beep" ;;
        error)    SOUND_FILE="alert" ;;
        muted|unmuted) SOUND_FILE="beep" ;;
        *)        exit 0 ;;  # No sound for unconfigured events
    esac
fi

# --- Play the sound ---

play_beep() {
    if [[ "$(uname)" == "Darwin" ]]; then
        # macOS: Use afplay with Tink sound or printf beep
        if [[ -f "/System/Library/Sounds/Tink.aiff" ]]; then
            afplay -v "$VOLUME" "/System/Library/Sounds/Tink.aiff" &
        else
            printf '\a'
        fi
    else
        # Linux: Use paplay or simple beep
        if command -v paplay &>/dev/null; then
            # Try to find a system sound
            for sound in /usr/share/sounds/freedesktop/stereo/bell.oga \
                         /usr/share/sounds/sound-icons/prompt.wav; do
                if [[ -f "$sound" ]]; then
                    paplay "$sound" &
                    return
                fi
            done
        fi
        printf '\a'
    fi
}

play_alert() {
    if [[ "$(uname)" == "Darwin" ]]; then
        if [[ -f "/System/Library/Sounds/Basso.aiff" ]]; then
            afplay -v "$VOLUME" "/System/Library/Sounds/Basso.aiff" &
        else
            printf '\a'
        fi
    else
        if command -v paplay &>/dev/null; then
            for sound in /usr/share/sounds/freedesktop/stereo/dialog-error.oga \
                         /usr/share/sounds/sound-icons/error.wav; do
                if [[ -f "$sound" ]]; then
                    paplay "$sound" &
                    return
                fi
            done
        fi
        printf '\a'
    fi
}

play_file() {
    local file="$1"

    # Find the file in sounds directories
    local filepath=""
    if [[ -f "$SOUNDS_DIR/$file" ]]; then
        filepath="$SOUNDS_DIR/$file"
    elif [[ -f "$REPO_SOUNDS_DIR/$file" ]]; then
        filepath="$REPO_SOUNDS_DIR/$file"
    elif [[ -f "$file" ]]; then
        filepath="$file"
    fi

    if [[ -z "$filepath" ]]; then
        # File not found, fall back to beep
        play_beep
        return
    fi

    if [[ "$(uname)" == "Darwin" ]]; then
        afplay -v "$VOLUME" "$filepath" &
    elif command -v paplay &>/dev/null; then
        paplay "$filepath" &
    elif command -v aplay &>/dev/null; then
        aplay -q "$filepath" &
    fi
}

# Handle the sound
case "$SOUND_FILE" in
    beep)  play_beep ;;
    alert) play_alert ;;
    *)     play_file "$SOUND_FILE" ;;
esac

exit 0
