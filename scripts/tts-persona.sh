#!/bin/bash
#
# tts-persona.sh - Manage TTS voice personas for this session
#
# Usage:
#   tts-persona.sh              List personas and show current
#   tts-persona.sh <name>       Switch to persona for this session
#   tts-persona.sh reset        Reset to global persona
#

set -euo pipefail

CONFIG_FILE="$HOME/.claude-tts/config.json"
PROJECTS_DIR="$HOME/.claude/projects"

# --- Session detection (same as other scripts) ---
get_session_id() {
    if [[ -n "${CLAUDE_TTS_SESSION:-}" ]]; then
        echo "$CLAUDE_TTS_SESSION"
        return
    fi
    local pwd_transformed
    pwd_transformed=$(echo "$PWD" | sed 's|/|-|g')
    if [[ -d "$PROJECTS_DIR" ]]; then
        for project_dir in "$PROJECTS_DIR"/*/; do
            local project_name
            project_name=$(basename "$project_dir")
            if [[ "$pwd_transformed" == "$project_name"* ]]; then
                echo "$project_name"
                return
            fi
        done
    fi
    echo "$pwd_transformed"
}

SESSION=$(get_session_id)

if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "No config file found"
    exit 1
fi

ARG="${1:-}"

if [[ -z "$ARG" || "$ARG" == "list" ]]; then
    # List personas and show current
    echo "Personas:"
    jq -r '.personas | to_entries[] | "  \(.key): \(.value.description // "No description")"' "$CONFIG_FILE"
    echo ""

    read -r global_persona session_persona < <(
        jq -r --arg s "$SESSION" '[
            (.active_persona // "default"),
            (.sessions[$s].persona // "")
        ] | @tsv' "$CONFIG_FILE"
    )

    echo "Global:  $global_persona"
    if [[ -n "$session_persona" ]]; then
        echo "Session: $session_persona"
    else
        echo "Session: (using global)"
    fi

elif [[ "$ARG" == "reset" ]]; then
    # Reset to global persona
    jq --arg s "$SESSION" '
        if .sessions[$s] then .sessions[$s] |= del(.persona) else . end
    ' "$CONFIG_FILE" > "$CONFIG_FILE.tmp" && mv "$CONFIG_FILE.tmp" "$CONFIG_FILE"

    global_persona=$(jq -r '.active_persona // "default"' "$CONFIG_FILE")
    echo "Reset to global persona: $global_persona"

else
    # Set persona for session
    if ! jq -e ".personas[\"$ARG\"]" "$CONFIG_FILE" > /dev/null 2>&1; then
        echo "Persona not found: $ARG"
        echo ""
        echo "Available personas:"
        jq -r '.personas | keys[]' "$CONFIG_FILE" | sed 's/^/  /'
        exit 1
    fi

    jq --arg s "$SESSION" --arg p "$ARG" '
        .sessions[$s] //= {} | .sessions[$s].persona = $p
    ' "$CONFIG_FILE" > "$CONFIG_FILE.tmp" && mv "$CONFIG_FILE.tmp" "$CONFIG_FILE"
    echo "Persona set to: $ARG"
fi
