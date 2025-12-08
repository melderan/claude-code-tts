#!/bin/bash
#
# tts-speed.sh - Adjust TTS speech speed for this session
#
# Usage:
#   tts-speed.sh          Show current speed
#   tts-speed.sh 1.5      Set speed to 1.5x
#   tts-speed.sh reset    Reset to persona default
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
    echo "No config file. Default speed: 2.0x"
    exit 0
fi

ARG="${1:-}"

if [[ -z "$ARG" ]]; then
    # Show current speed - use single jq call
    CONFIG_DATA=$(jq -r --arg s "$SESSION" '
        (.sessions[$s].persona // .active_persona // "default") as $persona |
        [
            $persona,
            (.personas[$persona].speed // 2.0),
            (.sessions[$s].speed // "")
        ] | @tsv
    ' "$CONFIG_FILE")

    IFS=$'\t' read -r effective_persona persona_speed session_speed <<< "$CONFIG_DATA"

    if [[ -n "$session_speed" ]]; then
        echo "Speed:   ${session_speed}x (session override)"
        echo "Default: ${persona_speed}x (from $effective_persona)"
    else
        echo "Speed: ${persona_speed}x (from $effective_persona)"
    fi
    echo ""
    echo "Usage: /tts-speed <value|reset>"

elif [[ "$ARG" == "reset" ]]; then
    # Reset to persona default
    jq --arg s "$SESSION" '
        if .sessions[$s] then .sessions[$s] |= del(.speed) else . end
    ' "$CONFIG_FILE" > "$CONFIG_FILE.tmp" && mv "$CONFIG_FILE.tmp" "$CONFIG_FILE"

    persona_speed=$(jq -r --arg s "$SESSION" '
        .personas[.sessions[$s].persona // .active_persona // "default"].speed // 2.0
    ' "$CONFIG_FILE")
    echo "Speed reset to persona default: ${persona_speed}x"

else
    # Set speed
    if ! echo "$ARG" | grep -qE '^[0-9]+\.?[0-9]*$'; then
        echo "Invalid speed: $ARG (must be a number)"
        exit 1
    fi

    # Check range
    if (( $(echo "$ARG < 0.5" | bc -l) )) || (( $(echo "$ARG > 4.0" | bc -l) )); then
        echo "Speed out of range: $ARG (must be 0.5-4.0)"
        exit 1
    fi

    jq --arg s "$SESSION" --argjson speed "$ARG" '
        .sessions[$s] //= {} | .sessions[$s].speed = $speed
    ' "$CONFIG_FILE" > "$CONFIG_FILE.tmp" && mv "$CONFIG_FILE.tmp" "$CONFIG_FILE"
    echo "Speed set to ${ARG}x"
fi
