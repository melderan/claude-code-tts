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
TTS_MUTE_FILE="/tmp/claude_tts_muted"

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
# Finds the most recently active Claude session whose project folder
# matches the current working directory. Falls back to PWD transformation.
# Prints the session ID to stdout.
get_session_id() {
    # Explicit override
    if [[ -n "${CLAUDE_TTS_SESSION:-}" ]]; then
        echo "$CLAUDE_TTS_SESSION"
        return
    fi

    local projects_dir="$HOME/.claude/projects"
    [[ -d "$projects_dir" ]] || { echo "unknown"; return; }

    # Strategy 1: Find the most recently modified transcript (.jsonl) whose
    # project folder contains our PWD. The active Claude session is writing
    # to its transcript right now, so it will be the most recent.
    #
    # We check: does the project folder name, when converted back to a path,
    # match as a prefix of PWD? Claude names projects by replacing / with -.
    local best_match=""
    local best_mtime=0

    for project_dir in "$projects_dir"/*/; do
        local project_name
        project_name=$(basename "$project_dir")

        # Find the most recent .jsonl in this project
        local latest_jsonl=""
        local latest_mtime=0
        for f in "$project_dir"*.jsonl; do
            [[ -f "$f" ]] || continue
            local mtime
            mtime=$(stat -f %m "$f" 2>/dev/null || stat -c %Y "$f" 2>/dev/null || echo 0)
            if (( mtime > latest_mtime )); then
                latest_mtime=$mtime
                latest_jsonl="$f"
            fi
        done

        # Skip projects with no recent transcript
        [[ -n "$latest_jsonl" ]] || continue

        # Check if this project matches our PWD.
        # Convert PWD to Claude format and see if it starts with project_name.
        local pwd_transformed
        pwd_transformed=$(echo "$PWD" | tr '/_.' '---')
        if [[ "$pwd_transformed" == "$project_name"* ]]; then
            if (( latest_mtime > best_mtime )); then
                best_mtime=$latest_mtime
                best_match="$project_name"
            fi
        fi
    done

    if [[ -n "$best_match" ]]; then
        echo "$best_match"
        return
    fi

    # Strategy 2: If PWD-based matching failed (worktrees, unusual paths),
    # find the most recently active session overall. When you run /tts-unmute,
    # YOUR session is the one actively writing a transcript right now.
    local now
    now=$(date +%s)
    best_match=""
    best_mtime=0

    for project_dir in "$projects_dir"/*/; do
        local project_name
        project_name=$(basename "$project_dir")
        for f in "$project_dir"*.jsonl; do
            [[ -f "$f" ]] || continue
            local mtime
            mtime=$(stat -f %m "$f" 2>/dev/null || stat -c %Y "$f" 2>/dev/null || echo 0)
            # Only consider transcripts active in the last 30 seconds
            local age=$(( now - mtime ))
            if (( age <= 30 && mtime > best_mtime )); then
                best_mtime=$mtime
                best_match="$project_name"
            fi
        done
    done

    if [[ -n "$best_match" ]]; then
        echo "$best_match"
        return
    fi

    # Strategy 3: Fall back to longest prefix match (original behavior)
    local pwd_transformed
    pwd_transformed=$(echo "$PWD" | tr '/_.' '---')
    best_match=""
    local best_length=0

    for project_dir in "$projects_dir"/*/; do
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

    echo "${best_match:-$pwd_transformed}"
}

# --- Load config from config.json ---
# Sets: TTS_MODE, TTS_MUTED, TTS_SPEED, TTS_SPEED_METHOD, TTS_VOICE,
#       TTS_MAX_CHARS, ACTIVE_PERSONA, PROJECT_NAME
tts_load_config() {
    # Defaults
    TTS_SPEED="${CLAUDE_TTS_SPEED:-2.0}"
    TTS_SPEED_METHOD=""
    TTS_VOICE="${CLAUDE_TTS_VOICE:-$HOME/.local/share/piper-voices/en_US-hfc_male-medium.onnx}"
    TTS_VOICE_KOKORO=""
    TTS_MAX_CHARS="${CLAUDE_TTS_MAX_CHARS:-10000}"
    TTS_MUTED="false"
    TTS_MODE="direct"
    PROJECT_NAME="session-${TTS_SESSION:0:8}"
    ACTIVE_PERSONA="claude-prime"

    if [[ -f "$TTS_CONFIG_FILE" ]]; then
        tts_debug "Loading config from $TTS_CONFIG_FILE"

        CONFIG_JSON=$(jq -r --arg s "$TTS_SESSION" '
            (.sessions[$s].persona // .project_personas[$s] // .active_persona // "default") as $persona |
            (if .sessions[$s] | has("muted") then .sessions[$s].muted
             elif .muted == true then true
             elif (.sessions[$s] == null) and (.default_muted == true) then true
             else false end) as $muted |
            [
                (.mode // "direct"),
                ($muted | tostring),
                $persona,
                (.sessions[$s].speed // "-" | tostring),
                (.personas[$persona].speed // "-" | tostring),
                (.personas[$persona].speed_method // "-"),
                (.personas[$persona].voice // "-"),
                (.personas[$persona].max_chars // "-" | tostring),
                (.personas[$persona].voice_kokoro // "-")
            ] | join("|")
        ' "$TTS_CONFIG_FILE" 2>/dev/null)

        if [[ -n "$CONFIG_JSON" ]]; then
            IFS='|' read -r TTS_MODE CONFIG_MUTED ACTIVE_PERSONA SESSION_SPEED \
                PERSONA_SPEED PERSONA_SPEED_METHOD PERSONA_VOICE PERSONA_MAX_CHARS \
                PERSONA_VOICE_KOKORO <<< "$CONFIG_JSON"
            # Normalize sentinels back to empty
            [[ "$SESSION_SPEED" == "-" ]] && SESSION_SPEED=""
            [[ "$PERSONA_SPEED" == "-" ]] && PERSONA_SPEED=""
            [[ "$PERSONA_SPEED_METHOD" == "-" ]] && PERSONA_SPEED_METHOD=""
            [[ "$PERSONA_VOICE" == "-" ]] && PERSONA_VOICE=""
            [[ "$PERSONA_MAX_CHARS" == "-" ]] && PERSONA_MAX_CHARS=""
            [[ "$PERSONA_VOICE_KOKORO" == "-" ]] && PERSONA_VOICE_KOKORO=""

            tts_debug "TTS mode: $TTS_MODE"
            tts_debug "Session: $TTS_SESSION"
            tts_debug "Active persona: $ACTIVE_PERSONA"

            if [[ "$CONFIG_MUTED" == "true" ]]; then
                TTS_MUTED="true"
                tts_debug "Session muted"
            fi

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

            tts_debug "Config loaded - speed:$TTS_SPEED method:$TTS_SPEED_METHOD max_chars:$TTS_MAX_CHARS kokoro:$TTS_VOICE_KOKORO"
        fi
    fi

    # Environment variables override config
    [[ -n "${CLAUDE_TTS_SPEED:-}" ]] && TTS_SPEED="$CLAUDE_TTS_SPEED"
    [[ -n "${CLAUDE_TTS_SPEED_METHOD:-}" ]] && TTS_SPEED_METHOD="$CLAUDE_TTS_SPEED_METHOD"
    [[ -n "${CLAUDE_TTS_VOICE:-}" ]] && TTS_VOICE="$CLAUDE_TTS_VOICE"
    [[ -n "${CLAUDE_TTS_MAX_CHARS:-}" ]] && TTS_MAX_CHARS="$CLAUDE_TTS_MAX_CHARS"
}

# --- Check early exit conditions ---
# Returns 0 (should exit) or 1 (continue)
tts_should_exit() {
    if [[ "${CLAUDE_TTS_ENABLED:-1}" != "1" ]]; then
        tts_debug "TTS disabled via env var"
        return 0
    fi
    if [[ -f "$TTS_MUTE_FILE" ]]; then
        tts_debug "TTS muted via mute file (legacy)"
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
    local text="$1"

    # Remove <thinking> blocks
    text=$(echo "$text" | perl -0777 -pe 's/<thinking>[\s\S]*?<\/thinking>//g')
    # Remove fenced code blocks
    text=$(echo "$text" | perl -0777 -pe 's/```[\s\S]*?```//g')
    # Remove indented code blocks
    text=$(echo "$text" | grep -v '^    ' || echo "$text")
    # Remove inline code
    text=$(echo "$text" | sed 's/`[^`]*`//g')
    # Remove markdown formatting
    text=$(echo "$text" | sed 's/^##* *//g')
    text=$(echo "$text" | sed 's/\*\*//g')
    text=$(echo "$text" | sed 's/\*//g')
    # Remove URL-only bullet lines
    text=$(echo "$text" | sed '/^[[:space:]]*[-*][[:space:]]*http/d')
    text=$(echo "$text" | sed '/^[[:space:]]*[-*][[:space:]]*\[.*\](http/d')
    # Replace markdown links with link text
    text=$(echo "$text" | sed 's/\[\([^]]*\)\]([^)]*)/ \1 /g')
    # Remove bare URLs
    text=$(echo "$text" | sed 's/https*:\/\/[^[:space:]]*//g')
    # Clean whitespace
    text=$(echo "$text" | tr '\n' ' ' | sed 's/  */ /g' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')

    echo "$text"
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

    cat > "$queue_file" << EOF
{
  "id": "$msg_id",
  "timestamp": $timestamp,
  "session_id": "$session_id",
  "project": "$project",
  "text": $(echo "$text" | jq -Rs .),
  "persona": "$persona"
}
EOF

    tts_debug "Wrote to queue: $queue_file"
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
        # Auto-start daemon if not running
        local pid_file="$HOME/.claude-tts/daemon.pid"
        local daemon_running=false

        if [[ -f "$pid_file" ]]; then
            local daemon_pid=$(cat "$pid_file" 2>/dev/null)
            if [[ -n "$daemon_pid" ]] && kill -0 "$daemon_pid" 2>/dev/null; then
                daemon_running=true
            fi
        fi

        if [[ "$daemon_running" == "false" ]]; then
            tts_debug "Daemon not running, starting it..."
            "$HOME/.claude-tts/tts-mode.sh" start >/dev/null 2>&1 &
            sleep 0.5
        fi

        tts_debug "Queue mode: writing to daemon queue"
        tts_write_queue "$text" "$TTS_SESSION" "$PROJECT_NAME" "${ACTIVE_PERSONA:-claude-prime}"
        return 0
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

    if command -v swift-kokoro &>/dev/null && [[ -n "$TTS_VOICE_KOKORO" ]]; then
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

tts_read_watermark() {
    local state_file=$(tts_state_file)
    if [[ -f "$state_file" ]]; then
        cat "$state_file" 2>/dev/null || echo "0"
    else
        echo "0"
    fi
}

tts_write_watermark() {
    local line_count="$1"
    echo "$line_count" > "$(tts_state_file)"
}

tts_clear_watermark() {
    rm -f "$(tts_state_file)"
}
