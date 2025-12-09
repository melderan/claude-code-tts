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
ARG2="${2:-}"

# --- Handle --project flag ---
if [[ "$ARG" == "--project" ]]; then
    if [[ -z "$ARG2" ]]; then
        # Show current project persona
        project_persona=$(jq -r --arg s "$SESSION" '.project_personas[$s] // ""' "$CONFIG_FILE")
        if [[ -n "$project_persona" ]]; then
            echo "Project persona: $project_persona"
        else
            echo "No project persona set"
            echo "Use: tts-persona.sh --project <name>"
        fi
        exit 0
    elif [[ "$ARG2" == "reset" ]]; then
        # Clear project persona
        jq --arg s "$SESSION" 'del(.project_personas[$s])' "$CONFIG_FILE" > "$CONFIG_FILE.tmp" \
            && mv "$CONFIG_FILE.tmp" "$CONFIG_FILE"
        echo "Project persona cleared"
        exit 0
    else
        # Set project persona
        if ! jq -e ".personas[\"$ARG2\"]" "$CONFIG_FILE" > /dev/null 2>&1; then
            echo "Persona not found: $ARG2"
            echo ""
            echo "Available personas:"
            jq -r '.personas | keys[]' "$CONFIG_FILE" | sed 's/^/  /'
            exit 1
        fi
        jq --arg s "$SESSION" --arg p "$ARG2" '
            .project_personas[$s] = $p
        ' "$CONFIG_FILE" > "$CONFIG_FILE.tmp" && mv "$CONFIG_FILE.tmp" "$CONFIG_FILE"
        echo "Project persona set to: $ARG2"
        echo "(This will be used by default for all sessions in this project)"
        exit 0
    fi
fi

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
