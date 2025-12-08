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

CONFIG_FILE="$HOME/.claude-tts/config.json"
PROJECTS_DIR="$HOME/.claude/projects"

# --- Session detection (must match speak-response.sh) ---
# Claude Code creates project folders like: ~/.claude/projects/-Users-foo-bar-project/
# The folder name is the path with / replaced by -
# We need to find the project that contains our current PWD
get_session_id() {
    # If explicitly set, use that
    if [[ -n "${CLAUDE_TTS_SESSION:-}" ]]; then
        echo "$CLAUDE_TTS_SESSION"
        return
    fi

    # Convert PWD to Claude Code format: /Users/foo/bar -> -Users-foo-bar
    local pwd_transformed
    pwd_transformed=$(echo "$PWD" | sed 's|/|-|g')

    # Look for a project folder that matches or is a prefix of our PWD
    # (handles being in a subdirectory of the project)
    if [[ -d "$PROJECTS_DIR" ]]; then
        for project_dir in "$PROJECTS_DIR"/*/; do
            local project_name
            project_name=$(basename "$project_dir")
            # Check if our transformed PWD starts with this project name
            # This handles subdirectories: -Users-foo-bar-src starts with -Users-foo-bar
            if [[ "$pwd_transformed" == "$project_name"* ]]; then
                echo "$project_name"
                return
            fi
        done
    fi

    # Fallback: use transformed PWD (may not match if in subdirectory)
    echo "$pwd_transformed"
}

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
