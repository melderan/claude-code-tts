#!/bin/bash
#
# tts-mode.sh - Manage TTS mode and daemon
#
# Usage: tts-mode.sh [command]
#   (no args)  - Show current mode and status
#   direct     - Switch to direct mode
#   queue      - Switch to queue mode
#   start      - Start the daemon
#   stop       - Stop the daemon
#   restart    - Restart the daemon
#   status     - Show detailed status
#   install    - Install system service
#   logs       - Show daemon logs
#

set -euo pipefail

CONFIG_FILE="$HOME/.claude-tts/config.json"
DAEMON_SCRIPT="$HOME/.claude-tts/tts-daemon.py"
PID_FILE="$HOME/.claude-tts/daemon.pid"
QUEUE_DIR="$HOME/.claude-tts/queue"

# macOS launchd
PLIST_SRC="$HOME/.claude-tts/services/com.claude-tts.daemon.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.claude-tts.daemon.plist"
LAUNCHD_LABEL="com.claude-tts.daemon"

# Linux systemd
SYSTEMD_SRC="$HOME/.claude-tts/services/claude-tts.service"
SYSTEMD_DST="$HOME/.config/systemd/user/claude-tts.service"
SYSTEMD_SERVICE="claude-tts"

# Detect platform
is_macos() { [[ "$(uname)" == "Darwin" ]]; }

# Get current mode from config
get_mode() {
    if [[ -f "$CONFIG_FILE" ]]; then
        jq -r '.mode // "direct"' "$CONFIG_FILE"
    else
        echo "direct"
    fi
}

# Check if daemon process is actually alive (not just registered with service manager)
daemon_running() {
    # Check PID file first -- most reliable indicator of actual process
    if [[ -f "$PID_FILE" ]]; then
        local pid
        pid=$(cat "$PID_FILE" 2>/dev/null)
        if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
            return 0
        fi
    fi

    # Check heartbeat file as secondary indicator
    local heartbeat="$HOME/.claude-tts/daemon.heartbeat"
    if [[ -f "$heartbeat" ]]; then
        local last_beat now age
        last_beat=$(cat "$heartbeat" 2>/dev/null)
        if [[ -n "$last_beat" ]]; then
            now=$(date +%s)
            age=$(( now - ${last_beat%%.*} ))
            if (( age < 10 )); then
                return 0
            fi
        fi
    fi

    return 1
}

# Get daemon PID
get_daemon_pid() {
    if is_macos; then
        launchctl list 2>/dev/null | grep "$LAUNCHD_LABEL" | awk '{print $1}' | grep -v "^-$" || true
    else
        systemctl --user show "$SYSTEMD_SERVICE" --property=MainPID 2>/dev/null | cut -d= -f2 || true
    fi

    # Fallback
    if [[ -f "$PID_FILE" ]]; then
        cat "$PID_FILE"
    fi
}

cmd_show() {
    local mode=$(get_mode)
    echo "TTS Mode: $mode"
    echo ""

    if [[ "$mode" == "queue" ]]; then
        if daemon_running; then
            local pid=$(get_daemon_pid)
            if is_macos; then
                echo "Daemon: RUNNING via launchd (PID: $pid)"
            else
                echo "Daemon: RUNNING via systemd (PID: $pid)"
            fi
        else
            echo "Daemon: NOT RUNNING"
        fi

        if [[ -d "$QUEUE_DIR" ]]; then
            local count=$(ls -1 "$QUEUE_DIR"/*.json 2>/dev/null | wc -l | tr -d ' ')
            echo "Queue depth: $count"
        fi
    fi

    echo ""
    echo "Commands:"
    echo "  tts-mode direct   - Immediate playback (no daemon)"
    echo "  tts-mode queue    - Queue mode (daemon required)"
    echo "  tts-mode start    - Start daemon"
    echo "  tts-mode stop     - Stop daemon"
    echo "  tts-mode restart  - Restart daemon"
    echo "  tts-mode status   - Detailed status"
    echo "  tts-mode install  - Install system service"
    echo "  tts-mode logs     - View logs"
}

cmd_direct() {
    if [[ -f "$CONFIG_FILE" ]]; then
        jq '.mode = "direct"' "$CONFIG_FILE" > "$CONFIG_FILE.tmp" && mv "$CONFIG_FILE.tmp" "$CONFIG_FILE"
        echo "Switched to DIRECT mode"
        echo "TTS plays immediately (no daemon needed)"
    else
        echo "No config file: $CONFIG_FILE"
        exit 1
    fi
}

cmd_queue() {
    if [[ -f "$CONFIG_FILE" ]]; then
        jq '.mode = "queue"' "$CONFIG_FILE" > "$CONFIG_FILE.tmp" && mv "$CONFIG_FILE.tmp" "$CONFIG_FILE"
        echo "Switched to QUEUE mode"
        echo "Start daemon with: tts-mode start"
    else
        echo "No config file: $CONFIG_FILE"
        exit 1
    fi
}

cmd_start() {
    if daemon_running; then
        echo "Daemon already running (PID: $(get_daemon_pid))"
        return 0
    fi

    if is_macos; then
        if [[ -f "$PLIST_DST" ]]; then
            echo "Starting via launchd..."
            launchctl load "$PLIST_DST" 2>/dev/null || true
            launchctl start "$LAUNCHD_LABEL"
            sleep 1
            if daemon_running; then
                echo "Daemon started (PID: $(get_daemon_pid))"
            else
                echo "Failed to start - check logs"
            fi
        elif [[ -f "$DAEMON_SCRIPT" ]]; then
            echo "No launchd service, starting standalone..."
            uv run --python 3.12 "$DAEMON_SCRIPT" start
        else
            echo "Daemon not installed"
            exit 1
        fi
    else
        if systemctl --user list-unit-files | grep -q "$SYSTEMD_SERVICE"; then
            echo "Starting via systemd..."
            systemctl --user start "$SYSTEMD_SERVICE"
            sleep 1
            systemctl --user status "$SYSTEMD_SERVICE" --no-pager | head -3
        elif [[ -f "$DAEMON_SCRIPT" ]]; then
            echo "No systemd service, starting standalone..."
            uv run --python 3.12 "$DAEMON_SCRIPT" start
        else
            echo "Daemon not installed"
            exit 1
        fi
    fi
}

cmd_stop() {
    if ! daemon_running; then
        echo "Daemon not running"
        return 0
    fi

    if is_macos; then
        if launchctl list 2>/dev/null | grep -q "$LAUNCHD_LABEL"; then
            echo "Stopping via launchd..."
            launchctl stop "$LAUNCHD_LABEL"
        elif [[ -f "$DAEMON_SCRIPT" ]]; then
            uv run --python 3.12 "$DAEMON_SCRIPT" stop
        fi
    else
        if systemctl --user is-active "$SYSTEMD_SERVICE" &>/dev/null; then
            echo "Stopping via systemd..."
            systemctl --user stop "$SYSTEMD_SERVICE"
        elif [[ -f "$DAEMON_SCRIPT" ]]; then
            uv run --python 3.12 "$DAEMON_SCRIPT" stop
        fi
    fi

    # Wait for the process to actually die (graceful shutdown needs time for goodbye speech)
    local waited=0
    while daemon_running && (( waited < 15 )); do
        sleep 1
        waited=$(( waited + 1 ))
        if (( waited % 3 == 0 )); then
            echo "  Waiting for daemon to finish... (${waited}s)"
        fi
    done

    if daemon_running; then
        echo "Daemon did not stop gracefully, forcing..."
        if [[ -f "$PID_FILE" ]]; then
            kill -9 "$(cat "$PID_FILE")" 2>/dev/null || true
            rm -f "$PID_FILE"
        fi
    fi
    echo "Daemon stopped"
}

cmd_restart() {
    cmd_stop
    # Clean up stale state from the old daemon
    rm -f "$PID_FILE" "$HOME/.claude-tts/daemon.heartbeat" "$HOME/.claude-tts/daemon.lock"
    sleep 0.5
    cmd_start
}

cmd_status() {
    echo "=== TTS Daemon Status ==="
    echo ""

    if is_macos; then
        if [[ -f "$PLIST_DST" ]]; then
            echo "Service: launchd ($LAUNCHD_LABEL)"
            echo "Plist: $PLIST_DST"
            echo ""
            launchctl list 2>/dev/null | grep -E "PID|$LAUNCHD_LABEL" | head -5 || echo "(not loaded)"
        else
            echo "Service: standalone (no launchd plist installed)"
        fi
    else
        if systemctl --user list-unit-files 2>/dev/null | grep -q "$SYSTEMD_SERVICE"; then
            echo "Service: systemd ($SYSTEMD_SERVICE)"
            systemctl --user status "$SYSTEMD_SERVICE" --no-pager 2>/dev/null || echo "(not running)"
        else
            echo "Service: standalone (no systemd unit installed)"
        fi
    fi

    echo ""
    if [[ -f "$DAEMON_SCRIPT" ]]; then
        uv run --python 3.12 "$DAEMON_SCRIPT" status
    fi
}

cmd_install() {
    if is_macos; then
        if [[ ! -f "$PLIST_SRC" ]]; then
            echo "Plist not found: $PLIST_SRC"
            echo "Run the TTS installer first"
            exit 1
        fi

        mkdir -p "$HOME/Library/LaunchAgents"

        # Unload if loaded
        if launchctl list 2>/dev/null | grep -q "$LAUNCHD_LABEL"; then
            launchctl unload "$PLIST_DST" 2>/dev/null || true
        fi

        # Copy and expand ~ to $HOME
        sed "s|~|$HOME|g" "$PLIST_SRC" > "$PLIST_DST"

        echo "Installed: $PLIST_DST"
        echo ""
        echo "To auto-start on login: launchctl load $PLIST_DST"
        echo "To start now: tts-mode start"
    else
        if [[ ! -f "$SYSTEMD_SRC" ]]; then
            echo "Service file not found: $SYSTEMD_SRC"
            echo "Run the TTS installer first"
            exit 1
        fi

        mkdir -p "$HOME/.config/systemd/user"
        cp "$SYSTEMD_SRC" "$SYSTEMD_DST"
        systemctl --user daemon-reload

        echo "Installed: $SYSTEMD_DST"
        echo ""
        echo "To auto-start on login: systemctl --user enable $SYSTEMD_SERVICE"
        echo "To start now: tts-mode start"
    fi
}

cmd_logs() {
    local log="$HOME/.claude-tts/daemon.log"
    local stdout="$HOME/.claude-tts/daemon.stdout.log"
    local stderr="$HOME/.claude-tts/daemon.stderr.log"

    echo "=== Daemon Logs ==="

    if [[ -f "$log" ]]; then
        echo ""
        echo "--- daemon.log (last 20 lines) ---"
        tail -20 "$log"
    fi

    if [[ -f "$stdout" ]] && [[ -s "$stdout" ]]; then
        echo ""
        echo "--- stdout (last 10 lines) ---"
        tail -10 "$stdout"
    fi

    if [[ -f "$stderr" ]] && [[ -s "$stderr" ]]; then
        echo ""
        echo "--- stderr (last 10 lines) ---"
        tail -10 "$stderr"
    fi
}

# Main
case "${1:-}" in
    "")        cmd_show ;;
    direct)    cmd_direct ;;
    queue)     cmd_queue ;;
    start)     cmd_start ;;
    stop)      cmd_stop ;;
    restart)   cmd_restart ;;
    status)    cmd_status ;;
    install)   cmd_install ;;
    logs)      cmd_logs ;;
    *)
        echo "Unknown command: $1"
        echo "Usage: tts-mode [direct|queue|start|stop|restart|status|install|logs]"
        exit 1
        ;;
esac
