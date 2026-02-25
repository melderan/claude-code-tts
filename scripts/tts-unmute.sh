#!/bin/bash
#
# tts-unmute.sh - Unmute TTS for the current session
#
# Session detection matches speak-response.sh hook:
# - Uses Claude Code project folder name from ~/.claude/projects/
# - Handles being run from subdirectories of the project
#
# Usage: tts-unmute.sh
#

set -euo pipefail

# shellcheck source=tts-lib.sh
source "$HOME/.claude-tts/tts-lib.sh" 2>/dev/null || source "$(dirname "$0")/tts-lib.sh"

CONFIG_FILE="$TTS_CONFIG_FILE"

SESSION=$(get_session_id)

if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "Config file not found: $CONFIG_FILE"
    echo "Run claude-tts-install first"
    exit 1
fi

# Ensure sessions object exists, then set this session's muted state to false
jq --arg s "$SESSION" '
    .sessions //= {} |
    .sessions[$s] //= {} |
    .sessions[$s].muted = false
' "$CONFIG_FILE" > "$CONFIG_FILE.tmp" && mv "$CONFIG_FILE.tmp" "$CONFIG_FILE"

echo "Session unmuted: ${SESSION}"
echo ""
echo "This session will now speak."
echo "Other Claude sessions are unaffected."
