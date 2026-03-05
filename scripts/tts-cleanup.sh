#!/bin/bash
#
# tts-cleanup.sh - Clean up stale TTS sessions
#
# Removes session files from sessions.d/ for projects that no longer exist
# in ~/.claude/projects/. Also cleans legacy .sessions entries from config.json.
#

set -euo pipefail

# shellcheck source=tts-lib.sh
source "$HOME/.claude-tts/tts-lib.sh" 2>/dev/null || source "$(dirname "$0")/tts-lib.sh"

PROJECTS_DIR="$HOME/.claude/projects"

if [[ ! -d "$PROJECTS_DIR" ]]; then
    echo "No Claude projects directory found"
    exit 0
fi

# Check sessions.d/ files
stale_sessions=()
active_sessions=()

if [[ -d "$TTS_SESSIONS_DIR" ]]; then
    for session_file in "$TTS_SESSIONS_DIR"/*.json; do
        [[ -f "$session_file" ]] || continue
        session_id=$(basename "$session_file" .json)
        if [[ -d "$PROJECTS_DIR/$session_id" ]]; then
            active_sessions+=("$session_id")
        else
            stale_sessions+=("$session_id")
        fi
    done
fi

# Also check for legacy .sessions in config.json
legacy_stale=()
if [[ -f "$TTS_CONFIG_FILE" ]]; then
    while IFS= read -r session; do
        [[ -z "$session" ]] && continue
        if [[ ! -d "$PROJECTS_DIR/$session" ]]; then
            legacy_stale+=("$session")
        fi
    done < <(jq -r '.sessions // {} | keys[]' "$TTS_CONFIG_FILE" 2>/dev/null)
fi

echo "Active sessions: ${#active_sessions[@]}"
echo "Stale sessions:  ${#stale_sessions[@]} (sessions.d) + ${#legacy_stale[@]} (legacy config.json)"

total_stale=$(( ${#stale_sessions[@]} + ${#legacy_stale[@]} ))
if [[ "$total_stale" -eq 0 ]]; then
    echo ""
    echo "Nothing to clean up."
    exit 0
fi

echo ""
echo "Stale sessions (project directories no longer exist):"
for session in "${stale_sessions[@]}"; do
    echo "  - $session (sessions.d)"
done
for session in "${legacy_stale[@]}"; do
    echo "  - $session (config.json legacy)"
done

# Check for --dry-run or prompt
if [[ "${1:-}" == "--dry-run" ]]; then
    echo ""
    echo "(dry run - no changes made)"
    exit 0
fi

echo ""
read -p "Remove these ${total_stale} stale session(s)? [y/N] " -n 1 -r
echo ""

if [[ $REPLY =~ ^[Yy]$ ]]; then
    # Remove stale session files (with restorable logging)
    for session in "${stale_sessions[@]}"; do
        sf="$TTS_SESSIONS_DIR/${session}.json"
        if [[ -f "$sf" ]]; then
            content=$(cat "$sf")
            tts_debug "Cleanup: removing $session | restore: echo '$content' > $sf"
            rm -f "$sf"
        fi
    done

    # Remove legacy entries from config.json
    if [[ ${#legacy_stale[@]} -gt 0 ]]; then
        JQ_FILTER="."
        for session in "${legacy_stale[@]}"; do
            JQ_FILTER="$JQ_FILTER | del(.sessions[\"$session\"])"
        done
        jq "$JQ_FILTER" "$TTS_CONFIG_FILE" > "$TTS_CONFIG_FILE.tmp" \
            && mv "$TTS_CONFIG_FILE.tmp" "$TTS_CONFIG_FILE"
    fi

    echo "Removed ${total_stale} stale session(s)"
else
    echo "Cancelled"
fi
