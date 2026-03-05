#!/bin/bash
#
# tts-intermediate.sh - Toggle intermediate speech (PostToolUse) for this session
#
# When disabled, only final responses (Stop hook) are spoken.
# Intermediate narration between tool calls is silenced.
#
# Usage: tts-intermediate.sh [on|off]
#   (no args)  Show current setting
#   on         Enable intermediate speech
#   off        Disable intermediate speech (final responses only)
#

set -euo pipefail

# shellcheck source=tts-lib.sh
source "$HOME/.claude-tts/tts-lib.sh" 2>/dev/null || source "$(dirname "$0")/tts-lib.sh"

CONFIG_FILE="$TTS_CONFIG_FILE"

if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "Config file not found: $CONFIG_FILE"
    echo "Run claude-tts-install first"
    exit 1
fi

SESSION=$(get_session_id)

case "${1:-}" in
    on)
        tts_session_set "$SESSION" "intermediate" "true" "bool"
        echo "Intermediate speech enabled for: ${SESSION}"
        echo ""
        echo "You will hear narration between tool calls."
        ;;
    off)
        tts_session_set "$SESSION" "intermediate" "false" "bool"
        echo "Intermediate speech disabled for: ${SESSION}"
        echo ""
        echo "Only final responses will be spoken."
        echo "Use /tts-intermediate on to restore."
        ;;
    *)
        local_session_file="$TTS_SESSIONS_DIR/${SESSION}.json"
        INTERMEDIATE="true"
        if [[ -f "$local_session_file" ]]; then
            val=$(jq -r '.intermediate // "true" | tostring' "$local_session_file" 2>/dev/null)
            [[ "$val" == "false" ]] && INTERMEDIATE="false"
        fi
        if [[ "$INTERMEDIATE" == "true" ]]; then
            echo "Intermediate speech: ENABLED (hearing all narration)"
        else
            echo "Intermediate speech: DISABLED (final responses only)"
        fi
        echo ""
        echo "Commands:"
        echo "  /tts-intermediate on   - Hear narration between tool calls"
        echo "  /tts-intermediate off  - Only hear final responses"
        ;;
esac
