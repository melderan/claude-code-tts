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

# shellcheck source=tts-lib.sh
source "$HOME/.claude-tts/tts-lib.sh" 2>/dev/null || source "$(dirname "$0")/tts-lib.sh"

CONFIG_FILE="$TTS_CONFIG_FILE"

SESSION=$(get_session_id)

if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "No config file. Default speed: 2.0x"
    exit 0
fi

ARG="${1:-}"

if [[ -z "$ARG" ]]; then
    # Show current speed - read session file + global config
    local_session_file="$TTS_SESSIONS_DIR/${SESSION}.json"
    session_speed=""
    session_persona=""
    if [[ -f "$local_session_file" ]]; then
        session_speed=$(jq -r '.speed // "" | tostring' "$local_session_file" 2>/dev/null)
        session_persona=$(jq -r '.persona // ""' "$local_session_file" 2>/dev/null)
        [[ "$session_speed" == "null" ]] && session_speed=""
        [[ "$session_persona" == "null" ]] && session_persona=""
    fi

    # Get persona speed from global config
    if [[ -z "$session_persona" ]]; then
        effective_persona=$(jq -r --arg s "$SESSION" '
            .project_personas[$s] // .active_persona // "default"
        ' "$CONFIG_FILE")
    else
        effective_persona="$session_persona"
    fi
    persona_speed=$(jq -r --arg p "$effective_persona" '.personas[$p].speed // 2.0' "$CONFIG_FILE")

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
    tts_session_del "$SESSION" "speed"

    # Determine effective persona to show default speed
    local_session_file="$TTS_SESSIONS_DIR/${SESSION}.json"
    session_persona=""
    [[ -f "$local_session_file" ]] && session_persona=$(jq -r '.persona // ""' "$local_session_file" 2>/dev/null)
    [[ "$session_persona" == "null" ]] && session_persona=""
    if [[ -z "$session_persona" ]]; then
        effective_persona=$(jq -r --arg s "$SESSION" '.project_personas[$s] // .active_persona // "default"' "$CONFIG_FILE")
    else
        effective_persona="$session_persona"
    fi
    persona_speed=$(jq -r --arg p "$effective_persona" '.personas[$p].speed // 2.0' "$CONFIG_FILE")
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

    tts_session_set "$SESSION" "speed" "$ARG" "number"
    echo "Speed set to ${ARG}x"
fi
