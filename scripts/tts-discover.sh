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

# shellcheck source=tts-lib.sh
source "$HOME/.claude-tts/tts-lib.sh" 2>/dev/null || source "$(dirname "$0")/tts-lib.sh"

CONFIG_FILE="$TTS_CONFIG_FILE"

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
