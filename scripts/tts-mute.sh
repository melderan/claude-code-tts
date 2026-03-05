#!/bin/bash
#
# tts-mute.sh - Mute TTS for the current session (or all sessions)
#
# Session detection matches speak-response.sh hook:
# - Uses Claude Code project folder name from ~/.claude/projects/
# - Handles being run from subdirectories of the project
#
# Usage: tts-mute.sh [--all]
#   --all   Mute every session (the "get out of jail free" flag)
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

# --- --all flag: mute every session and set global default ---
if [[ "${1:-}" == "--all" ]]; then
    # Set global mute flags
    jq '.default_muted = true | .muted = true' "$CONFIG_FILE" > "$CONFIG_FILE.tmp" \
        && mv "$CONFIG_FILE.tmp" "$CONFIG_FILE"

    # Mute all existing session files
    COUNT=0
    if [[ -d "$TTS_SESSIONS_DIR" ]]; then
        for sf in "$TTS_SESSIONS_DIR"/*.json; do
            [[ -f "$sf" ]] || continue
            jq '.muted = true' "$sf" > "$sf.tmp" && mv "$sf.tmp" "$sf"
            COUNT=$((COUNT + 1))
        done
    fi

    echo "All sessions muted ($COUNT session files + global default)."
    echo ""
    echo "Every Claude session is now silent."
    echo ""
    echo "Use /tts-unmute to restore voice for a specific session."
    exit 0
fi

SESSION=$(get_session_id)

tts_session_set "$SESSION" "muted" "true" "bool"

echo "Session muted: ${SESSION}"
echo ""
echo "This session will no longer speak."
echo "Other Claude sessions are unaffected."
echo ""
echo "Use /tts-unmute to restore voice."
