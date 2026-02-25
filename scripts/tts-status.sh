#!/bin/bash
#
# tts-status.sh - Show TTS status for current session
#
# Displays: session ID, mute state, persona, mode, daemon status
#

set -euo pipefail

# shellcheck source=tts-lib.sh
source "$HOME/.claude-tts/tts-lib.sh" 2>/dev/null || source "$(dirname "$0")/tts-lib.sh"

CONFIG_FILE="$TTS_CONFIG_FILE"
PID_FILE="$HOME/.claude-tts/daemon.pid"

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
