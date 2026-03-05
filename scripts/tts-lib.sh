#!/bin/bash
#
# tts-lib.sh - Shared library for TTS hooks
#
# Sourced by speak-response.sh (Stop) and speak-intermediate.sh (PostToolUse)
# to avoid duplicating config loading, text filtering, and speech routing.

# --- Debug logging ---
DEBUG_LOG="/tmp/claude_tts_debug.log"
tts_debug() {
    echo "[$(date '+%H:%M:%S')] $*" >> "$DEBUG_LOG"
}

# --- Configuration ---
TTS_CONFIG_FILE="$HOME/.claude-tts/config.json"
TTS_QUEUE_DIR="$HOME/.claude-tts/queue"
TTS_SESSIONS_DIR="$HOME/.claude-tts/sessions.d"

# --- Read hook input and extract transcript path ---
# Sets: INPUT, TRANSCRIPT_PATH
tts_read_input() {
    INPUT=$(cat)
    tts_debug "Input received: ${INPUT:0:200}"
    TRANSCRIPT_PATH=$(echo "$INPUT" | jq -r '.transcript_path // empty')
    tts_debug "Transcript path: $TRANSCRIPT_PATH"
}

# --- Detect session from transcript path (used by hooks) ---
# Sets: TTS_SESSION
tts_detect_session() {
    TTS_SESSION="${CLAUDE_TTS_SESSION:-}"
    if [[ -z "$TTS_SESSION" ]]; then
        TTS_SESSION=$(echo "$TRANSCRIPT_PATH" | sed -n 's|.*/projects/\([^/]*\)/.*|\1|p')
        if [[ -n "$TTS_SESSION" ]]; then
            tts_debug "Auto-detected session: $TTS_SESSION"
        fi
    fi
}

# --- Detect session from PWD (used by command scripts) ---
# Looks up the actual Claude Code project folder name from ~/.claude/projects/
# rather than re-deriving it, because Claude Code's path encoding isn't a
# simple tr '/' '-' (it also strips underscores and possibly other chars).
# Prints the session ID to stdout.
get_session_id() {
    # Explicit override
    if [[ -n "${CLAUDE_TTS_SESSION:-}" ]]; then
        echo "$CLAUDE_TTS_SESSION"
        return
    fi

    if [[ -n "${PROJECT_ROOT:-}" ]]; then
        local projects_dir="$HOME/.claude/projects"
        if [[ -d "$projects_dir" ]]; then
            # Compare alphanumeric content to match regardless of encoding quirks
            local target
            target=$(echo "$PROJECT_ROOT" | tr -dc 'a-zA-Z0-9')
            for dir in "$projects_dir"/*/; do
                [[ -d "$dir" ]] || continue
                local folder
                folder=$(basename "$dir")
                local candidate
                candidate=$(echo "$folder" | tr -dc 'a-zA-Z0-9')
                if [[ "$candidate" == "$target" ]]; then
                    echo "$folder"
                    return
                fi
            done
        fi
        # Fallback if ~/.claude/projects/ lookup fails
        echo "$PROJECT_ROOT" | tr '/' '-'
        return
    fi

    # Fallback for non-Claude-Code contexts (standalone terminal usage).
    echo "$PWD" | tr '/' '-'
}

# --- Session file helpers (sessions.d/) ---

# Get the session file path for a given session ID
tts_session_file() {
    echo "$TTS_SESSIONS_DIR/${1}.json"
}

# Set a key=value in a session file (creates file and dir if needed)
# Usage: tts_session_set <session_id> <key> <value> [type]
# type: "string" (default), "bool", "number"
tts_session_set() {
    local session="$1" key="$2" value="$3" type="${4:-string}"
    local file="$TTS_SESSIONS_DIR/${session}.json"
    mkdir -p "$TTS_SESSIONS_DIR"

    local existing='{}'
    [[ -f "$file" ]] && existing=$(cat "$file")

    if [[ "$type" == "bool" || "$type" == "number" ]]; then
        echo "$existing" | jq --arg k "$key" --argjson v "$value" '.[$k] = $v' > "$file.tmp"
    else
        echo "$existing" | jq --arg k "$key" --arg v "$value" '.[$k] = $v' > "$file.tmp"
    fi
    mv "$file.tmp" "$file"
}

# Delete a key from a session file
tts_session_del() {
    local session="$1" key="$2"
    local file="$TTS_SESSIONS_DIR/${session}.json"
    [[ -f "$file" ]] || return 0
    jq --arg k "$key" 'del(.[$k])' "$file" > "$file.tmp" && mv "$file.tmp" "$file"
}

# Migrate a single session from legacy config.json .sessions to sessions.d/
# Returns 0 if migration happened, 1 if no legacy data
_tts_migrate_session() {
    local session="$1"
    local file="$TTS_SESSIONS_DIR/${session}.json"

    # Skip if already migrated
    [[ -f "$file" ]] && return 1

    # Check for legacy data in config.json
    local legacy
    legacy=$(jq -r --arg s "$session" '
        if .sessions[$s] then .sessions[$s] | tojson else "null" end
    ' "$TTS_CONFIG_FILE" 2>/dev/null)

    if [[ "$legacy" != "null" && -n "$legacy" ]]; then
        mkdir -p "$TTS_SESSIONS_DIR"
        echo "$legacy" > "$file"
        # Remove from config.json
        jq --arg s "$session" 'del(.sessions[$s])' "$TTS_CONFIG_FILE" > "$TTS_CONFIG_FILE.tmp" \
            && mv "$TTS_CONFIG_FILE.tmp" "$TTS_CONFIG_FILE"
        tts_debug "Migrated session $session from config.json to sessions.d/"
        return 0
    fi
    return 1
}

# Auto-cleanup stale session files (throttled to once per hour)
# Logs removed sessions with full JSON for restoration
_tts_maybe_cleanup() {
    local marker="$TTS_SESSIONS_DIR/.last_cleanup"
    local now
    now=$(date +%s)

    if [[ -f "$marker" ]]; then
        local last
        last=$(cat "$marker" 2>/dev/null || echo 0)
        (( now - last < 3600 )) && return
    fi

    mkdir -p "$TTS_SESSIONS_DIR"
    echo "$now" > "$marker"

    local projects_dir="$HOME/.claude/projects"
    [[ -d "$projects_dir" ]] || return

    for session_file in "$TTS_SESSIONS_DIR"/*.json; do
        [[ -f "$session_file" ]] || continue
        local session_id
        session_id=$(basename "$session_file" .json)
        if [[ ! -d "$projects_dir/$session_id" ]]; then
            local content
            content=$(cat "$session_file")
            tts_debug "Auto-cleanup: removing $session_id | restore: echo '$content' > $session_file"
            rm -f "$session_file"
        fi
    done
}

# --- Load config from config.json + sessions.d/ ---
# Sets: TTS_MODE, TTS_MUTED, TTS_INTERMEDIATE, TTS_SPEED, TTS_SPEED_METHOD,
#       TTS_VOICE, TTS_MAX_CHARS, ACTIVE_PERSONA, PROJECT_NAME
tts_load_config() {
    # Defaults
    TTS_SPEED="${CLAUDE_TTS_SPEED:-2.0}"
    TTS_SPEED_METHOD=""
    TTS_VOICE="${CLAUDE_TTS_VOICE:-$HOME/.local/share/piper-voices/en_US-hfc_male-medium.onnx}"
    TTS_VOICE_KOKORO=""
    TTS_VOICE_KOKORO_BLEND=""
    TTS_MAX_CHARS="${CLAUDE_TTS_MAX_CHARS:-10000}"
    TTS_MUTED="false"
    TTS_INTERMEDIATE="true"
    TTS_MODE="direct"
    PROJECT_NAME="session-${TTS_SESSION:0:8}"
    ACTIVE_PERSONA="claude-prime"

    # --- Step 1: Read session-specific config from sessions.d/ ---
    local SESSION_MUTED="" SESSION_PERSONA="" SESSION_SPEED="" SESSION_INTERMEDIATE=""
    local session_file="$TTS_SESSIONS_DIR/${TTS_SESSION}.json"

    if [[ ! -f "$session_file" ]]; then
        # Try migrating from legacy config.json .sessions
        _tts_migrate_session "$TTS_SESSION" 2>/dev/null || true
    fi

    if [[ -f "$session_file" ]]; then
        local session_json
        session_json=$(jq -r '[
            (.muted // "-" | tostring),
            (.persona // "-"),
            (.speed // "-" | tostring),
            (.intermediate // "-" | tostring)
        ] | join("|")' "$session_file" 2>/dev/null)

        if [[ -n "$session_json" ]]; then
            IFS='|' read -r SESSION_MUTED SESSION_PERSONA SESSION_SPEED SESSION_INTERMEDIATE <<< "$session_json"
            [[ "$SESSION_MUTED" == "-" ]] && SESSION_MUTED=""
            [[ "$SESSION_PERSONA" == "-" ]] && SESSION_PERSONA=""
            [[ "$SESSION_SPEED" == "-" ]] && SESSION_SPEED=""
            [[ "$SESSION_INTERMEDIATE" == "-" ]] && SESSION_INTERMEDIATE=""
        fi
    fi

    # --- Step 2: Read global config from config.json ---
    if [[ -f "$TTS_CONFIG_FILE" ]]; then
        tts_debug "Loading config from $TTS_CONFIG_FILE"

        # Determine the effective persona: session > project > global
        local persona_arg="${SESSION_PERSONA:-}"
        CONFIG_JSON=$(jq -r --arg s "$TTS_SESSION" --arg sp "$persona_arg" '
            (if $sp != "" then $sp else (.project_personas[$s] // .active_persona // "default") end) as $persona |
            [
                (.mode // "direct"),
                (.muted // false | tostring),
                (.default_muted // false | tostring),
                $persona,
                (.personas[$persona].speed // "-" | tostring),
                (.personas[$persona].speed_method // "-"),
                (.personas[$persona].voice // "-"),
                (.personas[$persona].max_chars // "-" | tostring),
                (.personas[$persona].voice_kokoro // "-"),
                (.personas[$persona].voice_kokoro_blend // "-")
            ] | join("|")
        ' "$TTS_CONFIG_FILE" 2>/dev/null)

        if [[ -n "$CONFIG_JSON" ]]; then
            local GLOBAL_MUTED DEFAULT_MUTED
            IFS='|' read -r TTS_MODE GLOBAL_MUTED DEFAULT_MUTED ACTIVE_PERSONA \
                PERSONA_SPEED PERSONA_SPEED_METHOD PERSONA_VOICE PERSONA_MAX_CHARS \
                PERSONA_VOICE_KOKORO PERSONA_VOICE_KOKORO_BLEND <<< "$CONFIG_JSON"
            # Normalize sentinels back to empty
            [[ "$PERSONA_SPEED" == "-" ]] && PERSONA_SPEED=""
            [[ "$PERSONA_SPEED_METHOD" == "-" ]] && PERSONA_SPEED_METHOD=""
            [[ "$PERSONA_VOICE" == "-" ]] && PERSONA_VOICE=""
            [[ "$PERSONA_MAX_CHARS" == "-" ]] && PERSONA_MAX_CHARS=""
            [[ "$PERSONA_VOICE_KOKORO" == "-" ]] && PERSONA_VOICE_KOKORO=""
            [[ "$PERSONA_VOICE_KOKORO_BLEND" == "-" ]] && PERSONA_VOICE_KOKORO_BLEND=""

            tts_debug "TTS mode: $TTS_MODE"
            tts_debug "Session: $TTS_SESSION"
            tts_debug "Active persona: $ACTIVE_PERSONA"

            # --- Step 3: Determine mute state ---
            # Priority: session-level mute > global mute > default_muted for new sessions
            if [[ -n "$SESSION_MUTED" ]]; then
                [[ "$SESSION_MUTED" == "true" ]] && TTS_MUTED="true"
            elif [[ "$GLOBAL_MUTED" == "true" ]]; then
                TTS_MUTED="true"
            elif [[ "$DEFAULT_MUTED" == "true" && ! -f "$session_file" ]]; then
                TTS_MUTED="true"
            fi

            if [[ "$TTS_MUTED" == "true" ]]; then
                tts_debug "Session muted"
            fi

            # --- Step 4: Merge persona and session overrides ---
            [[ -n "$PERSONA_SPEED" ]] && TTS_SPEED="$PERSONA_SPEED"
            [[ -n "$PERSONA_SPEED_METHOD" ]] && TTS_SPEED_METHOD="$PERSONA_SPEED_METHOD"
            [[ -n "$PERSONA_MAX_CHARS" ]] && TTS_MAX_CHARS="$PERSONA_MAX_CHARS"

            if [[ -n "$SESSION_SPEED" ]]; then
                TTS_SPEED="$SESSION_SPEED"
                tts_debug "Using session speed override: $SESSION_SPEED"
            fi

            if [[ -n "$PERSONA_VOICE" ]]; then
                TTS_VOICE="$HOME/.local/share/piper-voices/${PERSONA_VOICE}.onnx"
            fi

            [[ -n "$PERSONA_VOICE_KOKORO" ]] && TTS_VOICE_KOKORO="$PERSONA_VOICE_KOKORO"
            [[ -n "$PERSONA_VOICE_KOKORO_BLEND" ]] && TTS_VOICE_KOKORO_BLEND="$PERSONA_VOICE_KOKORO_BLEND"

            if [[ "$SESSION_INTERMEDIATE" == "false" ]]; then
                TTS_INTERMEDIATE="false"
            fi

            tts_debug "Config loaded - speed:$TTS_SPEED method:$TTS_SPEED_METHOD max_chars:$TTS_MAX_CHARS kokoro:$TTS_VOICE_KOKORO"
        fi
    fi

    # Environment variables override config
    [[ -n "${CLAUDE_TTS_SPEED:-}" ]] && TTS_SPEED="$CLAUDE_TTS_SPEED"
    [[ -n "${CLAUDE_TTS_SPEED_METHOD:-}" ]] && TTS_SPEED_METHOD="$CLAUDE_TTS_SPEED_METHOD"
    [[ -n "${CLAUDE_TTS_VOICE:-}" ]] && TTS_VOICE="$CLAUDE_TTS_VOICE"
    [[ -n "${CLAUDE_TTS_MAX_CHARS:-}" ]] && TTS_MAX_CHARS="$CLAUDE_TTS_MAX_CHARS"

    # Opportunistic cleanup (background, throttled)
    _tts_maybe_cleanup &>/dev/null &
}

# --- Check early exit conditions ---
# Returns 0 (should exit) or 1 (continue)
tts_should_exit() {
    if [[ "${CLAUDE_TTS_ENABLED:-1}" != "1" ]]; then
        tts_debug "TTS disabled via env var"
        return 0
    fi
    if [[ -z "$TRANSCRIPT_PATH" || ! -f "$TRANSCRIPT_PATH" ]]; then
        tts_debug "No transcript path or file not found"
        return 0
    fi
    return 1
}

# --- Filter text for speech ---
# Takes raw text as $1, prints filtered text to stdout
tts_filter_text() {
    echo "$1" | uv run --python 3.12 "$HOME/.claude-tts/tts-filter.py"
}

# --- Write to queue for daemon ---
tts_write_queue() {
    local text="$1"
    local session_id="$2"
    local project="$3"
    local persona="$4"

    mkdir -p "$TTS_QUEUE_DIR"

    local timestamp=$(date +%s.%N)
    local msg_id=$(head -c 8 /dev/urandom | xxd -p)
    local queue_file="$TTS_QUEUE_DIR/${timestamp}_${msg_id}.json"

    # Include resolved speed and method so the daemon respects session overrides
    cat > "$queue_file" << EOF
{
  "id": "$msg_id",
  "timestamp": $timestamp,
  "session_id": "$session_id",
  "project": "$project",
  "text": $(echo "$text" | jq -Rs .),
  "persona": "$persona",
  "speed": ${TTS_SPEED:-2.0},
  "speed_method": "${TTS_SPEED_METHOD:-playback}",
  "voice_kokoro": "${TTS_VOICE_KOKORO:-}",
  "voice_kokoro_blend": "${TTS_VOICE_KOKORO_BLEND:-}"
}
EOF

    tts_debug "Wrote to queue: $queue_file (speed=${TTS_SPEED:-2.0})"
}

# --- Speak text (queue or direct) ---
tts_speak() {
    local text="$1"

    # Truncate to max chars
    if [[ ${#text} -gt $TTS_MAX_CHARS ]]; then
        text="${text:0:$TTS_MAX_CHARS}..."
    fi

    # Queue mode
    if [[ "$TTS_MODE" == "queue" ]]; then
        # Check daemon health: alive AND heartbeating
        local pid_file="$HOME/.claude-tts/daemon.pid"
        local heartbeat_file="$HOME/.claude-tts/daemon.heartbeat"
        local daemon_healthy=false

        if [[ -f "$pid_file" ]]; then
            local daemon_pid=$(cat "$pid_file" 2>/dev/null)
            if [[ -n "$daemon_pid" ]] && kill -0 "$daemon_pid" 2>/dev/null; then
                if [[ -f "$heartbeat_file" ]]; then
                    local last_beat=$(cat "$heartbeat_file" 2>/dev/null)
                    local now=$(date +%s)
                    local age=$(( now - ${last_beat%%.*} ))
                    if (( age < 30 )); then
                        daemon_healthy=true
                    fi
                else
                    daemon_healthy=true
                fi
            fi
        fi

        if [[ "$daemon_healthy" == "false" ]]; then
            tts_debug "Daemon not healthy, skipping speech (no fallback to direct mode)"
            return 0
        else
            tts_debug "Queue mode: writing to daemon queue"
            tts_write_queue "$text" "$TTS_SESSION" "$PROJECT_NAME" "${ACTIVE_PERSONA:-claude-prime}"
            return 0
        fi
    fi

    # Direct mode
    local platform="unknown"
    if [[ "$(uname)" == "Darwin" ]]; then
        platform="macos"
    elif [[ -f /proc/version ]] && grep -qi microsoft /proc/version 2>/dev/null; then
        platform="wsl"
    elif [[ "$(uname)" == "Linux" ]]; then
        platform="linux"
    fi

    if [[ -z "$TTS_SPEED_METHOD" ]]; then
        if [[ "$platform" == "macos" ]]; then
            TTS_SPEED_METHOD="playback"
        else
            TTS_SPEED_METHOD="length_scale"
        fi
    fi

    local slot=$(( $(date +%s) % 5 ))
    local temp_file="/tmp/claude_tts_${slot}.wav"

    if command -v swift-kokoro &>/dev/null && [[ -n "$TTS_VOICE_KOKORO_BLEND" ]]; then
        # Swift Kokoro blend backend
        echo "$text" | swift-kokoro --blend "$TTS_VOICE_KOKORO_BLEND" --output "$temp_file" 2>/dev/null

        if [[ -f "$temp_file" ]]; then
            if command -v afplay &>/dev/null; then
                afplay -r "$TTS_SPEED" "$temp_file" 2>/dev/null &
            fi
        fi
    elif command -v swift-kokoro &>/dev/null && [[ -n "$TTS_VOICE_KOKORO" ]]; then
        # Swift Kokoro backend (CoreML, Apple Silicon native)
        echo "$text" | swift-kokoro --voice "$TTS_VOICE_KOKORO" --output "$temp_file" 2>/dev/null

        if [[ -f "$temp_file" ]]; then
            if command -v afplay &>/dev/null; then
                afplay -r "$TTS_SPEED" "$temp_file" 2>/dev/null &
            fi
        fi
    elif command -v piper &>/dev/null; then
        if [[ "$TTS_SPEED_METHOD" == "length_scale" ]]; then
            local length_scale=$(awk "BEGIN {printf \"%.2f\", 1.0 / $TTS_SPEED}")
            echo "$text" | piper --model "$TTS_VOICE" --length_scale "$length_scale" --output_file "$temp_file" 2>/dev/null
        else
            echo "$text" | piper --model "$TTS_VOICE" --output_file "$temp_file" 2>/dev/null
        fi

        if [[ -f "$temp_file" ]]; then
            if command -v afplay &>/dev/null; then
                if [[ "$TTS_SPEED_METHOD" == "playback" ]]; then
                    afplay -r "$TTS_SPEED" "$temp_file" 2>/dev/null &
                else
                    afplay "$temp_file" 2>/dev/null &
                fi
            elif command -v paplay &>/dev/null; then
                paplay "$temp_file" 2>/dev/null &
            elif command -v aplay &>/dev/null; then
                aplay -q "$temp_file" 2>/dev/null &
            fi
        fi
    elif command -v say &>/dev/null; then
        local rate=$(awk "BEGIN {printf \"%.0f\", $TTS_SPEED * 200}")
        say -r "$rate" "$text" &
    fi
}

# --- Watermark state tracking ---
TTS_STATE_DIR="/tmp"

tts_state_file() {
    echo "${TTS_STATE_DIR}/claude_tts_spoken_${TTS_SESSION}.state"
}

# Atomic mkdir-based lock (works on macOS and Linux, no flock needed)
_tts_watermark_lock() {
    local lockdir="${TTS_STATE_DIR}/claude_tts_wm_${TTS_SESSION}.lock"
    local attempts=0
    while ! mkdir "$lockdir" 2>/dev/null; do
        attempts=$((attempts + 1))
        if (( attempts > 20 )); then
            tts_debug "Watermark lock timeout, breaking stale lock"
            rm -rf "$lockdir"
            mkdir "$lockdir" 2>/dev/null || true
            break
        fi
        sleep 0.05
    done
}

_tts_watermark_unlock() {
    local lockdir="${TTS_STATE_DIR}/claude_tts_wm_${TTS_SESSION}.lock"
    rm -rf "$lockdir" 2>/dev/null || true
}

tts_read_watermark() {
    _tts_watermark_lock
    local state_file=$(tts_state_file)
    local wm=0
    if [[ -f "$state_file" ]]; then
        wm=$(cat "$state_file" 2>/dev/null || echo "0")
    fi

    # Auto-reset stale watermark: if the transcript is shorter than the
    # watermark, a new session started in the same project. Reset to 0
    # so we don't skip every message in the new conversation.
    if [[ -n "$TRANSCRIPT_PATH" && -f "$TRANSCRIPT_PATH" ]]; then
        local current
        current=$(wc -l < "$TRANSCRIPT_PATH" | tr -d ' ')
        if (( wm > current )); then
            tts_debug "Watermark reset: was $wm but transcript only has $current lines (new session)"
            wm=0
            echo "0" > "$state_file"
        fi
    fi

    _tts_watermark_unlock
    echo "$wm"
}

tts_write_watermark() {
    _tts_watermark_lock
    local line_count="$1"
    echo "$line_count" > "$(tts_state_file)"
    _tts_watermark_unlock
}

tts_clear_watermark() {
    rm -f "$(tts_state_file)"
}
