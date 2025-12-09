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

# --- Session detection (must match speak-response.sh) ---
# Claude Code creates project folders like: ~/.claude/projects/-Users-foo-bar-project/
# The folder name is the path with / and _ replaced by -
# We need to find the LONGEST matching project that contains our current PWD
get_session_id() {
    if [[ -n "${CLAUDE_TTS_SESSION:-}" ]]; then
        echo "$CLAUDE_TTS_SESSION"
        return
    fi

    # Convert PWD to Claude Code format: /Users/foo/_bar -> -Users-foo--bar
    # Both / and _ are replaced with -
    local pwd_transformed
    pwd_transformed=$(echo "$PWD" | tr '/_' '--')

    # Look for the LONGEST project folder that matches our PWD prefix
    local best_match=""
    local best_length=0

    if [[ -d "$PROJECTS_DIR" ]]; then
        for project_dir in "$PROJECTS_DIR"/*/; do
            local project_name
            project_name=$(basename "$project_dir")
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
        echo "$pwd_transformed"
    fi
}

SESSION=$(get_session_id)

if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "No config file. Default speed: 2.0x"
    exit 0
fi

ARG="${1:-}"

if [[ -z "$ARG" ]]; then
    # Show current speed - use single jq call (session > project > global)
    CONFIG_DATA=$(jq -r --arg s "$SESSION" '
        (.sessions[$s].persona // .project_personas[$s] // .active_persona // "default") as $persona |
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
        .personas[.sessions[$s].persona // .project_personas[$s] // .active_persona // "default"].speed // 2.0
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
