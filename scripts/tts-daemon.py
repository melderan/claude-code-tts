#!/usr/bin/env python3
"""
tts-daemon.py - Multi-session TTS message bus daemon

Monitors a queue directory and plays TTS messages in order,
allowing multiple Claude sessions to speak without overlapping.

Usage:
    python tts-daemon.py start       # Start daemon in background
    python tts-daemon.py stop        # Stop the daemon
    python tts-daemon.py status      # Check if running
    python tts-daemon.py --foreground  # Run in foreground (debug)
"""

import argparse
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

# --- Configuration ---

HOME = Path.home()
TTS_CONFIG_DIR = HOME / ".claude-tts"
TTS_CONFIG_FILE = TTS_CONFIG_DIR / "config.json"
QUEUE_DIR = TTS_CONFIG_DIR / "queue"
PID_FILE = TTS_CONFIG_DIR / "daemon.pid"
LOCK_FILE = TTS_CONFIG_DIR / "daemon.lock"
LOG_FILE = TTS_CONFIG_DIR / "daemon.log"
VOICES_DIR = HOME / ".local" / "share" / "piper-voices"

# Global lock file handle (kept open while daemon runs)
_lock_fd = None

# Heartbeat file -- daemon touches this every poll cycle so hooks can detect stalls
HEARTBEAT_FILE = TTS_CONFIG_DIR / "daemon.heartbeat"

# Graceful shutdown flag -- set by SIGTERM handler, checked by main loop
_shutdown_requested = False

# Default voice model
DEFAULT_VOICE = "en_US-hfc_male-medium"

# --- Logging ---

def log(msg: str, level: str = "INFO") -> None:
    """Log a message to the daemon log file."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] [{level}] {msg}\n"

    # Always print in foreground mode
    if not DAEMON_MODE:
        print(line.strip())

    # Write to log file
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line)
    except Exception:
        pass

# Global flag for daemon vs foreground mode
DAEMON_MODE = False


# --- Lock File ---

def acquire_lock(lockpick: bool = False) -> bool:
    """Acquire exclusive lock to prevent duplicate daemons.

    Args:
        lockpick: If True, forcibly take the lock (for recovery from unclean shutdown)

    Returns:
        True if lock acquired, False if another daemon is running.
    """
    global _lock_fd

    try:
        # Create lock file if needed
        LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        _lock_fd = open(LOCK_FILE, 'w')

        if lockpick:
            # Force acquire - used for recovery
            try:
                fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                # Someone has the lock - try to kill them
                if PID_FILE.exists():
                    try:
                        old_pid = int(PID_FILE.read_text().strip())
                        os.kill(old_pid, signal.SIGTERM)
                        time.sleep(1)
                    except (ValueError, ProcessLookupError):
                        pass
                # Try again
                fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            # Normal acquire - fail if locked
            fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        # Write our PID to lock file
        _lock_fd.write(str(os.getpid()))
        _lock_fd.flush()
        return True

    except BlockingIOError:
        if _lock_fd:
            _lock_fd.close()
            _lock_fd = None
        return False
    except Exception as e:
        log(f"Lock acquisition failed: {e}", "ERROR")
        if _lock_fd:
            _lock_fd.close()
            _lock_fd = None
        return False


def release_lock() -> None:
    """Release the daemon lock."""
    global _lock_fd
    if _lock_fd:
        try:
            fcntl.flock(_lock_fd, fcntl.LOCK_UN)
            _lock_fd.close()
        except Exception:
            pass
        _lock_fd = None


# --- Heartbeat ---

def write_heartbeat() -> None:
    """Touch the heartbeat file so hooks know we're alive."""
    try:
        HEARTBEAT_FILE.write_text(str(time.time()))
    except Exception:
        pass


def daemon_is_healthy() -> bool:
    """Check if the daemon has heartbeated recently (for use by hooks)."""
    if not HEARTBEAT_FILE.exists():
        return False
    try:
        last_beat = float(HEARTBEAT_FILE.read_text().strip())
        return (time.time() - last_beat) < 10
    except (ValueError, IOError):
        return False


# --- Config Loading ---

def load_config() -> dict:
    """Load TTS config."""
    if TTS_CONFIG_FILE.exists():
        with open(TTS_CONFIG_FILE) as f:
            return json.load(f)
    return {}


# --- Playback State (for pause/resume) ---

PLAYBACK_STATE_FILE = TTS_CONFIG_DIR / "playback.json"


def read_playback_state() -> dict:
    """Read current playback state."""
    if PLAYBACK_STATE_FILE.exists():
        try:
            # Force fresh read from disk (bypass any OS caching)
            fd = os.open(str(PLAYBACK_STATE_FILE), os.O_RDONLY)
            try:
                data = os.read(fd, 10000).decode('utf-8')
                return json.loads(data)
            finally:
                os.close(fd)
        except (json.JSONDecodeError, IOError, OSError):
            pass
    return {"paused": False, "audio_pid": None, "current_message": None}


def write_playback_state(
    audio_pid: Optional[int] = None,
    paused: Optional[bool] = None,
    current_message: Optional[dict] = "UNSET"  # Use sentinel to distinguish None from unset
) -> None:
    """Update playback state atomically."""
    state = read_playback_state()
    if audio_pid is not None:
        state["audio_pid"] = audio_pid
    if paused is not None:
        state["paused"] = paused
    if current_message != "UNSET":
        state["current_message"] = current_message
    state["updated_at"] = time.time()

    # Atomic write with sync
    tmp = PLAYBACK_STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f)
        f.flush()
        os.fsync(f.fileno())
    tmp.rename(PLAYBACK_STATE_FILE)


def clear_current_message() -> None:
    """Clear the current message (called after successful playback)."""
    write_playback_state(current_message=None)


def get_interrupted_message() -> Optional[dict]:
    """Get the interrupted message if any, and clear it."""
    state = read_playback_state()
    msg = state.get("current_message")
    if msg:
        write_playback_state(current_message=None)
    return msg


def get_queue_config() -> dict:
    """Get queue-specific config with defaults."""
    config = load_config()
    defaults = {
        "max_depth": 20,
        "max_age_seconds": 300,
        "speaker_transition": "chime",  # "chime", "announce", "none"
        "coalesce_rapid_ms": 500,
        "idle_poll_ms": 100,
    }
    return {**defaults, **config.get("queue", {})}


def get_persona_config(persona_name: str) -> dict:
    """Get config for a specific persona."""
    config = load_config()
    personas = config.get("personas", {})

    if persona_name in personas:
        return personas[persona_name]

    # Return defaults
    return {
        "voice": DEFAULT_VOICE,
        "speed": 2.0,
        "speed_method": "playback",
    }


# --- Audio Playback ---

def get_audio_player() -> tuple[str, list[str]]:
    """Detect available audio player and return (name, base_command)."""
    import shutil

    if shutil.which("afplay"):
        return ("afplay", ["afplay"])
    elif shutil.which("paplay"):
        return ("paplay", ["paplay"])
    elif shutil.which("aplay"):
        return ("aplay", ["aplay", "-q"])
    else:
        return ("none", [])


def play_audio(wav_file: Path, speed: float = 1.0) -> tuple[bool, bool]:
    """Play a WAV file. Returns (success, was_killed).

    was_killed=True means audio was interrupted by pause (should replay on resume).
    was_killed=False means audio completed normally or had an error.
    """
    player_name, player_cmd = get_audio_player()

    if player_name == "none":
        log("No audio player available", "ERROR")
        return (False, False)

    try:
        cmd = player_cmd.copy()

        # afplay supports playback speed
        if player_name == "afplay" and speed != 1.0:
            cmd.extend(["-r", str(speed)])

        cmd.append(str(wav_file))

        # Use Popen to get PID for kill-on-pause
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        write_playback_state(audio_pid=proc.pid)
        log(f"Audio started (PID {proc.pid}), polling for pause...")

        # Poll for completion, checking pause state
        poll_count = 0
        while proc.poll() is None:
            state = read_playback_state()
            poll_count += 1
            if poll_count % 20 == 0:  # Log every ~1 second
                log(f"Poll #{poll_count}: paused={state.get('paused')}, pid={proc.pid}")
            if state.get("paused"):
                # Kill the audio process immediately (user paused)
                log(f"Audio killed for pause (PID {proc.pid})")
                try:
                    proc.terminate()
                    proc.wait(timeout=1)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                write_playback_state(audio_pid=None)
                return (False, True)  # was_killed=True
            time.sleep(0.05)  # Small poll interval

        # Clear PID from state
        write_playback_state(audio_pid=None)

        return (proc.returncode == 0, False)
    except Exception as e:
        log(f"Audio playback failed: {e}", "ERROR")
        write_playback_state(audio_pid=None)
        return (False, False)


def play_chime() -> None:
    """Play a brief chime to indicate speaker change."""
    import shutil

    # Try system sounds first (macOS) - prefer shorter sounds
    system_sounds = [
        "/System/Library/Sounds/Tink.aiff",   # 0.56s
        "/System/Library/Sounds/Morse.aiff",  # 0.70s
        "/System/Library/Sounds/Pop.aiff",    # 1.63s (fallback)
    ]

    for sound in system_sounds:
        if Path(sound).exists():
            player_name, player_cmd = get_audio_player()
            if player_name == "afplay":
                try:
                    # Play at lower volume
                    subprocess.run(["afplay", "-v", "0.3", sound],
                                   check=True, capture_output=True)
                    return
                except subprocess.CalledProcessError:
                    pass

    # Fallback: generate a quick beep with piper if available
    if shutil.which("piper"):
        # Just skip the chime if we can't play system sounds
        pass


def speak_announcement(text: str, persona: str = "claude-prime") -> None:
    """Speak a short announcement (used for daemon lifecycle messages)."""
    audio_file = Path("/tmp/tts_daemon_announce.wav")
    if generate_speech(text, persona, audio_file):
        persona_config = get_persona_config(persona)
        speed = persona_config.get("speed", 2.0)
        if persona_config.get("speed_method") == "playback":
            play_audio(audio_file, speed)
        else:
            play_audio(audio_file)
        audio_file.unlink(missing_ok=True)


def generate_speech(text: str, persona: str, output_file: Path) -> bool:
    """Generate speech audio using Piper. Returns True on success."""
    import shutil

    if not shutil.which("piper"):
        log("Piper not found", "ERROR")
        return False

    persona_config = get_persona_config(persona)
    voice_name = persona_config.get("voice", DEFAULT_VOICE)
    voice_file = VOICES_DIR / f"{voice_name}.onnx"

    if not voice_file.exists():
        log(f"Voice file not found: {voice_file}", "ERROR")
        # Try default voice
        voice_file = VOICES_DIR / f"{DEFAULT_VOICE}.onnx"
        if not voice_file.exists():
            log(f"Default voice also not found", "ERROR")
            return False

    speed = persona_config.get("speed", 2.0)
    speed_method = persona_config.get("speed_method", "playback")

    try:
        cmd = ["piper", "--model", str(voice_file), "--output_file", str(output_file)]

        # Apply speed via length_scale if not using playback method
        if speed_method == "length_scale" and speed != 1.0:
            length_scale = 1.0 / speed
            cmd.extend(["--length_scale", str(length_scale)])

        subprocess.run(cmd, input=text, text=True, check=True, capture_output=True)
        return output_file.exists()
    except subprocess.CalledProcessError as e:
        log(f"Piper failed: {e}", "ERROR")
        return False


# --- Queue Management ---

def get_queue_messages() -> list[dict]:
    """Get all messages in the queue, sorted by timestamp."""
    messages = []

    if not QUEUE_DIR.exists():
        return messages

    for f in QUEUE_DIR.glob("*.json"):
        try:
            with open(f) as fp:
                msg = json.load(fp)
                msg["_file"] = f
                messages.append(msg)
        except (json.JSONDecodeError, IOError) as e:
            log(f"Failed to read queue file {f}: {e}", "WARN")
            # Remove corrupt file
            f.unlink(missing_ok=True)

    # Sort by timestamp
    messages.sort(key=lambda m: m.get("timestamp", 0))
    return messages


def cleanup_old_messages(max_age_seconds: int) -> int:
    """Remove messages older than max_age. Returns count removed."""
    removed = 0
    cutoff = time.time() - max_age_seconds

    for f in QUEUE_DIR.glob("*.json"):
        try:
            with open(f) as fp:
                msg = json.load(fp)
            if msg.get("timestamp", 0) < cutoff:
                f.unlink()
                removed += 1
                log(f"Removed stale message: {f.name}")
        except (json.JSONDecodeError, IOError):
            f.unlink(missing_ok=True)
            removed += 1

    return removed


def enforce_max_depth(max_depth: int) -> int:
    """Remove oldest messages if queue exceeds max depth. Returns count removed."""
    messages = get_queue_messages()
    removed = 0

    while len(messages) > max_depth:
        oldest = messages.pop(0)
        oldest["_file"].unlink(missing_ok=True)
        removed += 1
        log(f"Queue overflow, removed: {oldest.get('project', 'unknown')}")

    return removed


# --- Main Daemon Loop ---

def daemon_loop(lockpick: bool = False) -> None:
    """Main daemon processing loop."""
    global _shutdown_requested

    # Acquire exclusive lock to prevent duplicate daemons
    if not acquire_lock(lockpick=lockpick):
        log("Another daemon is already running. Exiting.", "ERROR")
        print("Another daemon is already running. Use --lockpick to force takeover.")
        sys.exit(1)

    # Graceful shutdown handler -- sets flag, loop finishes current work
    def handle_shutdown(signum: int, frame: object) -> None:
        global _shutdown_requested
        _shutdown_requested = True
        log(f"Shutdown requested (signal {signum}), finishing current work...")

    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    log("Daemon starting...")

    # Write PID file (for status checks, even in foreground mode)
    PID_FILE.write_text(str(os.getpid()))

    # Ensure queue directory exists
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)

    config = get_queue_config()
    poll_interval = config["idle_poll_ms"] / 1000.0
    last_speaker = None

    log(f"Queue config: max_depth={config['max_depth']}, "
        f"max_age={config['max_age_seconds']}s, "
        f"transition={config['speaker_transition']}")

    # Startup announcement
    write_heartbeat()
    speak_announcement("Voice daemon online. Ready when you are.")
    log("Startup announcement complete")

    while not _shutdown_requested:
        try:
            # Write heartbeat so hooks know we're alive
            write_heartbeat()

            # Cleanup old messages
            cleanup_old_messages(config["max_age_seconds"])

            # Enforce max depth
            enforce_max_depth(config["max_depth"])

            # Check pause state before processing new messages
            state = read_playback_state()
            if state.get("paused"):
                time.sleep(poll_interval)
                continue

            # Check for interrupted message to replay first
            interrupted = get_interrupted_message()
            if interrupted:
                log(f"Replaying interrupted message: {interrupted.get('text', '')[:50]}...")
                session_id = interrupted.get("session_id", "unknown")
                text = interrupted.get("text", "")
                persona = interrupted.get("persona", "claude-prime")

                audio_file = Path(f"/tmp/tts_queue_{session_id}.wav")
                persona_config = get_persona_config(persona)
                speed = persona_config.get("speed", 2.0)

                if generate_speech(text, persona, audio_file):
                    # Save as current message in case interrupted again
                    write_playback_state(current_message=interrupted)

                    if persona_config.get("speed_method") == "playback":
                        _, was_killed = play_audio(audio_file, speed)
                    else:
                        _, was_killed = play_audio(audio_file)

                    audio_file.unlink(missing_ok=True)

                    if was_killed:
                        # Interrupted again, leave current_message for next resume
                        log("Interrupted message paused again")
                        continue
                    else:
                        # Completed, clear current_message
                        clear_current_message()
                continue

            # Get pending messages
            messages = get_queue_messages()

            if not messages:
                time.sleep(poll_interval)
                continue

            # Process the oldest message
            msg = messages[0]
            msg_file = msg["_file"]

            session_id = msg.get("session_id", "unknown")
            project = msg.get("project", "unknown")
            text = msg.get("text", "")
            persona = msg.get("persona", "claude-prime")

            if not text.strip():
                log(f"Empty message from {project}, skipping")
                msg_file.unlink(missing_ok=True)
                continue

            # Generate speech FIRST (before any chimes) so it's ready to play
            log(f"Speaking for {project}: {text[:50]}...")
            audio_file = Path(f"/tmp/tts_queue_{session_id}.wav")
            persona_config = get_persona_config(persona)
            speed = persona_config.get("speed", 2.0)

            if not generate_speech(text, persona, audio_file):
                log(f"Failed to generate speech for message from {project}", "ERROR")
                msg_file.unlink(missing_ok=True)
                continue

            # Speaker transition (audio is already generated, ready to play)
            speaker_key = f"{session_id}:{project}"
            if last_speaker and last_speaker != speaker_key:
                transition = config["speaker_transition"]

                if transition == "chime":
                    log(f"Speaker change: {last_speaker} -> {speaker_key}")
                    play_chime()
                elif transition == "announce":
                    log(f"Announcing speaker: {project}")
                    announce_file = Path("/tmp/tts_announce.wav")
                    if generate_speech(f"{project} says:", persona, announce_file):
                        announce_speed = speed if persona_config.get("speed_method") == "playback" else None
                        if announce_speed:
                            play_audio(announce_file, announce_speed)
                        else:
                            play_audio(announce_file)
                        announce_file.unlink(missing_ok=True)
                    time.sleep(0.3)

            last_speaker = speaker_key

            # Save message info before playing (for replay if interrupted)
            current_msg_info = {
                "session_id": session_id,
                "project": project,
                "text": text,
                "persona": persona,
            }
            write_playback_state(current_message=current_msg_info)
            log(f"Saved current_message for potential replay")

            # Play the pre-generated audio
            if persona_config.get("speed_method") == "playback":
                _, was_killed = play_audio(audio_file, speed)
            else:
                _, was_killed = play_audio(audio_file)

            audio_file.unlink(missing_ok=True)

            if was_killed:
                # Audio was interrupted by pause - leave current_message for replay
                log("Message interrupted, will replay on resume")
                # Don't delete the queue file yet - but actually we should since
                # the message is saved in current_message. Let's delete it.
                msg_file.unlink(missing_ok=True)
                continue
            else:
                # Completed normally - clear current_message and remove queue file
                clear_current_message()
                msg_file.unlink(missing_ok=True)

        except KeyboardInterrupt:
            log("Received interrupt, shutting down...")
            break
        except Exception as e:
            log(f"Error in daemon loop: {e}", "ERROR")
            time.sleep(1)  # Avoid tight error loop

    # Graceful shutdown -- announce and clean up
    log("Shutting down gracefully...")
    speak_announcement("Voice daemon shutting down. Catch you later.")
    HEARTBEAT_FILE.unlink(missing_ok=True)
    release_lock()
    log("Daemon stopped")


# --- Daemon Management ---

def is_daemon_running() -> tuple[bool, Optional[int]]:
    """Check if daemon is running. Returns (is_running, pid)."""
    if not PID_FILE.exists():
        return False, None

    try:
        pid = int(PID_FILE.read_text().strip())
        # Check if process exists
        os.kill(pid, 0)
        return True, pid
    except (ValueError, ProcessLookupError, PermissionError):
        # Stale PID file
        PID_FILE.unlink(missing_ok=True)
        return False, None


def start_daemon(lockpick: bool = False) -> bool:
    """Start the daemon in background. Returns True on success."""
    if not lockpick:
        running, pid = is_daemon_running()
        if running:
            print(f"Daemon already running (PID: {pid})")
            print("Use --lockpick to force takeover")
            return False

    # Fork to background
    try:
        pid = os.fork()
        if pid > 0:
            # Parent - wait briefly and check if child started
            time.sleep(0.5)
            running, child_pid = is_daemon_running()
            if running:
                print(f"Daemon started (PID: {child_pid})")
                return True
            else:
                print("Daemon failed to start. Check ~/.claude-tts/daemon.log")
                return False
    except OSError as e:
        print(f"Fork failed: {e}")
        return False

    # Child process - become daemon
    os.setsid()

    # Second fork to prevent zombie
    try:
        pid = os.fork()
        if pid > 0:
            os._exit(0)
    except OSError:
        os._exit(1)

    # Setup daemon environment
    os.chdir("/")
    os.umask(0)

    # Redirect standard file descriptors
    sys.stdin = open(os.devnull, 'r')
    sys.stdout = open(os.devnull, 'w')
    sys.stderr = open(os.devnull, 'w')

    # Write PID file
    TTS_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))

    # Ignore SIGHUP (terminal closed) -- SIGTERM/SIGINT handled in daemon_loop
    signal.signal(signal.SIGHUP, signal.SIG_IGN)

    # Cleanup PID file on exit
    import atexit
    atexit.register(lambda: PID_FILE.unlink(missing_ok=True))

    global DAEMON_MODE
    DAEMON_MODE = True

    # Run the daemon (lockpick already handled in parent check above)
    daemon_loop(lockpick=lockpick)
    return True


def stop_daemon() -> bool:
    """Stop the daemon gracefully. Returns True on success."""
    running, pid = is_daemon_running()

    if not running:
        print("Daemon is not running")
        return False

    assert pid is not None  # guaranteed by is_daemon_running returning True

    try:
        os.kill(pid, signal.SIGTERM)
        # Give daemon time to finish current audio + speak goodbye (~15s max)
        for i in range(150):
            time.sleep(0.1)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                PID_FILE.unlink(missing_ok=True)
                print("Daemon stopped gracefully")
                return True
            # Progress indicator every 3 seconds
            if i > 0 and i % 30 == 0:
                print(f"  Waiting for daemon to finish... ({i // 10}s)")

        # Force kill if still running after 15 seconds
        print("Daemon did not stop gracefully, forcing...")
        os.kill(pid, signal.SIGKILL)
        PID_FILE.unlink(missing_ok=True)
        HEARTBEAT_FILE.unlink(missing_ok=True)
        print("Daemon killed")
        return True
    except ProcessLookupError:
        PID_FILE.unlink(missing_ok=True)
        print("Daemon was not running")
        return False
    except PermissionError:
        print(f"Permission denied to stop daemon (PID: {pid})")
        return False


def daemon_status() -> None:
    """Print daemon status."""
    running, pid = is_daemon_running()

    if running:
        print(f"Daemon is running (PID: {pid})")

        # Show queue stats
        messages = get_queue_messages()
        print(f"Queue depth: {len(messages)}")

        if messages:
            print("Pending messages:")
            for msg in messages[:5]:
                project = msg.get("project", "unknown")
                text = msg.get("text", "")[:40]
                print(f"  - {project}: {text}...")
            if len(messages) > 5:
                print(f"  ... and {len(messages) - 5} more")
    else:
        print("Daemon is not running")

    # Show log tail
    if LOG_FILE.exists():
        print(f"\nRecent log ({LOG_FILE}):")
        try:
            lines = LOG_FILE.read_text().strip().split("\n")
            for line in lines[-5:]:
                print(f"  {line}")
        except Exception:
            pass


def run_foreground(lockpick: bool = False) -> None:
    """Run daemon in foreground (for debugging)."""
    if not lockpick:
        running, pid = is_daemon_running()
        if running:
            print(f"Background daemon is running (PID: {pid})")
            print("Stop it first with: python tts-daemon.py stop")
            print("Or use --lockpick to force takeover")
            return

    print("Running in foreground (Ctrl+C to stop)...")
    print(f"Queue directory: {QUEUE_DIR}")
    print()

    global DAEMON_MODE
    DAEMON_MODE = False

    try:
        daemon_loop(lockpick=lockpick)
    except KeyboardInterrupt:
        print("\nStopped")


# --- CLI ---

def main() -> None:
    parser = argparse.ArgumentParser(
        description="TTS daemon for multi-session message bus",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Commands:
  start      Start the daemon in background
  stop       Stop the daemon
  status     Show daemon status and queue info

Options:
  --foreground    Run in foreground (for debugging)
  --lockpick      Force takeover if another daemon is running

Examples:
  python tts-daemon.py start
  python tts-daemon.py status
  python tts-daemon.py --foreground
  python tts-daemon.py --foreground --lockpick  # Force restart
        """,
    )

    parser.add_argument(
        "command",
        nargs="?",
        choices=["start", "stop", "status"],
        help="Daemon command",
    )
    parser.add_argument(
        "--foreground", "-f",
        action="store_true",
        help="Run in foreground",
    )
    parser.add_argument(
        "--lockpick",
        action="store_true",
        help="Force takeover if another daemon is running (use after unclean shutdown)",
    )

    args = parser.parse_args()

    if args.foreground:
        run_foreground(lockpick=args.lockpick)
    elif args.command == "start":
        start_daemon(lockpick=args.lockpick)
    elif args.command == "stop":
        stop_daemon()
    elif args.command == "status":
        daemon_status()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
