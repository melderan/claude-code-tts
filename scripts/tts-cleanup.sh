#!/bin/bash
#
# tts-cleanup.sh - Clean up stale TTS sessions
#
# Removes session entries from config.json for projects that no longer exist
# in ~/.claude/projects/
#

set -euo pipefail

CONFIG_FILE="$HOME/.claude-tts/config.json"
PROJECTS_DIR="$HOME/.claude/projects"

if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "No config file found"
    exit 0
fi

if [[ ! -d "$PROJECTS_DIR" ]]; then
    echo "No Claude projects directory found"
    exit 0
fi

# Get list of sessions from config
SESSIONS=$(jq -r '.sessions | keys[]' "$CONFIG_FILE" 2>/dev/null)

if [[ -z "$SESSIONS" ]]; then
    echo "No sessions in config"
    exit 0
fi

# Check each session
stale_sessions=()
active_sessions=()

while IFS= read -r session; do
    if [[ -d "$PROJECTS_DIR/$session" ]]; then
        active_sessions+=("$session")
    else
        stale_sessions+=("$session")
    fi
done <<< "$SESSIONS"

echo "Active sessions: ${#active_sessions[@]}"
echo "Stale sessions:  ${#stale_sessions[@]}"

if [[ ${#stale_sessions[@]} -eq 0 ]]; then
    echo ""
    echo "Nothing to clean up."
    exit 0
fi

echo ""
echo "Stale sessions (project directories no longer exist):"
for session in "${stale_sessions[@]}"; do
    echo "  - $session"
done

# Check for --dry-run or prompt
if [[ "${1:-}" == "--dry-run" ]]; then
    echo ""
    echo "(dry run - no changes made)"
    exit 0
fi

echo ""
read -p "Remove these ${#stale_sessions[@]} stale session(s)? [y/N] " -n 1 -r
echo ""

if [[ $REPLY =~ ^[Yy]$ ]]; then
    # Build jq filter to delete stale sessions
    JQ_FILTER="."
    for session in "${stale_sessions[@]}"; do
        JQ_FILTER="$JQ_FILTER | del(.sessions[\"$session\"])"
    done

    # Apply filter
    jq "$JQ_FILTER" "$CONFIG_FILE" > "$CONFIG_FILE.tmp" && mv "$CONFIG_FILE.tmp" "$CONFIG_FILE"
    echo "Removed ${#stale_sessions[@]} stale session(s)"
else
    echo "Cancelled"
fi
