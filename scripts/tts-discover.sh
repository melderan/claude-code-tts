#!/bin/bash
#
# tts-discover.sh - Gather repo context for persona discovery
#
# Reads project files (README, CLAUDE.md, package.json, etc.) and outputs
# context that Claude can analyze to suggest an appropriate voice persona.
#
# Usage: tts-discover.sh
#

set -euo pipefail

CONFIG_FILE="$HOME/.claude-tts/config.json"
PROJECTS_DIR="$HOME/.claude/projects"

# --- Session detection (must match speak-response.sh) ---
get_session_id() {
    if [[ -n "${CLAUDE_TTS_SESSION:-}" ]]; then
        echo "$CLAUDE_TTS_SESSION"
        return
    fi

    local pwd_transformed
    pwd_transformed=$(echo "$PWD" | tr '/_.' '---')

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

# --- Gather repo context ---
echo "=== REPO CONTEXT ==="
echo ""
echo "Directory: $PWD"
echo "Session: $SESSION"
echo ""

# Check for common project files and extract key info
for file in CLAUDE.md README.md README package.json pyproject.toml Cargo.toml go.mod build.gradle pom.xml Makefile; do
    if [[ -f "$file" ]]; then
        echo "--- $file (first 30 lines) ---"
        head -30 "$file" 2>/dev/null || true
        echo ""
    fi
done

# Check for directory structure hints
echo "--- Directory structure ---"
ls -1 2>/dev/null | head -20
echo ""

# --- List available personas ---
echo "=== AVAILABLE PERSONAS ==="
echo ""

if [[ -f "$CONFIG_FILE" ]]; then
    jq -r '
        .personas | to_entries[] |
        "- \(.key): voice=\(.value.voice // "default"), speed=\(.value.speed // 1.0), ai_type=\(.value.ai_type // "unset")"
    ' "$CONFIG_FILE" 2>/dev/null || echo "No personas configured"
else
    echo "No config file found"
fi

echo ""
echo "=== CURRENT STATE ==="
echo ""

if [[ -f "$CONFIG_FILE" ]]; then
    # Get current persona for this session
    current=$(jq -r --arg s "$SESSION" '
        (.sessions[$s].persona // .project_personas[$s] // .active_persona // "default")
    ' "$CONFIG_FILE")
    echo "Current persona: $current"

    # Check if project persona is set
    project_persona=$(jq -r --arg s "$SESSION" '.project_personas[$s] // "not set"' "$CONFIG_FILE")
    echo "Project persona: $project_persona"
fi
