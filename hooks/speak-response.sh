#!/bin/bash
#
# speak-response.sh - TTS hook for Claude Code
#
# Reads Claude's responses aloud using Piper TTS (with macOS 'say' fallback).
# Designed to be triggered as a Claude Code "Stop" hook.
#
# PLATFORMS:
#   - macOS: Uses afplay for audio playback
#   - Linux: Uses paplay (PulseAudio) or aplay (ALSA)
#   - WSL 2: Uses paplay via WSLg's PulseAudio server
#
# CONFIGURATION (environment variables):
#   CLAUDE_TTS_ENABLED=1              Enable/disable TTS (default: 1)
#   CLAUDE_TTS_SPEED=2.0              Playback speed multiplier (default: 2.0)
#   CLAUDE_TTS_SPEED_METHOD=playback  Speed method: "playback" (chipmunk) or "length_scale" (natural)
#                                     Default: playback on macOS, length_scale on Linux/WSL
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
TTS_CONFIG_FILE="$HOME/.claude-tts/config.json"
TTS_QUEUE_DIR="$HOME/.claude-tts/queue"

# --- Queue Mode Support ---
# Write message to queue for daemon to process
write_to_queue() {
    local text="$1"
    local session_id="$2"
    local project="$3"
    local persona="$4"

    mkdir -p "$TTS_QUEUE_DIR"

    # Generate unique filename with timestamp
    local timestamp=$(date +%s.%N)
    local msg_id=$(head -c 8 /dev/urandom | xxd -p)
    local queue_file="$TTS_QUEUE_DIR/${timestamp}_${msg_id}.json"

    # Get project path from transcript
    local project_path=""
    if [[ -n "$TRANSCRIPT_PATH" ]]; then
        # Extract project path - transcript is at ~/.claude/projects/<hash>/<files>
        project_path=$(dirname "$(dirname "$TRANSCRIPT_PATH")")
    fi

    # Write queue message
    cat > "$queue_file" << EOF
{
  "id": "$msg_id",
  "timestamp": $timestamp,
  "session_id": "$session_id",
  "project": "$project",
  "project_path": "$project_path",
  "text": $(echo "$text" | jq -Rs .),
  "persona": "$persona"
}
EOF

    debug "Wrote to queue: $queue_file"
}
TTS_TEMP_DIR="/tmp"
TTS_MUTE_FILE="/tmp/claude_tts_muted"  # Legacy mute file for backward compat

# Exit early if TTS is disabled via env var
TTS_ENABLED="${CLAUDE_TTS_ENABLED:-1}"
if [[ "$TTS_ENABLED" != "1" ]]; then
    debug "TTS disabled via env var"
    exit 0
fi

# Legacy mute file check (fast exit before processing)
if [[ -f "$TTS_MUTE_FILE" ]]; then
    debug "TTS muted via mute file (legacy)"
    exit 0
fi

# --- Read hook input from stdin (need this early for session detection) ---
INPUT=$(cat)
debug "Input received: ${INPUT:0:200}"
TRANSCRIPT_PATH=$(echo "$INPUT" | jq -r '.transcript_path // empty')
debug "Transcript path: $TRANSCRIPT_PATH"

if [[ -z "$TRANSCRIPT_PATH" || ! -f "$TRANSCRIPT_PATH" ]]; then
    debug "No transcript path or file not found"
    exit 0
fi

# --- Auto-detect session from project hash ---
# Transcript path format: ~/.claude/projects/<HASH>/transcript.jsonl
# Extract the hash as the automatic session ID
TTS_SESSION="${CLAUDE_TTS_SESSION:-}"
if [[ -z "$TTS_SESSION" ]]; then
    # Auto-detect from transcript path
    PROJECT_HASH=$(echo "$TRANSCRIPT_PATH" | sed -n 's|.*/projects/\([^/]*\)/.*|\1|p')
    if [[ -n "$PROJECT_HASH" ]]; then
        TTS_SESSION="$PROJECT_HASH"
        debug "Auto-detected session from project: $TTS_SESSION"
    fi
fi

# --- Extract project name for queue messages ---
# Try to get a human-readable project name from the project directory
PROJECT_NAME="session-${TTS_SESSION:0:8}"
if [[ -n "$TRANSCRIPT_PATH" ]]; then
    PROJECT_DIR=$(dirname "$(dirname "$TRANSCRIPT_PATH")")
    if [[ -d "$PROJECT_DIR" ]]; then
        # Look for project name in various ways
        # 1. Try to read from a .project-name file if we ever create one
        # 2. Use the actual directory name from CWD stored in transcript
        # 3. Fall back to session hash
        CWD_FILE="$PROJECT_DIR/cwd"
        if [[ -f "$CWD_FILE" ]]; then
            FULL_PATH=$(cat "$CWD_FILE" 2>/dev/null)
            PROJECT_NAME=$(basename "$FULL_PATH" 2>/dev/null || echo "$PROJECT_NAME")
        fi
    fi
fi
debug "Project name: $PROJECT_NAME"

# Default values (can be overridden by config file or env vars)
TTS_SPEED="${CLAUDE_TTS_SPEED:-2.0}"
TTS_SPEED_METHOD=""  # Will be set based on platform later
TTS_VOICE="${CLAUDE_TTS_VOICE:-$HOME/.local/share/piper-voices/en_US-hfc_male-medium.onnx}"
TTS_MAX_CHARS="${CLAUDE_TTS_MAX_CHARS:-10000}"
TTS_MUTED="false"

# Load from config file if it exists
TTS_MODE="direct"  # Default to direct mode
PROJECT_NAME="unknown"

if [[ -f "$TTS_CONFIG_FILE" ]]; then
    debug "Loading config from $TTS_CONFIG_FILE"

    # Single jq call to extract all config values at once (much faster than multiple calls)
    # Output format: tab-separated values for reliable parsing
    CONFIG_JSON=$(jq -r --arg s "$TTS_SESSION" '
        # Determine effective persona: session > project > global
        (.sessions[$s].persona // .project_personas[$s] // .active_persona // "default") as $persona |
        # Check if session has explicit muted setting
        (if .sessions[$s] | has("muted") then .sessions[$s].muted
         elif .muted == true then true
         elif (.sessions[$s] == null) and (.default_muted == true) then true
         else false end) as $muted |
        [
            (.mode // "direct"),
            $muted,
            $persona,
            (.sessions[$s].speed // ""),
            (.personas[$persona].speed // ""),
            (.personas[$persona].speed_method // ""),
            (.personas[$persona].voice // ""),
            (.personas[$persona].max_chars // "")
        ] | @tsv
    ' "$TTS_CONFIG_FILE" 2>/dev/null)

    if [[ -n "$CONFIG_JSON" ]]; then
        IFS=$'\t' read -r TTS_MODE CONFIG_MUTED ACTIVE_PERSONA SESSION_SPEED \
            PERSONA_SPEED PERSONA_SPEED_METHOD PERSONA_VOICE PERSONA_MAX_CHARS <<< "$CONFIG_JSON"

        debug "TTS mode: $TTS_MODE"
        debug "Session: $TTS_SESSION"
        debug "Active persona: $ACTIVE_PERSONA"

        # Apply muted state
        if [[ "$CONFIG_MUTED" == "true" ]]; then
            TTS_MUTED="true"
            debug "Session muted"
        fi

        # Apply persona settings (if set)
        [[ -n "$PERSONA_SPEED" ]] && TTS_SPEED="$PERSONA_SPEED"
        [[ -n "$PERSONA_SPEED_METHOD" ]] && TTS_SPEED_METHOD="$PERSONA_SPEED_METHOD"
        [[ -n "$PERSONA_MAX_CHARS" ]] && TTS_MAX_CHARS="$PERSONA_MAX_CHARS"

        # Session speed override takes priority over persona
        if [[ -n "$SESSION_SPEED" ]]; then
            TTS_SPEED="$SESSION_SPEED"
            debug "Using session speed override: $SESSION_SPEED"
        fi

        # Voice needs path expansion
        if [[ -n "$PERSONA_VOICE" ]]; then
            TTS_VOICE="$HOME/.local/share/piper-voices/${PERSONA_VOICE}.onnx"
        fi

        debug "Config loaded - speed:$TTS_SPEED method:$TTS_SPEED_METHOD max_chars:$TTS_MAX_CHARS"
    else
        debug "Failed to parse config, using defaults"
    fi
else
    debug "No config file, using defaults/env vars"
fi

# Environment variables override config file
[[ -n "${CLAUDE_TTS_SPEED:-}" ]] && TTS_SPEED="$CLAUDE_TTS_SPEED"
[[ -n "${CLAUDE_TTS_SPEED_METHOD:-}" ]] && TTS_SPEED_METHOD="$CLAUDE_TTS_SPEED_METHOD"
[[ -n "${CLAUDE_TTS_VOICE:-}" ]] && TTS_VOICE="$CLAUDE_TTS_VOICE"
[[ -n "${CLAUDE_TTS_MAX_CHARS:-}" ]] && TTS_MAX_CHARS="$CLAUDE_TTS_MAX_CHARS"

# Exit if muted via config (checked after session detection)
if [[ "$TTS_MUTED" == "true" ]]; then
    debug "TTS muted via config (session: ${TTS_SESSION:-global})"
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
done < <(utac "$TRANSCRIPT_PATH" 2>/dev/null || tail -r "$TRANSCRIPT_PATH" 2>/dev/null || tac "$TRANSCRIPT_PATH" 2>/dev/null)

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
# Remove <thinking> blocks (internal reasoning, not for speech)
CLIFF_NOTES=$(echo "$RESPONSE" | perl -0777 -pe 's/<thinking>[\s\S]*?<\/thinking>//g')

# Remove fenced code blocks (```...```)
CLIFF_NOTES=$(echo "$CLIFF_NOTES" | perl -0777 -pe 's/```[\s\S]*?```//g')

# Remove indented code blocks (4+ spaces at start of line)
CLIFF_NOTES=$(echo "$CLIFF_NOTES" | grep -v '^    ' || echo "$CLIFF_NOTES")

# Remove inline code (`...`)
CLIFF_NOTES=$(echo "$CLIFF_NOTES" | sed 's/`[^`]*`//g')

# Remove markdown formatting for cleaner speech (BSD sed compatible)
CLIFF_NOTES=$(echo "$CLIFF_NOTES" | sed 's/^##* *//g')             # Headers
CLIFF_NOTES=$(echo "$CLIFF_NOTES" | sed 's/\*\*//g')               # Bold markers
CLIFF_NOTES=$(echo "$CLIFF_NOTES" | sed 's/\*//g')                 # Italic markers

# Remove lines that are just bullet points with URLs (plain or markdown links)
# This catches "- https://..." and "- [text](url)" patterns
CLIFF_NOTES=$(echo "$CLIFF_NOTES" | sed '/^[[:space:]]*[-*][[:space:]]*http/d')
CLIFF_NOTES=$(echo "$CLIFF_NOTES" | sed '/^[[:space:]]*[-*][[:space:]]*\[.*\](http/d')

# Replace remaining markdown links with just the link text (not "removed_link")
CLIFF_NOTES=$(echo "$CLIFF_NOTES" | sed 's/\[\([^]]*\)\]([^)]*)/ \1 /g')

# Remove any bare URLs that might remain
CLIFF_NOTES=$(echo "$CLIFF_NOTES" | sed 's/https*:\/\/[^[:space:]]*//g')

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

# --- Queue mode: write to daemon queue and exit ---
if [[ "$TTS_MODE" == "queue" ]]; then
    # Auto-start daemon if not running
    TTS_PID_FILE="$HOME/.claude-tts/daemon.pid"
    DAEMON_RUNNING=false

    if [[ -f "$TTS_PID_FILE" ]]; then
        DAEMON_PID=$(cat "$TTS_PID_FILE" 2>/dev/null)
        if [[ -n "$DAEMON_PID" ]] && kill -0 "$DAEMON_PID" 2>/dev/null; then
            DAEMON_RUNNING=true
        fi
    fi

    if [[ "$DAEMON_RUNNING" == "false" ]]; then
        debug "Daemon not running, starting it..."
        "$HOME/.claude-tts/tts-mode.sh" start >/dev/null 2>&1 &
        sleep 0.5  # Brief wait for daemon to start
    fi

    debug "Queue mode: writing to daemon queue"
    write_to_queue "$CLIFF_NOTES" "$TTS_SESSION" "$PROJECT_NAME" "${ACTIVE_PERSONA:-claude-prime}"
    exit 0
fi

# --- Direct mode: generate and play immediately ---

# --- Rotating temp file (5 slots to avoid file conflicts) ---
SLOT=$(( $(date +%s) % 5 ))
TEMP_FILE="${TTS_TEMP_DIR}/claude_tts_${SLOT}.wav"

# --- Detect platform ---
PLATFORM="unknown"
if [[ "$(uname)" == "Darwin" ]]; then
    PLATFORM="macos"
elif [[ -f /proc/version ]] && grep -qi microsoft /proc/version 2>/dev/null; then
    PLATFORM="wsl"
elif [[ "$(uname)" == "Linux" ]]; then
    PLATFORM="linux"
fi
debug "Detected platform: $PLATFORM"

# --- Speed method configuration ---
# "playback" = generate normal, speed up with afplay -r (old voice, macOS only)
# "length_scale" = generate fast with Piper --length_scale (new voice, all platforms)
# Default: playback on macOS (old voice), length_scale elsewhere (only option)
if [[ -z "$TTS_SPEED_METHOD" ]]; then
    # Not set by config or env var, use platform default
    if [[ "$PLATFORM" == "macos" ]]; then
        TTS_SPEED_METHOD="playback"
    else
        TTS_SPEED_METHOD="length_scale"
    fi
fi
debug "Speed method: $TTS_SPEED_METHOD"

# --- Generate and play audio ---
debug "Final text (${#CLIFF_NOTES} chars): ${CLIFF_NOTES:0:100}..."

if command -v piper &>/dev/null; then
    debug "Using piper for TTS"

    if [[ "$TTS_SPEED_METHOD" == "length_scale" ]]; then
        # Generate audio at target speed (natural pitch)
        LENGTH_SCALE=$(awk "BEGIN {printf \"%.2f\", 1.0 / $TTS_SPEED}")
        debug "Speed $TTS_SPEED -> length_scale $LENGTH_SCALE"
        echo "$CLIFF_NOTES" | piper --model "$TTS_VOICE" --length_scale "$LENGTH_SCALE" --output_file "$TEMP_FILE" 2>/dev/null
    else
        # Generate at normal speed (will speed up during playback)
        debug "Generating at normal speed for playback speedup"
        echo "$CLIFF_NOTES" | piper --model "$TTS_VOICE" --output_file "$TEMP_FILE" 2>/dev/null
    fi

    if [[ -f "$TEMP_FILE" ]]; then
        debug "Playing audio: $TEMP_FILE"

        if command -v afplay &>/dev/null; then
            # macOS - can speed up during playback
            if [[ "$TTS_SPEED_METHOD" == "playback" ]]; then
                afplay -r "$TTS_SPEED" "$TEMP_FILE" 2>/dev/null &
            else
                afplay "$TEMP_FILE" 2>/dev/null &
            fi
        elif command -v paplay &>/dev/null; then
            # PulseAudio (Linux native, WSL 2 via WSLg)
            paplay "$TEMP_FILE" 2>/dev/null &
        elif command -v aplay &>/dev/null; then
            # ALSA fallback
            aplay -q "$TEMP_FILE" 2>/dev/null &
        else
            debug "ERROR: No audio player found (need afplay, paplay, or aplay)"
        fi
    else
        debug "ERROR: Failed to generate audio file"
    fi
else
    # Fallback to macOS 'say' command (rate in words per minute, ~200 is normal)
    if command -v say &>/dev/null; then
        RATE=$(awk "BEGIN {printf \"%.0f\", $TTS_SPEED * 200}")
        debug "Using macOS say with rate $RATE"
        say -r "$RATE" "$CLIFF_NOTES" &
    else
        debug "ERROR: No TTS engine available (need piper or say)"
    fi
fi

exit 0
