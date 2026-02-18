#!/bin/bash
#
# speak-response.sh - TTS Stop hook for Claude Code
#
# Reads Claude's final response text aloud. Coordinates with
# speak-intermediate.sh (PostToolUse) via watermark state to
# avoid re-speaking text that was already spoken during the turn.
#
# PLATFORMS:
#   - macOS: Uses afplay for audio playback
#   - Linux: Uses paplay (PulseAudio) or aplay (ALSA)
#   - WSL 2: Uses paplay via WSLg's PulseAudio server

set -euo pipefail

# --- Source shared library ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_FILE="$HOME/.claude-tts/tts-lib.sh"
if [[ -f "$LIB_FILE" ]]; then
    source "$LIB_FILE"
else
    # Fallback to repo copy during development
    REPO_LIB="$(dirname "$SCRIPT_DIR")/scripts/tts-lib.sh"
    if [[ -f "$REPO_LIB" ]]; then
        source "$REPO_LIB"
    else
        exit 0
    fi
fi

tts_debug "=== Stop hook triggered ==="

# --- Read input and check early exits ---
tts_read_input
if tts_should_exit; then exit 0; fi

# --- Detect session and load config ---
tts_detect_session
tts_load_config

# Exit if muted
if [[ "$TTS_MUTED" == "true" ]]; then
    tts_debug "Stop: muted, skipping"
    exit 0
fi

# --- Check watermark: only speak text after what PostToolUse already spoke ---
WATERMARK=$(tts_read_watermark)
CURRENT_LINES=$(wc -l < "$TRANSCRIPT_PATH" | tr -d ' ')
tts_debug "Stop: watermark=$WATERMARK current=$CURRENT_LINES"

# Clean up state file (this turn is done, next turn starts fresh)
tts_clear_watermark

if [[ "$CURRENT_LINES" -le "$WATERMARK" ]]; then
    tts_debug "Stop: no new lines since last PostToolUse speak"
    exit 0
fi

# --- Scan lines after the watermark for the last assistant text ---
LAST_ASSISTANT=""
if [[ "$WATERMARK" -gt 0 ]]; then
    # Only scan new lines (after what PostToolUse already processed)
    while IFS= read -r line; do
        TYPE=$(echo "$line" | jq -r '.type // empty' 2>/dev/null)
        if [[ "$TYPE" == "assistant" ]]; then
            HAS_TEXT=$(echo "$line" | jq -r '[.message.content[]? | select(.type == "text")] | length' 2>/dev/null || echo "0")
            if [[ "$HAS_TEXT" -gt 0 ]]; then
                LAST_ASSISTANT="$line"
            fi
        fi
    done < <(tail -n +"$((WATERMARK + 1))" "$TRANSCRIPT_PATH" 2>/dev/null)
else
    # No watermark (no PostToolUse fired this turn) -- scan entire transcript in reverse
    # for the last assistant message with text (original behavior)
    while IFS= read -r line; do
        TYPE=$(echo "$line" | jq -r '.type // empty' 2>/dev/null)
        if [[ "$TYPE" == "assistant" ]]; then
            HAS_TEXT=$(echo "$line" | jq -r '[.message.content[]? | select(.type == "text")] | length' 2>/dev/null || echo "0")
            if [[ "$HAS_TEXT" -gt 0 ]]; then
                LAST_ASSISTANT="$line"
                break
            fi
        fi
    done < <(utac "$TRANSCRIPT_PATH" 2>/dev/null || tail -r "$TRANSCRIPT_PATH" 2>/dev/null || tac "$TRANSCRIPT_PATH" 2>/dev/null)
fi

if [[ -z "$LAST_ASSISTANT" ]]; then
    tts_debug "Stop: no assistant message with text found"
    exit 0
fi
tts_debug "Stop: found assistant message with text (${#LAST_ASSISTANT} chars)"

# --- Extract text content ---
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
    tts_debug "Stop: no response content extracted"
    exit 0
fi
tts_debug "Stop: extracted response (${#RESPONSE} chars): ${RESPONSE:0:100}..."

# --- Filter and speak ---
CLIFF_NOTES=$(tts_filter_text "$RESPONSE")

if [[ -z "$CLIFF_NOTES" || ${#CLIFF_NOTES} -lt 10 ]]; then
    tts_debug "Stop: text too short after filtering, skipping"
    exit 0
fi

tts_speak "$CLIFF_NOTES"
tts_debug "Stop: speech queued/played"

exit 0
