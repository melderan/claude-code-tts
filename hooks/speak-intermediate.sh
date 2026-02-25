#!/bin/bash
#
# speak-intermediate.sh - PostToolUse TTS hook for Claude Code
#
# Speaks intermediate text blocks between tool calls, so the user
# hears Claude's narration as it works rather than only at the end.
#
# Coordinates with speak-response.sh (Stop hook) via a watermark
# state file to prevent double-speaking.

set -uo pipefail

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

tts_debug "=== PostToolUse hook triggered ==="

# --- Read input and check early exits ---
tts_read_input
if tts_should_exit; then exit 0; fi

# --- Skip tool types that produce boilerplate narration ---
TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // empty' 2>/dev/null)
if [[ "$TOOL_NAME" == "Task" || "$TOOL_NAME" == "TodoWrite" ]]; then
    tts_debug "PostToolUse: skipping tool type $TOOL_NAME"
    exit 0
fi

# --- Detect session and load config ---
tts_detect_session
tts_load_config

# Exit if muted
if [[ "$TTS_MUTED" == "true" ]]; then
    tts_debug "PostToolUse: muted, skipping"
    exit 0
fi

# Exit if intermediate speech disabled for this session
if [[ "$TTS_INTERMEDIATE" == "false" ]]; then
    tts_debug "PostToolUse: intermediate disabled, skipping"
    exit 0
fi

# --- Check watermark: only process new transcript lines ---
WATERMARK=$(tts_read_watermark)
CURRENT_LINES=$(wc -l < "$TRANSCRIPT_PATH" | tr -d ' ')
tts_debug "PostToolUse: watermark=$WATERMARK current=$CURRENT_LINES"

if [[ "$CURRENT_LINES" -le "$WATERMARK" ]]; then
    tts_debug "PostToolUse: no new lines since last speak"
    exit 0
fi

# --- Scan new lines for the last assistant message with text ---
LAST_ASSISTANT=""
while IFS= read -r line; do
    TYPE=$(echo "$line" | jq -r '.type // empty' 2>/dev/null)
    if [[ "$TYPE" == "assistant" ]]; then
        HAS_TEXT=$(echo "$line" | jq -r '[.message.content[]? | select(.type == "text")] | length' 2>/dev/null || echo "0")
        if [[ "$HAS_TEXT" -gt 0 ]]; then
            LAST_ASSISTANT="$line"
            # Don't break -- we want the LAST text message in the new region
        fi
    fi
done < <(tail -n +"$((WATERMARK + 1))" "$TRANSCRIPT_PATH" 2>/dev/null)

if [[ -z "$LAST_ASSISTANT" ]]; then
    tts_debug "PostToolUse: no assistant text in new lines"
    exit 0
fi

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
    tts_debug "PostToolUse: no text extracted"
    exit 0
fi

tts_debug "PostToolUse: extracted ${#RESPONSE} chars: ${RESPONSE:0:100}..."

# --- Filter and speak ---
CLIFF_NOTES=$(tts_filter_text "$RESPONSE")

if [[ -z "$CLIFF_NOTES" || ${#CLIFF_NOTES} -lt 10 ]]; then
    tts_debug "PostToolUse: text too short after filtering"
    exit 0
fi

# Update watermark BEFORE speaking (prevent re-speak on failure)
tts_write_watermark "$CURRENT_LINES"
tts_debug "PostToolUse: watermark updated to $CURRENT_LINES"

tts_speak "$CLIFF_NOTES"
tts_debug "PostToolUse: speech queued/played"

exit 0
