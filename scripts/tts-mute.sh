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

CONFIG_FILE="$HOME/.claude-tts/config.json"
PROJECTS_DIR="$HOME/.claude/projects"

if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "Config file not found: $CONFIG_FILE"
    echo "Run claude-tts-install first"
    exit 1
fi

# --- --all flag: mute every session and set global default ---
if [[ "${1:-}" == "--all" ]]; then
    jq '
        .default_muted = true |
        .muted = true |
        (.sessions // {}) as $s |
        .sessions = ($s | to_entries | map(.value.muted = true) | from_entries)
    ' "$CONFIG_FILE" > "$CONFIG_FILE.tmp" && mv "$CONFIG_FILE.tmp" "$CONFIG_FILE"

    COUNT=$(jq '.sessions | length' "$CONFIG_FILE")
    echo "All sessions muted ($COUNT sessions + global default)."
    echo ""
    echo "Every Claude session is now silent."
    echo ""
    echo "Use /tts-unmute to restore voice for a specific session."
    exit 0
fi

# --- Session detection (must match speak-response.sh) ---
# Claude Code creates project folders like: ~/.claude/projects/-Users-foo-bar-project/
# The folder name is the path with / and _ replaced by -
# We need to find the LONGEST matching project that contains our current PWD
get_session_id() {
    # If explicitly set, use that
    if [[ -n "${CLAUDE_TTS_SESSION:-}" ]]; then
        echo "$CLAUDE_TTS_SESSION"
        return
    fi

    # Convert PWD to Claude Code format: /Users/foo/_bar -> -Users-foo--bar
    # Characters /, _, and . are replaced with -
    local pwd_transformed
    pwd_transformed=$(echo "$PWD" | tr '/_.' '---')

    # Look for the LONGEST project folder that matches our PWD prefix
    # This is critical: -Users-alice would match everything, but we want
    # the most specific match like -Users-alice-projects-myproject
    local best_match=""
    local best_length=0

    if [[ -d "$PROJECTS_DIR" ]]; then
        for project_dir in "$PROJECTS_DIR"/*/; do
            local project_name
            project_name=$(basename "$project_dir")
            # Check if our transformed PWD starts with this project name
            if [[ "$pwd_transformed" == "$project_name"* ]]; then
                local len=${#project_name}
                if (( len > best_length )); then
                    best_match="$project_name"
                    best_length=$len
                fi
            fi
        done
    fi

    if [[ -n "$best_match" ]]; then
        echo "$best_match"
    else
        # Fallback: use transformed PWD
        echo "$pwd_transformed"
    fi
}

SESSION=$(get_session_id)

# Ensure sessions object exists, then set this session's muted state
# Using jq to handle nested object creation properly
jq --arg s "$SESSION" '
    .sessions //= {} |
    .sessions[$s] //= {} |
    .sessions[$s].muted = true
' "$CONFIG_FILE" > "$CONFIG_FILE.tmp" && mv "$CONFIG_FILE.tmp" "$CONFIG_FILE"

echo "Session muted: ${SESSION}"
echo ""
echo "This session will no longer speak."
echo "Other Claude sessions are unaffected."
echo ""
echo "Use /tts-unmute to restore voice."
