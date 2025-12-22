#!/bin/bash
#
# tts-status.sh - Show TTS status for current session
#
# Displays: session ID, mute state, persona, mode, daemon status
#

set -euo pipefail

CONFIG_FILE="$HOME/.claude-tts/config.json"
PROJECTS_DIR="$HOME/.claude/projects"
PID_FILE="$HOME/.claude-tts/daemon.pid"

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
    echo "TTS not configured (no config.json)"
    exit 1
fi

# Read all config in one jq call - output as tab-separated for reliable parsing
read -r mode default_muted global_muted active_persona session_muted_set session_muted session_persona project_persona < <(
    jq -r --arg s "$SESSION" '
    [
        (.mode // "direct"),
        (.default_muted // false),
        (.muted // false),
        (.active_persona // "default"),
        (if .sessions[$s] | has("muted") then "yes" else "no" end),
        (if .sessions[$s] | has("muted") then .sessions[$s].muted else "unset" end),
        (.sessions[$s].persona // "unset"),
        (.project_personas[$s] // "unset")
    ] | @tsv
    ' "$CONFIG_FILE"
)

# Determine effective mute state
if [[ "$session_muted_set" == "yes" ]]; then
    effective_muted="$session_muted"
    mute_source="session"
elif [[ "$global_muted" == "true" ]]; then
    effective_muted="true"
    mute_source="global"
elif [[ "$default_muted" == "true" ]]; then
    effective_muted="true"
    mute_source="default_muted"
else
    effective_muted="false"
    mute_source="default"
fi

# Determine effective persona (session > project > global)
if [[ "$session_persona" != "unset" && -n "$session_persona" ]]; then
    effective_persona="$session_persona"
elif [[ "$project_persona" != "unset" && -n "$project_persona" ]]; then
    effective_persona="$project_persona"
else
    effective_persona="$active_persona"
fi

# Check daemon status
daemon_status="not running"
if [[ -f "$PID_FILE" ]]; then
    pid=$(cat "$PID_FILE" 2>/dev/null)
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
        daemon_status="running (PID $pid)"
    fi
fi

# Playback/pause state
PLAYBACK_FILE="$HOME/.claude-tts/playback.json"
if [[ -f "$PLAYBACK_FILE" ]]; then
    pause_state=$(jq -r '.paused // false' "$PLAYBACK_FILE")
    audio_pid=$(jq -r '.audio_pid // empty' "$PLAYBACK_FILE")
else
    pause_state="false"
    audio_pid=""
fi

# Output
echo "Session:  ${SESSION}"
echo "Muted:    ${effective_muted} (${mute_source})"
echo "Paused:   ${pause_state}"
if [[ -n "$audio_pid" ]] && kill -0 "$audio_pid" 2>/dev/null; then
    echo "Playing:  yes (PID $audio_pid)"
fi
echo "Persona:  ${effective_persona}"
echo "Mode:     ${mode}"
echo "Daemon:   ${daemon_status}"
