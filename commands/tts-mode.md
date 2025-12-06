Switch TTS mode between direct playback and queue (daemon) mode.

Check if the user provided an argument. The argument will be appended to this prompt.

**If no argument:** Show current mode and daemon status:

```bash
CONFIG_FILE="$HOME/.claude-tts/config.json"
PLIST="$HOME/Library/LaunchAgents/com.claude-tts.daemon.plist"
SYSTEMD_SERVICE="claude-tts"

MODE="direct"
if [[ -f "$CONFIG_FILE" ]]; then
    MODE=$(jq -r '.mode // "direct"' "$CONFIG_FILE")
fi

echo "TTS Mode: $MODE"
echo ""

if [[ "$MODE" == "queue" ]]; then
    # Check daemon status based on platform
    if [[ "$(uname)" == "Darwin" ]]; then
        # macOS - check launchctl
        if launchctl list | grep -q "com.claude-tts.daemon"; then
            PID=$(launchctl list | grep "com.claude-tts.daemon" | awk '{print $1}')
            if [[ "$PID" != "-" ]]; then
                echo "Daemon: RUNNING via launchd (PID: $PID)"
            else
                echo "Daemon: LOADED but not running"
            fi
        else
            # Fall back to PID file check
            PID_FILE="$HOME/.claude-tts/daemon.pid"
            if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
                echo "Daemon: RUNNING standalone (PID: $(cat "$PID_FILE"))"
            else
                echo "Daemon: NOT RUNNING"
            fi
        fi
    else
        # Linux - check systemctl
        if systemctl --user is-active "$SYSTEMD_SERVICE" &>/dev/null; then
            echo "Daemon: RUNNING via systemd"
            systemctl --user status "$SYSTEMD_SERVICE" --no-pager | head -3
        else
            PID_FILE="$HOME/.claude-tts/daemon.pid"
            if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
                echo "Daemon: RUNNING standalone (PID: $(cat "$PID_FILE"))"
            else
                echo "Daemon: NOT RUNNING"
            fi
        fi
    fi

    # Show queue depth
    QUEUE_DIR="$HOME/.claude-tts/queue"
    if [[ -d "$QUEUE_DIR" ]]; then
        COUNT=$(ls -1 "$QUEUE_DIR"/*.json 2>/dev/null | wc -l | tr -d ' ')
        echo "Queue depth: $COUNT"
    fi
fi

echo ""
echo "Commands:"
echo "  /tts-mode direct   - Switch to direct mode (immediate playback)"
echo "  /tts-mode queue    - Switch to queue mode (daemon handles playback)"
echo "  /tts-mode start    - Start the TTS daemon"
echo "  /tts-mode stop     - Stop the TTS daemon"
echo "  /tts-mode restart  - Restart the daemon"
echo "  /tts-mode status   - Show detailed daemon status"
echo "  /tts-mode install  - Install as system service (launchd/systemd)"
echo "  /tts-mode logs     - Show daemon logs"
```

**If argument is "direct":** Switch to direct mode:

```bash
CONFIG_FILE="$HOME/.claude-tts/config.json"

if [[ -f "$CONFIG_FILE" ]]; then
    jq '.mode = "direct"' "$CONFIG_FILE" > "$CONFIG_FILE.tmp" && mv "$CONFIG_FILE.tmp" "$CONFIG_FILE"
    echo "Switched to DIRECT mode"
    echo "TTS will play immediately (no daemon needed)"
else
    echo "No config file found at $CONFIG_FILE"
fi
```

**If argument is "queue":** Switch to queue mode:

```bash
CONFIG_FILE="$HOME/.claude-tts/config.json"

if [[ -f "$CONFIG_FILE" ]]; then
    jq '.mode = "queue"' "$CONFIG_FILE" > "$CONFIG_FILE.tmp" && mv "$CONFIG_FILE.tmp" "$CONFIG_FILE"
    echo "Switched to QUEUE mode"
    echo "TTS messages will be queued for the daemon"
    echo ""
    echo "Start the daemon with: /tts-mode start"
else
    echo "No config file found at $CONFIG_FILE"
fi
```

**If argument is "start":** Start the daemon:

```bash
PLIST="$HOME/Library/LaunchAgents/com.claude-tts.daemon.plist"
SYSTEMD_SERVICE="claude-tts"
DAEMON_SCRIPT="$HOME/.claude-tts/tts-daemon.py"

if [[ "$(uname)" == "Darwin" ]]; then
    # macOS - prefer launchctl
    if [[ -f "$PLIST" ]]; then
        if launchctl list | grep -q "com.claude-tts.daemon"; then
            echo "Daemon already loaded, starting..."
            launchctl start com.claude-tts.daemon
        else
            echo "Loading and starting daemon via launchd..."
            launchctl load "$PLIST"
            launchctl start com.claude-tts.daemon
        fi
        sleep 1
        if launchctl list | grep -q "com.claude-tts.daemon"; then
            PID=$(launchctl list | grep "com.claude-tts.daemon" | awk '{print $1}')
            echo "Daemon started (PID: $PID)"
        fi
    elif [[ -f "$DAEMON_SCRIPT" ]]; then
        echo "No launchd service installed, starting standalone..."
        python3 "$DAEMON_SCRIPT" start
    else
        echo "Daemon not found. Run installer first."
    fi
else
    # Linux - prefer systemctl
    if systemctl --user list-unit-files | grep -q "$SYSTEMD_SERVICE"; then
        echo "Starting daemon via systemd..."
        systemctl --user start "$SYSTEMD_SERVICE"
        sleep 1
        systemctl --user status "$SYSTEMD_SERVICE" --no-pager | head -3
    elif [[ -f "$DAEMON_SCRIPT" ]]; then
        echo "No systemd service installed, starting standalone..."
        python3 "$DAEMON_SCRIPT" start
    else
        echo "Daemon not found. Run installer first."
    fi
fi
```

**If argument is "stop":** Stop the daemon:

```bash
PLIST="$HOME/Library/LaunchAgents/com.claude-tts.daemon.plist"
SYSTEMD_SERVICE="claude-tts"
DAEMON_SCRIPT="$HOME/.claude-tts/tts-daemon.py"

if [[ "$(uname)" == "Darwin" ]]; then
    if launchctl list | grep -q "com.claude-tts.daemon"; then
        echo "Stopping daemon via launchd..."
        launchctl stop com.claude-tts.daemon
        echo "Daemon stopped"
    elif [[ -f "$DAEMON_SCRIPT" ]]; then
        python3 "$DAEMON_SCRIPT" stop
    else
        echo "Daemon not running"
    fi
else
    if systemctl --user is-active "$SYSTEMD_SERVICE" &>/dev/null; then
        echo "Stopping daemon via systemd..."
        systemctl --user stop "$SYSTEMD_SERVICE"
        echo "Daemon stopped"
    elif [[ -f "$DAEMON_SCRIPT" ]]; then
        python3 "$DAEMON_SCRIPT" stop
    else
        echo "Daemon not running"
    fi
fi
```

**If argument is "restart":** Restart the daemon:

```bash
PLIST="$HOME/Library/LaunchAgents/com.claude-tts.daemon.plist"
SYSTEMD_SERVICE="claude-tts"
DAEMON_SCRIPT="$HOME/.claude-tts/tts-daemon.py"

if [[ "$(uname)" == "Darwin" ]]; then
    if launchctl list | grep -q "com.claude-tts.daemon"; then
        echo "Restarting daemon via launchd..."
        launchctl stop com.claude-tts.daemon
        sleep 1
        launchctl start com.claude-tts.daemon
        echo "Daemon restarted"
    else
        echo "Daemon not loaded. Use /tts-mode start"
    fi
else
    if systemctl --user list-unit-files | grep -q "$SYSTEMD_SERVICE"; then
        echo "Restarting daemon via systemd..."
        systemctl --user restart "$SYSTEMD_SERVICE"
        echo "Daemon restarted"
    else
        echo "Daemon not installed. Use /tts-mode install"
    fi
fi
```

**If argument is "status":** Show detailed daemon status:

```bash
PLIST="$HOME/Library/LaunchAgents/com.claude-tts.daemon.plist"
SYSTEMD_SERVICE="claude-tts"
DAEMON_SCRIPT="$HOME/.claude-tts/tts-daemon.py"

echo "=== TTS Daemon Status ==="
echo ""

if [[ "$(uname)" == "Darwin" ]]; then
    if [[ -f "$PLIST" ]]; then
        echo "Service: launchd (com.claude-tts.daemon)"
        echo "Plist: $PLIST"
        echo ""
        launchctl list | grep -E "PID|com.claude-tts" | head -5
    else
        echo "Service: standalone (no launchd plist)"
    fi
else
    if systemctl --user list-unit-files | grep -q "$SYSTEMD_SERVICE"; then
        echo "Service: systemd ($SYSTEMD_SERVICE)"
        systemctl --user status "$SYSTEMD_SERVICE" --no-pager
    else
        echo "Service: standalone (no systemd unit)"
    fi
fi

echo ""
if [[ -f "$DAEMON_SCRIPT" ]]; then
    python3 "$DAEMON_SCRIPT" status
fi
```

**If argument is "install":** Install as system service:

```bash
PLIST_SRC="$HOME/.claude-tts/services/com.claude-tts.daemon.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.claude-tts.daemon.plist"
SYSTEMD_SRC="$HOME/.claude-tts/services/claude-tts.service"
SYSTEMD_DST="$HOME/.config/systemd/user/claude-tts.service"

if [[ "$(uname)" == "Darwin" ]]; then
    if [[ ! -f "$PLIST_SRC" ]]; then
        echo "Plist source not found: $PLIST_SRC"
        echo "Run the installer to set up service files"
        exit 1
    fi

    mkdir -p "$HOME/Library/LaunchAgents"

    # Unload if already loaded
    if launchctl list | grep -q "com.claude-tts.daemon"; then
        launchctl unload "$PLIST_DST" 2>/dev/null
    fi

    # Copy and fix paths (replace ~ with actual home)
    sed "s|~|$HOME|g" "$PLIST_SRC" > "$PLIST_DST"

    echo "Installed launchd service: $PLIST_DST"
    echo ""
    echo "To start automatically on login:"
    echo "  launchctl load $PLIST_DST"
    echo ""
    echo "To start now:"
    echo "  /tts-mode start"
else
    if [[ ! -f "$SYSTEMD_SRC" ]]; then
        echo "Service file not found: $SYSTEMD_SRC"
        echo "Run the installer to set up service files"
        exit 1
    fi

    mkdir -p "$HOME/.config/systemd/user"
    cp "$SYSTEMD_SRC" "$SYSTEMD_DST"

    systemctl --user daemon-reload

    echo "Installed systemd service: $SYSTEMD_DST"
    echo ""
    echo "To start automatically on login:"
    echo "  systemctl --user enable claude-tts"
    echo ""
    echo "To start now:"
    echo "  /tts-mode start"
fi
```

**If argument is "logs":** Show daemon logs:

```bash
LOG_FILE="$HOME/.claude-tts/daemon.log"
STDOUT_LOG="$HOME/.claude-tts/daemon.stdout.log"
STDERR_LOG="$HOME/.claude-tts/daemon.stderr.log"

echo "=== Recent Daemon Logs ==="
echo ""

if [[ -f "$LOG_FILE" ]]; then
    echo "--- daemon.log (last 20 lines) ---"
    tail -20 "$LOG_FILE"
fi

if [[ -f "$STDOUT_LOG" ]]; then
    echo ""
    echo "--- stdout (last 10 lines) ---"
    tail -10 "$STDOUT_LOG"
fi

if [[ -f "$STDERR_LOG" ]] && [[ -s "$STDERR_LOG" ]]; then
    echo ""
    echo "--- stderr (last 10 lines) ---"
    tail -10 "$STDERR_LOG"
fi
```

After running the appropriate command, summarize the result to the user.
