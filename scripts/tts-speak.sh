#!/bin/bash
#
# tts-speak.sh - Standalone TTS tool for testing voices
#
# Speaks text using Piper TTS without requiring Claude Code.
# Useful for testing voices, speeds, and methods without burning tokens.
#
# Usage:
#   tts-speak.sh "Hello world"                    # Use current persona settings
#   tts-speak.sh --voice en_US-joe-medium "Hi"    # Specify voice
#   tts-speak.sh --speed 1.8 "Hello"              # Specify speed
#   tts-speak.sh --method length_scale "Hello"    # Specify method
#   tts-speak.sh --voice en_US-ryan-medium --speed 2.0 --method playback "Test"
#
# Options:
#   --voice NAME     Voice model name (without path/extension)
#   --speed N        Speed multiplier (0.5-4.0)
#   --method METHOD  Speed method: playback, length_scale, or hybrid
#   --boost N        Playback boost for hybrid mode (default: 1.0)
#   --speaker N      Speaker ID for multi-speaker models (e.g., libritts has 904)
#   --list           List available voices
#   --random         Pick a random speaker (for multi-speaker models)
#   --help           Show this help
#

set -euo pipefail

# --- Configuration ---
CONFIG_FILE="$HOME/.claude-tts/config.json"
VOICES_DIR="$HOME/.local/share/piper-voices"
TEMP_FILE="/tmp/tts_speak_$$.wav"

# --- Defaults ---
VOICE=""
SPEED=""
METHOD=""
BOOST="1.0"
SPEAKER=""
TEXT=""

# --- Parse arguments ---
show_help() {
    sed -n '2,/^$/p' "$0" | sed 's/^# *//'
    exit 0
}

list_voices() {
    echo "Available voices in $VOICES_DIR:"
    echo ""
    if [[ -d "$VOICES_DIR" ]]; then
        for f in "$VOICES_DIR"/*.onnx; do
            [[ -f "$f" ]] || continue
            basename "$f" .onnx
        done | sort
    else
        echo "  (no voices installed)"
    fi
    exit 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --voice)
            VOICE="$2"
            shift 2
            ;;
        --speed)
            SPEED="$2"
            shift 2
            ;;
        --method)
            METHOD="$2"
            shift 2
            ;;
        --boost)
            BOOST="$2"
            shift 2
            ;;
        --speaker)
            SPEAKER="$2"
            shift 2
            ;;
        --random)
            SPEAKER="random"
            shift
            ;;
        --list)
            list_voices
            ;;
        --help|-h)
            show_help
            ;;
        -*)
            echo "Unknown option: $1" >&2
            exit 1
            ;;
        *)
            TEXT="$1"
            shift
            ;;
    esac
done

if [[ -z "$TEXT" ]]; then
    echo "Usage: tts-speak.sh [options] \"text to speak\"" >&2
    echo "       tts-speak.sh --help for more info" >&2
    exit 1
fi

# --- Load defaults from config if not specified ---
if [[ -f "$CONFIG_FILE" ]]; then
    ACTIVE_PERSONA=$(jq -r '.active_persona // "default"' "$CONFIG_FILE")

    if [[ -z "$VOICE" ]]; then
        VOICE=$(jq -r --arg p "$ACTIVE_PERSONA" '.personas[$p].voice // "en_US-hfc_male-medium"' "$CONFIG_FILE")
    fi
    if [[ -z "$SPEED" ]]; then
        SPEED=$(jq -r --arg p "$ACTIVE_PERSONA" '.personas[$p].speed // 2.0' "$CONFIG_FILE")
    fi
    if [[ -z "$METHOD" ]]; then
        METHOD=$(jq -r --arg p "$ACTIVE_PERSONA" '.personas[$p].speed_method // "playback"' "$CONFIG_FILE")
    fi
else
    # Fallbacks if no config
    [[ -z "$VOICE" ]] && VOICE="en_US-hfc_male-medium"
    [[ -z "$SPEED" ]] && SPEED="2.0"
    [[ -z "$METHOD" ]] && METHOD="playback"
fi

# --- Resolve voice path ---
VOICE_PATH="$VOICES_DIR/${VOICE}.onnx"
if [[ ! -f "$VOICE_PATH" ]]; then
    echo "Voice not found: $VOICE_PATH" >&2
    echo "Run tts-speak.sh --list to see available voices" >&2
    exit 1
fi

# --- Check for piper ---
if ! command -v piper &>/dev/null; then
    echo "Error: piper not found. Install it first." >&2
    exit 1
fi

# --- Generate and play ---
cleanup() {
    rm -f "$TEMP_FILE"
}
trap cleanup EXIT

# --- Check for multi-speaker model ---
VOICE_JSON="$VOICES_DIR/${VOICE}.onnx.json"
NUM_SPEAKERS=1
if [[ -f "$VOICE_JSON" ]]; then
    NUM_SPEAKERS=$(jq -r '.num_speakers // 1' "$VOICE_JSON")
fi

# Handle random speaker selection
if [[ "$SPEAKER" == "random" ]]; then
    if [[ "$NUM_SPEAKERS" -gt 1 ]]; then
        SPEAKER=$((RANDOM % NUM_SPEAKERS))
    else
        SPEAKER=""
    fi
fi

# Build speaker flag
SPEAKER_FLAG=""
if [[ -n "$SPEAKER" && "$NUM_SPEAKERS" -gt 1 ]]; then
    SPEAKER_FLAG="--speaker $SPEAKER"
fi

echo "Voice: $VOICE"
[[ "$NUM_SPEAKERS" -gt 1 ]] && echo "Speakers available: $NUM_SPEAKERS"
[[ -n "$SPEAKER" ]] && echo "Speaker ID: $SPEAKER"
echo "Speed: ${SPEED}x ($METHOD)"
[[ "$METHOD" == "hybrid" ]] && echo "Boost: ${BOOST}x"
echo ""

case "$METHOD" in
    length_scale)
        LENGTH_SCALE=$(awk "BEGIN {printf \"%.2f\", 1.0 / $SPEED}")
        echo "$TEXT" | piper --model "$VOICE_PATH" --length_scale "$LENGTH_SCALE" $SPEAKER_FLAG --output_file "$TEMP_FILE" 2>/dev/null
        afplay "$TEMP_FILE" 2>/dev/null
        ;;
    playback)
        echo "$TEXT" | piper --model "$VOICE_PATH" $SPEAKER_FLAG --output_file "$TEMP_FILE" 2>/dev/null
        afplay -r "$SPEED" "$TEMP_FILE" 2>/dev/null
        ;;
    hybrid)
        # Generate with length_scale, then also speed up playback
        LENGTH_SCALE=$(awk "BEGIN {printf \"%.2f\", 1.0 / $SPEED}")
        echo "$TEXT" | piper --model "$VOICE_PATH" --length_scale "$LENGTH_SCALE" $SPEAKER_FLAG --output_file "$TEMP_FILE" 2>/dev/null
        afplay -r "$BOOST" "$TEMP_FILE" 2>/dev/null
        ;;
    *)
        echo "Unknown method: $METHOD (use playback, length_scale, or hybrid)" >&2
        exit 1
        ;;
esac

echo "Done."
