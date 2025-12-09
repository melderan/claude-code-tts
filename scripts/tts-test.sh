#!/bin/bash
#
# tts-test.sh - Test TTS with a realistic workflow sample
#
# Usage:
#   tts-test.sh              Run full test with current persona
#   tts-test.sh --quick      Short test (just a sentence)
#   tts-test.sh --table      Test table reading specifically
#   tts-test.sh <persona>    Test with a specific persona
#

set -euo pipefail

CONFIG_FILE="$HOME/.claude-tts/config.json"
VOICES_DIR="$HOME/.local/share/piper-voices"

# --- Parse arguments ---
TEST_TYPE="full"
TEST_PERSONA=""

for arg in "$@"; do
    case "$arg" in
        --quick) TEST_TYPE="quick" ;;
        --table) TEST_TYPE="table" ;;
        --help|-h)
            echo "Usage: tts-test.sh [--quick|--table] [persona]"
            echo ""
            echo "Options:"
            echo "  --quick     Short test (one sentence)"
            echo "  --table     Test table reading"
            echo "  <persona>   Test with specific persona"
            exit 0
            ;;
        *)
            if [[ -z "$TEST_PERSONA" ]]; then
                TEST_PERSONA="$arg"
            fi
            ;;
    esac
done

# --- Get voice settings ---
if [[ -n "$TEST_PERSONA" ]]; then
    # Use specified persona
    PERSONA="$TEST_PERSONA"
    if ! jq -e ".personas[\"$PERSONA\"]" "$CONFIG_FILE" > /dev/null 2>&1; then
        echo "Persona not found: $PERSONA"
        echo "Available: $(jq -r '.personas | keys | join(", ")' "$CONFIG_FILE")"
        exit 1
    fi
else
    # Use current session persona or global
    PERSONA=$(jq -r '.active_persona // "claude-prime"' "$CONFIG_FILE")
fi

# Get persona settings
read -r VOICE SPEED SPEED_METHOD < <(
    jq -r --arg p "$PERSONA" '
        .personas[$p] // .personas["claude-prime"] |
        [.voice, .speed, .speed_method] | @tsv
    ' "$CONFIG_FILE"
)

VOICE_FILE="$VOICES_DIR/${VOICE}.onnx"

if [[ ! -f "$VOICE_FILE" ]]; then
    echo "Voice file not found: $VOICE_FILE"
    exit 1
fi

echo "Testing persona: $PERSONA"
echo "  Voice: $VOICE"
echo "  Speed: ${SPEED}x ($SPEED_METHOD)"
echo ""

# --- Test passages ---
QUICK_TEST="Hello! I'm ready to help you with your project today."

TABLE_TEST="Here's a summary of the changes. The first item is authentication, which is complete. The second item is database migrations, currently in progress. The third item is API endpoints, still pending review."

FULL_TEST="Alright, I've analyzed the codebase and here's what I found.

The main issue is in the authentication module. There are three files we need to update: the user controller, the auth middleware, and the session handler.

For the user controller, we need to add input validation before processing the login request. This prevents injection attacks and ensures data integrity.

The auth middleware needs a small fix. Currently it's not properly checking token expiration, which could allow stale sessions to persist.

Finally, the session handler should implement the new refresh token logic we discussed.

I'll start with the user controller since that's the most critical security fix. Does this plan sound good to you?"

# --- Generate and play ---
speak_text() {
    local text="$1"
    local temp_file="/tmp/tts_test_$$.wav"

    if [[ "$SPEED_METHOD" == "length_scale" ]]; then
        LENGTH_SCALE=$(awk "BEGIN {printf \"%.2f\", 1.0 / $SPEED}")
        echo "$text" | piper --model "$VOICE_FILE" --length_scale "$LENGTH_SCALE" --output_file "$temp_file" 2>/dev/null
        afplay "$temp_file" 2>/dev/null || paplay "$temp_file" 2>/dev/null || aplay -q "$temp_file" 2>/dev/null
    else
        echo "$text" | piper --model "$VOICE_FILE" --output_file "$temp_file" 2>/dev/null
        if command -v afplay &>/dev/null; then
            afplay -r "$SPEED" "$temp_file" 2>/dev/null
        else
            paplay "$temp_file" 2>/dev/null || aplay -q "$temp_file" 2>/dev/null
        fi
    fi

    rm -f "$temp_file"
}

case "$TEST_TYPE" in
    quick)
        echo "Playing quick test..."
        speak_text "$QUICK_TEST"
        ;;
    table)
        echo "Playing table test..."
        speak_text "$TABLE_TEST"
        ;;
    full)
        echo "Playing full workflow test..."
        echo "(This simulates a typical Claude response)"
        echo ""
        speak_text "$FULL_TEST"
        ;;
esac

echo ""
echo "Test complete. How did that sound?"
echo ""
echo "Adjust settings with:"
echo "  /tts-speed <value>     Change speed (current: ${SPEED}x)"
echo "  /tts-persona <name>    Switch persona"
echo "  /tts-random            Try a random voice"
