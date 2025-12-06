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
LOG_FILE = TTS_CONFIG_DIR / "daemon.log"
VOICES_DIR = HOME / ".local" / "share" / "piper-voices"

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

# --- Config Loading ---

def load_config() -> dict:
    """Load TTS config."""
    if TTS_CONFIG_FILE.exists():
        with open(TTS_CONFIG_FILE) as f:
            return json.load(f)
    return {}


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


def play_audio(wav_file: Path, speed: float = 1.0) -> bool:
    """Play a WAV file. Returns True on success."""
    player_name, player_cmd = get_audio_player()

    if player_name == "none":
        log("No audio player available", "ERROR")
        return False

    try:
        cmd = player_cmd.copy()

        # afplay supports playback speed
        if player_name == "afplay" and speed != 1.0:
            cmd.extend(["-r", str(speed)])

        cmd.append(str(wav_file))
        subprocess.run(cmd, check=True, capture_output=True)
        return True
    except subprocess.CalledProcessError as e:
        log(f"Audio playback failed: {e}", "ERROR")
        return False


def play_chime() -> None:
    """Play a brief chime to indicate speaker change."""
    import shutil

    # Try system sounds first (macOS)
    system_sounds = [
        "/System/Library/Sounds/Pop.aiff",
        "/System/Library/Sounds/Tink.aiff",
        "/System/Library/Sounds/Blow.aiff",
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

def daemon_loop() -> None:
    """Main daemon processing loop."""
    log("Daemon starting...")

    # Ensure queue directory exists
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)

    config = get_queue_config()
    poll_interval = config["idle_poll_ms"] / 1000.0
    last_speaker = None

    log(f"Queue config: max_depth={config['max_depth']}, "
        f"max_age={config['max_age_seconds']}s, "
        f"transition={config['speaker_transition']}")

    while True:
        try:
            # Cleanup old messages
            cleanup_old_messages(config["max_age_seconds"])

            # Enforce max depth
            enforce_max_depth(config["max_depth"])

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

            # Speaker transition
            speaker_key = f"{session_id}:{project}"
            if last_speaker and last_speaker != speaker_key:
                transition = config["speaker_transition"]

                if transition == "chime":
                    log(f"Speaker change: {last_speaker} -> {speaker_key}")
                    play_chime()
                    time.sleep(0.2)  # Brief pause after chime
                elif transition == "announce":
                    log(f"Announcing speaker: {project}")
                    announce_file = Path("/tmp/tts_announce.wav")
                    if generate_speech(f"{project} says:", persona, announce_file):
                        persona_config = get_persona_config(persona)
                        speed = persona_config.get("speed", 2.0)
                        if persona_config.get("speed_method") == "playback":
                            play_audio(announce_file, speed)
                        else:
                            play_audio(announce_file)
                        announce_file.unlink(missing_ok=True)
                    time.sleep(0.3)

            last_speaker = speaker_key

            # Generate and play the message
            log(f"Speaking for {project}: {text[:50]}...")

            audio_file = Path(f"/tmp/tts_queue_{session_id}.wav")

            if generate_speech(text, persona, audio_file):
                persona_config = get_persona_config(persona)
                speed = persona_config.get("speed", 2.0)

                if persona_config.get("speed_method") == "playback":
                    play_audio(audio_file, speed)
                else:
                    play_audio(audio_file)

                audio_file.unlink(missing_ok=True)
            else:
                log(f"Failed to generate speech for message from {project}", "ERROR")

            # Remove processed message
            msg_file.unlink(missing_ok=True)

        except KeyboardInterrupt:
            log("Received interrupt, shutting down...")
            break
        except Exception as e:
            log(f"Error in daemon loop: {e}", "ERROR")
            time.sleep(1)  # Avoid tight error loop

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


def start_daemon() -> bool:
    """Start the daemon in background. Returns True on success."""
    running, pid = is_daemon_running()
    if running:
        print(f"Daemon already running (PID: {pid})")
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

    # Setup signal handlers
    signal.signal(signal.SIGTERM, lambda s, f: sys.exit(0))
    signal.signal(signal.SIGHUP, signal.SIG_IGN)

    # Cleanup PID file on exit
    import atexit
    atexit.register(lambda: PID_FILE.unlink(missing_ok=True))

    global DAEMON_MODE
    DAEMON_MODE = True

    # Run the daemon
    daemon_loop()
    return True


def stop_daemon() -> bool:
    """Stop the daemon. Returns True on success."""
    running, pid = is_daemon_running()

    if not running:
        print("Daemon is not running")
        return False

    try:
        os.kill(pid, signal.SIGTERM)
        # Wait for process to exit
        for _ in range(50):  # 5 seconds max
            time.sleep(0.1)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                PID_FILE.unlink(missing_ok=True)
                print("Daemon stopped")
                return True

        # Force kill if still running
        os.kill(pid, signal.SIGKILL)
        PID_FILE.unlink(missing_ok=True)
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


def run_foreground() -> None:
    """Run daemon in foreground (for debugging)."""
    running, pid = is_daemon_running()
    if running:
        print(f"Background daemon is running (PID: {pid})")
        print("Stop it first with: python tts-daemon.py stop")
        return

    print("Running in foreground (Ctrl+C to stop)...")
    print(f"Queue directory: {QUEUE_DIR}")
    print()

    global DAEMON_MODE
    DAEMON_MODE = False

    try:
        daemon_loop()
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

Examples:
  python tts-daemon.py start
  python tts-daemon.py status
  python tts-daemon.py --foreground
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

    args = parser.parse_args()

    if args.foreground:
        run_foreground()
    elif args.command == "start":
        start_daemon()
    elif args.command == "stop":
        stop_daemon()
    elif args.command == "status":
        daemon_status()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
