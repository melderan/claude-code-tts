#!/bin/bash
#
# tts-sounds.sh - Toggle or configure notification sounds
#
# Usage: tts-sounds.sh [on|off|test]
#   (no args)  Show current sound settings
#   on         Enable notification sounds
#   off        Disable notification sounds
#   test       Play a test beep
#

set -euo pipefail

CONFIG_FILE="$HOME/.claude-tts/config.json"
PLAY_SOUND="$HOME/.claude/hooks/play-sound.sh"

if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "No config file found at $CONFIG_FILE"
    echo "Run claude-tts-install first"
    exit 1
fi

case "${1:-}" in
    on)
        jq '.sounds.enabled = true' "$CONFIG_FILE" > "$CONFIG_FILE.tmp" && mv "$CONFIG_FILE.tmp" "$CONFIG_FILE"
        echo "Notification sounds enabled"
        [[ -x "$PLAY_SOUND" ]] && "$PLAY_SOUND" unmuted &
        ;;
    off)
        jq '.sounds.enabled = false' "$CONFIG_FILE" > "$CONFIG_FILE.tmp" && mv "$CONFIG_FILE.tmp" "$CONFIG_FILE"
        echo "Notification sounds disabled"
        ;;
    test)
        if [[ -x "$PLAY_SOUND" ]]; then
            "$PLAY_SOUND" unmuted
            echo "Test sound played"
        else
            echo "Sound player not found at $PLAY_SOUND"
        fi
        ;;
    *)
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
        echo "  /tts-sounds on    - Enable notification sounds"
        echo "  /tts-sounds off   - Disable notification sounds"
        echo "  /tts-sounds test  - Play a test beep"
        ;;
esac
