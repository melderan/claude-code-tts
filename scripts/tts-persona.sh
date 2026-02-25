#!/bin/bash
#
# tts-persona.sh - Manage TTS voice personas for this session
#
# Usage:
#   tts-persona.sh                    List personas and show current
#   tts-persona.sh <name>             Switch to persona for this session
#   tts-persona.sh reset              Reset to global persona
#   tts-persona.sh --project          Show project persona
#   tts-persona.sh --project <name>   Set sticky project persona
#   tts-persona.sh --project reset    Clear project persona
#   tts-persona.sh --project <name> --session  Set both project and session (flags in any order)
#

set -euo pipefail

# shellcheck source=tts-lib.sh
source "$HOME/.claude-tts/tts-lib.sh" 2>/dev/null || source "$(dirname "$0")/tts-lib.sh"

CONFIG_FILE="$TTS_CONFIG_FILE"

SESSION=$(get_session_id)

if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "No config file found"
    exit 1
fi

# --- Parse arguments (flags can be in any order) ---
FLAG_PROJECT=false
FLAG_SESSION=false
PERSONA_NAME=""

for arg in "$@"; do
    case "$arg" in
        --project) FLAG_PROJECT=true ;;
        --session) FLAG_SESSION=true ;;
        *) PERSONA_NAME="$arg" ;;
    esac
done

# --- Handle --project flag ---
if [[ "$FLAG_PROJECT" == true ]]; then
    if [[ -z "$PERSONA_NAME" ]]; then
        # Show current project persona
        project_persona=$(jq -r --arg s "$SESSION" '.project_personas[$s] // ""' "$CONFIG_FILE")
        if [[ -n "$project_persona" ]]; then
            echo "Project persona: $project_persona"
        else
            echo "No project persona set"
            echo "Use: tts-persona.sh --project <name>"
        fi
        exit 0
    elif [[ "$PERSONA_NAME" == "reset" ]]; then
        # Clear project persona
        jq --arg s "$SESSION" 'del(.project_personas[$s])' "$CONFIG_FILE" > "$CONFIG_FILE.tmp" \
            && mv "$CONFIG_FILE.tmp" "$CONFIG_FILE"
        echo "Project persona cleared"
        exit 0
    else
        # Set project persona
        if ! jq -e ".personas[\"$PERSONA_NAME\"]" "$CONFIG_FILE" > /dev/null 2>&1; then
            echo "Persona not found: $PERSONA_NAME"
            echo ""
            echo "Available personas:"
            jq -r '.personas | keys[]' "$CONFIG_FILE" | sed 's/^/  /'
            exit 1
        fi

        if [[ "$FLAG_SESSION" == true ]]; then
            # Set BOTH project and session
            jq --arg s "$SESSION" --arg p "$PERSONA_NAME" '
                .project_personas[$s] = $p |
                .sessions[$s] //= {} | .sessions[$s].persona = $p
            ' "$CONFIG_FILE" > "$CONFIG_FILE.tmp" && mv "$CONFIG_FILE.tmp" "$CONFIG_FILE"
            echo "Project and session persona set to: $PERSONA_NAME"
        else
            # Set project only
            jq --arg s "$SESSION" --arg p "$PERSONA_NAME" '
                .project_personas[$s] = $p
            ' "$CONFIG_FILE" > "$CONFIG_FILE.tmp" && mv "$CONFIG_FILE.tmp" "$CONFIG_FILE"
            echo "Project persona set to: $PERSONA_NAME"
            echo "(This will be used by default for all sessions in this project)"
        fi
        exit 0
    fi
fi

# Legacy support: just --session with a persona name sets session only
ARG="$PERSONA_NAME"

if [[ -z "$ARG" || "$ARG" == "list" ]]; then
    # List personas and show current
    echo "Personas:"
    jq -r '.personas | to_entries[] | "  \(.key): \(.value.description // "No description")"' "$CONFIG_FILE"
    echo ""

    read -r global_persona project_persona session_persona < <(
        jq -r --arg s "$SESSION" '[
            (.active_persona // "default"),
            (.project_personas[$s] // ""),
            (.sessions[$s].persona // "")
        ] | @tsv' "$CONFIG_FILE"
    )

    echo "Global:  $global_persona"
    if [[ -n "$project_persona" ]]; then
        echo "Project: $project_persona"
    else
        echo "Project: (none)"
    fi
    if [[ -n "$session_persona" ]]; then
        echo "Session: $session_persona"
    else
        echo "Session: (using project or global)"
    fi

    # Show effective persona
    effective="$global_persona"
    [[ -n "$project_persona" ]] && effective="$project_persona"
    [[ -n "$session_persona" ]] && effective="$session_persona"
    echo ""
    echo "Effective: $effective"

elif [[ "$ARG" == "reset" ]]; then
    # Reset session persona (will fall back to project or global)
    jq --arg s "$SESSION" '
        if .sessions[$s] then .sessions[$s] |= del(.persona) else . end
    ' "$CONFIG_FILE" > "$CONFIG_FILE.tmp" && mv "$CONFIG_FILE.tmp" "$CONFIG_FILE"

    # Show effective persona after reset (project > global)
    read -r project_persona global_persona < <(
        jq -r --arg s "$SESSION" '[
            (.project_personas[$s] // ""),
            (.active_persona // "default")
        ] | @tsv' "$CONFIG_FILE"
    )

    if [[ -n "$project_persona" ]]; then
        echo "Session persona cleared"
        echo "Effective: $project_persona (project default)"
    else
        echo "Session persona cleared"
        echo "Effective: $global_persona (global default)"
    fi

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
