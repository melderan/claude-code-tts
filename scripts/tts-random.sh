#!/bin/bash
#
# tts-random.sh - Generate a random persona from available voices
#
# Usage:
#   tts-random.sh              Generate and apply a random persona
#   tts-random.sh --preview    Show what would be generated without applying
#
# Creates a temporary persona with:
#   - Random voice from installed voices
#   - Speed between 1.8-2.2x (conversational pace)
#   - Playback speed method for natural pitch variation
#

set -euo pipefail

CONFIG_FILE="$HOME/.claude-tts/config.json"
VOICES_DIR="$HOME/.local/share/piper-voices"
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
    # Characters /, _, and . are replaced with -
    local pwd_transformed
    pwd_transformed=$(echo "$PWD" | tr '/_.' '---')

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
    echo "No config file found"
    exit 1
fi

if [[ ! -d "$VOICES_DIR" ]]; then
    echo "No voices directory found at $VOICES_DIR"
    exit 1
fi

# Get list of installed voices
VOICES=()
while IFS= read -r -d '' onnx_file; do
    voice_name=$(basename "$onnx_file" .onnx)
    VOICES+=("$voice_name")
done < <(find "$VOICES_DIR" -name "*.onnx" -print0)

if [[ ${#VOICES[@]} -eq 0 ]]; then
    echo "No voices found in $VOICES_DIR"
    exit 1
fi

# Pick random voice
RANDOM_INDEX=$((RANDOM % ${#VOICES[@]}))
RANDOM_VOICE="${VOICES[$RANDOM_INDEX]}"

# Generate random speed between 1.8 and 2.2 (in 0.1 increments)
SPEED_OPTIONS=(1.8 1.9 2.0 2.1 2.2)
SPEED_INDEX=$((RANDOM % ${#SPEED_OPTIONS[@]}))
RANDOM_SPEED="${SPEED_OPTIONS[$SPEED_INDEX]}"

# Generate persona name
PERSONA_NAME="random-$(date +%s)"

ARG="${1:-}"

if [[ "$ARG" == "--preview" ]]; then
    echo "Would create random persona:"
    echo "  Voice: $RANDOM_VOICE"
    echo "  Speed: ${RANDOM_SPEED}x"
    echo ""
    echo "Available voices:"
    printf '  %s\n' "${VOICES[@]}"
    exit 0
fi

# Create the random persona and set it for this session
jq --arg name "$PERSONA_NAME" \
   --arg voice "$RANDOM_VOICE" \
   --argjson speed "$RANDOM_SPEED" \
   --arg session "$SESSION" '
    .personas[$name] = {
        "description": "Randomly generated persona",
        "voice": $voice,
        "speed": $speed,
        "speed_method": "playback",
        "max_chars": 10000,
        "ai_type": "claude"
    } |
    .sessions[$session] //= {} |
    .sessions[$session].persona = $name
' "$CONFIG_FILE" > "$CONFIG_FILE.tmp" && mv "$CONFIG_FILE.tmp" "$CONFIG_FILE"

echo "Random persona applied:"
echo "  Voice: $RANDOM_VOICE"
echo "  Speed: ${RANDOM_SPEED}x"
echo "  Persona: $PERSONA_NAME"
