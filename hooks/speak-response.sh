#!/bin/bash
#
# speak-response.sh - TTS hook for Claude Code
#
# Reads Claude's responses aloud using Piper TTS (with macOS 'say' fallback).
# Designed to be triggered as a Claude Code "Stop" hook.
#
# CONFIGURATION (environment variables):
#   CLAUDE_TTS_ENABLED=1              Enable/disable TTS (default: 1)
#   CLAUDE_TTS_SPEED=2.0              Playback speed multiplier (default: 2.0)
#   CLAUDE_TTS_VOICE=<path>           Piper voice model path (default: ~/.local/share/piper-voices/en_US-hfc_male-medium.onnx)
#   CLAUDE_TTS_MAX_CHARS=10000        Max characters to speak (default: 10000)
#
# MUTING:
#   Create /tmp/claude_tts_muted to temporarily mute (use /mute command)
#   Remove it to unmute (use /unmute command)
#
# DEBUG:
#   Logs written to /tmp/claude_tts_debug.log
#

set -euo pipefail

# --- Debug logging ---
DEBUG_LOG="/tmp/claude_tts_debug.log"
debug() {
    echo "[$(date '+%H:%M:%S')] $*" >> "$DEBUG_LOG"
}
debug "=== Hook triggered ==="

# --- Configuration ---
TTS_ENABLED="${CLAUDE_TTS_ENABLED:-1}"
TTS_SPEED="${CLAUDE_TTS_SPEED:-2.0}"
TTS_VOICE="${CLAUDE_TTS_VOICE:-$HOME/.local/share/piper-voices/en_US-hfc_male-medium.onnx}"
TTS_MAX_CHARS="${CLAUDE_TTS_MAX_CHARS:-10000}"
TTS_TEMP_DIR="/tmp"
TTS_MUTE_FILE="/tmp/claude_tts_muted"

# Exit early if TTS is disabled via env var
if [[ "$TTS_ENABLED" != "1" ]]; then
    debug "TTS disabled via env var"
    exit 0
fi

# Exit early if muted via /mute command (check for mute file)
if [[ -f "$TTS_MUTE_FILE" ]]; then
    debug "TTS muted via mute file"
    exit 0
fi

# --- Read hook input from stdin ---
INPUT=$(cat)
debug "Input received: ${INPUT:0:200}"
TRANSCRIPT_PATH=$(echo "$INPUT" | jq -r '.transcript_path // empty')
debug "Transcript path: $TRANSCRIPT_PATH"

if [[ -z "$TRANSCRIPT_PATH" || ! -f "$TRANSCRIPT_PATH" ]]; then
    debug "No transcript path or file not found"
    exit 0
fi

# --- Extract last assistant message WITH TEXT from transcript (JSONL format) ---
# Find the last line with type "assistant" that contains text content (not just tool_use)
# This is important: when Claude runs tools, the transcript may have assistant messages
# that only contain tool_use blocks. We need to skip those and find actual text.
LAST_ASSISTANT=""
while IFS= read -r line; do
    TYPE=$(echo "$line" | jq -r '.type // empty' 2>/dev/null)
    if [[ "$TYPE" == "assistant" ]]; then
        # Check if this message has any text content blocks
        HAS_TEXT=$(echo "$line" | jq -r '[.message.content[]? | select(.type == "text")] | length' 2>/dev/null || echo "0")
        if [[ "$HAS_TEXT" -gt 0 ]]; then
            LAST_ASSISTANT="$line"
            break
        fi
        debug "Skipping assistant message with no text (tool_use only)"
    fi
done < <(tac "$TRANSCRIPT_PATH" 2>/dev/null)

if [[ -z "$LAST_ASSISTANT" ]]; then
    debug "No assistant message with text found"
    exit 0
fi
debug "Found assistant message with text (${#LAST_ASSISTANT} chars)"

# --- Extract text content from the message ---
RESPONSE=$(echo "$LAST_ASSISTANT" | jq -r '
    if .message.content then
        if (.message.content | type) == "string" then
            .message.content
        elif (.message.content | type) == "array" then
            [.message.content[] | select(.type == "text") | .text] | join(" ")
        else
            empty
        end
    else
        empty
    end
' 2>/dev/null || true)

if [[ -z "$RESPONSE" ]]; then
    debug "No response content extracted"
    exit 0
fi
debug "Extracted response (${#RESPONSE} chars): ${RESPONSE:0:100}..."

# --- Filter out code blocks and clean up for speech ---
# Remove fenced code blocks (```...```)
CLIFF_NOTES=$(echo "$RESPONSE" | perl -0777 -pe 's/```[\s\S]*?```//g')

# Remove indented code blocks (4+ spaces at start of line)
CLIFF_NOTES=$(echo "$CLIFF_NOTES" | grep -v '^    ' || echo "$CLIFF_NOTES")

# Remove inline code (`...`)
CLIFF_NOTES=$(echo "$CLIFF_NOTES" | sed 's/`[^`]*`//g')

# Remove markdown formatting for cleaner speech (BSD sed compatible)
CLIFF_NOTES=$(echo "$CLIFF_NOTES" | sed 's/^##* *//g')             # Headers
CLIFF_NOTES=$(echo "$CLIFF_NOTES" | sed 's/\*\*//g')               # Bold markers
CLIFF_NOTES=$(echo "$CLIFF_NOTES" | sed 's/\*//g')                 # Italic markers
CLIFF_NOTES=$(echo "$CLIFF_NOTES" | sed 's/\[.*\](.*)/removed_link/g') # Links

# Clean up whitespace
CLIFF_NOTES=$(echo "$CLIFF_NOTES" | tr '\n' ' ' | sed 's/  */ /g' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')

# Skip if nothing left after filtering
if [[ -z "$CLIFF_NOTES" || ${#CLIFF_NOTES} -lt 10 ]]; then
    debug "Text too short after filtering, skipping"
    exit 0
fi

# Truncate to max chars
if [[ ${#CLIFF_NOTES} -gt $TTS_MAX_CHARS ]]; then
    CLIFF_NOTES="${CLIFF_NOTES:0:$TTS_MAX_CHARS}..."
fi

# --- Rotating temp file (5 slots to avoid file conflicts) ---
SLOT=$(( $(date +%s) % 5 ))
TEMP_FILE="${TTS_TEMP_DIR}/claude_tts_${SLOT}.wav"

# --- Generate and play audio ---
debug "Final text (${#CLIFF_NOTES} chars): ${CLIFF_NOTES:0:100}..."

if command -v piper &>/dev/null; then
    debug "Using piper for TTS"
    echo "$CLIFF_NOTES" | piper --model "$TTS_VOICE" --output_file "$TEMP_FILE" 2>/dev/null
    if [[ -f "$TEMP_FILE" ]]; then
        debug "Playing audio: $TEMP_FILE"
        # macOS: afplay with rate adjustment
        # Linux: would use aplay or paplay
        if command -v afplay &>/dev/null; then
            afplay -r "$TTS_SPEED" "$TEMP_FILE" 2>/dev/null &
        elif command -v paplay &>/dev/null; then
            # PulseAudio (Linux) - note: no speed adjustment available
            paplay "$TEMP_FILE" 2>/dev/null &
        elif command -v aplay &>/dev/null; then
            aplay "$TEMP_FILE" 2>/dev/null &
        fi
    else
        debug "ERROR: Failed to generate audio file"
    fi
else
    # Fallback to macOS 'say' command (rate in words per minute, ~200 is normal)
    if command -v say &>/dev/null; then
        RATE=$(echo "$TTS_SPEED * 200" | bc | cut -d'.' -f1)
        say -r "$RATE" "$CLIFF_NOTES" &
    else
        debug "ERROR: No TTS engine available (piper or say)"
    fi
fi

exit 0
