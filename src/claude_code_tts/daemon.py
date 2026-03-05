"""TTS daemon — multi-session message bus for queue mode.

Monitors ~/.claude-tts/queue/ and plays TTS messages in order,
preventing overlap between multiple Claude sessions.

Absorbed from scripts/tts-daemon.py into the Python CLI.
"""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import signal
import subprocess
import sys
import time
from datetime import datetime
from io import TextIOWrapper
from pathlib import Path

from claude_code_tts.audio import generate_speech as _generate_speech, detect_player
from claude_code_tts.config import (
    TTS_CONFIG_DIR,
    TTS_QUEUE_DIR,
    VOICES_DIR,
    load_raw_config,
)

# --- Daemon path constants ---

PID_FILE = TTS_CONFIG_DIR / "daemon.pid"
LOCK_FILE = TTS_CONFIG_DIR / "daemon.lock"
LOG_FILE = TTS_CONFIG_DIR / "daemon.log"
HEARTBEAT_FILE = TTS_CONFIG_DIR / "daemon.heartbeat"
PLAYBACK_STATE_FILE = TTS_CONFIG_DIR / "playback.json"
VERSION_FILE = TTS_CONFIG_DIR / "daemon.version"
RESPAWN_MARKER = TTS_CONFIG_DIR / "daemon.respawn"

# Default voice model
DEFAULT_VOICE = "en_US-hfc_male-medium"

# Global state
_lock_fd: TextIOWrapper | None = None
_shutdown_requested = False
_daemon_mode = False


# --- Logging ---


def log(msg: str, level: str = "INFO") -> None:
    """Log a message to the daemon log file."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] [{level}] {msg}\n"

    if not _daemon_mode:
        print(line.strip())

    try:
        with open(LOG_FILE, "a") as f:
            f.write(line)
    except Exception:
        pass


# --- Lock File ---


def acquire_lock(lockpick: bool = False) -> bool:
    """Acquire exclusive lock to prevent duplicate daemons."""
    global _lock_fd

    try:
        LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        _lock_fd = open(LOCK_FILE, "w")

        if lockpick:
            try:
                fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if PID_FILE.exists():
                    try:
                        old_pid = int(PID_FILE.read_text().strip())
                        os.kill(old_pid, signal.SIGTERM)
                        time.sleep(1)
                    except (ValueError, ProcessLookupError):
                        pass
                fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

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


# --- Playback State (pause/resume) ---


def read_playback_state() -> dict:
    """Read current playback state."""
    if PLAYBACK_STATE_FILE.exists():
        try:
            fd = os.open(str(PLAYBACK_STATE_FILE), os.O_RDONLY)
            try:
                data = os.read(fd, 10000).decode("utf-8")
                return json.loads(data)
            finally:
                os.close(fd)
        except (json.JSONDecodeError, IOError, OSError):
            pass
    return {"paused": False, "audio_pid": None, "current_message": None}


_UNSET = object()


def write_playback_state(
    audio_pid: int | None = None,
    paused: bool | None = None,
    current_message: dict | None | object = _UNSET,
) -> None:
    """Update playback state atomically."""
    state = read_playback_state()
    if audio_pid is not None:
        state["audio_pid"] = audio_pid
    if paused is not None:
        state["paused"] = paused
    if current_message is not _UNSET:
        state["current_message"] = current_message
    state["updated_at"] = time.time()

    tmp = PLAYBACK_STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f)
        f.flush()
        os.fsync(f.fileno())
    tmp.rename(PLAYBACK_STATE_FILE)


def clear_current_message() -> None:
    """Clear the current message (called after successful playback)."""
    write_playback_state(current_message=None)


def get_interrupted_message() -> dict | None:
    """Get the interrupted message if any, and clear it."""
    state = read_playback_state()
    msg = state.get("current_message")
    if msg:
        write_playback_state(current_message=None)
    return msg


# --- Persona/Config helpers ---


def get_queue_config() -> dict:
    """Get queue-specific config with defaults."""
    config = load_raw_config()
    defaults = {
        "max_depth": 20,
        "max_age_seconds": 300,
        "speaker_transition": "chime",
        "coalesce_rapid_ms": 500,
        "idle_poll_ms": 100,
    }
    return {**defaults, **config.get("queue", {})}


def get_persona_config(persona_name: str) -> dict:
    """Get config for a specific persona."""
    config = load_raw_config()
    personas = config.get("personas", {})
    if persona_name in personas:
        return personas[persona_name]
    return {
        "voice": DEFAULT_VOICE,
        "speed": 2.0,
        "speed_method": "playback",
    }


# --- Audio (daemon-specific) ---


def daemon_generate_speech(
    text: str,
    persona: str,
    output_file: Path,
    voice_kokoro_override: str = "",
    voice_kokoro_blend_override: str = "",
) -> bool:
    """Generate speech for daemon playback, resolving persona config.

    Returns True on success.
    """
    persona_config = get_persona_config(persona)

    kokoro_blend = voice_kokoro_blend_override or persona_config.get("voice_kokoro_blend", "")
    kokoro_voice = voice_kokoro_override or persona_config.get("voice_kokoro", "")
    voice_name = persona_config.get("voice", DEFAULT_VOICE)
    voice_path = VOICES_DIR / f"{voice_name}.onnx"
    speed = persona_config.get("speed", 2.0)
    speed_method = persona_config.get("speed_method", "playback")

    # Fall back to default voice if persona voice not found
    if not voice_path.exists():
        voice_path = VOICES_DIR / f"{DEFAULT_VOICE}.onnx"

    result = _generate_speech(
        text,
        voice_path=voice_path if voice_path.exists() else None,
        voice_kokoro=kokoro_voice,
        voice_kokoro_blend=kokoro_blend,
        speed=speed,
        speed_method=speed_method,
        output_path=output_file,
    )
    return result is not None


def daemon_play_audio(wav_file: Path, speed: float = 1.0) -> tuple[bool, bool]:
    """Play a WAV file with pause-aware polling.

    Returns (success, was_killed).
    was_killed=True means audio was interrupted by pause (should replay on resume).
    """
    player = detect_player()
    if not player:
        log("No audio player available", "ERROR")
        return (False, False)

    try:
        cmd = list(player)
        if cmd[0] == "afplay" and speed != 1.0:
            cmd.extend(["-r", str(speed)])
        cmd.append(str(wav_file))

        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        write_playback_state(audio_pid=proc.pid)
        log(f"Audio started (PID {proc.pid}), polling for pause...")

        poll_count = 0
        while proc.poll() is None:
            state = read_playback_state()
            poll_count += 1
            if poll_count % 20 == 0:
                log(f"Poll #{poll_count}: paused={state.get('paused')}, pid={proc.pid}")
                write_heartbeat()
            if state.get("paused"):
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
                return (False, True)
            time.sleep(0.05)

        write_playback_state(audio_pid=None)
        return (proc.returncode == 0, False)
    except Exception as e:
        log(f"Audio playback failed: {e}", "ERROR")
        write_playback_state(audio_pid=None)
        return (False, False)


def play_chime() -> None:
    """Play a brief chime to indicate speaker change."""
    system_sounds = [
        "/System/Library/Sounds/Tink.aiff",
        "/System/Library/Sounds/Morse.aiff",
        "/System/Library/Sounds/Pop.aiff",
    ]
    for sound in system_sounds:
        if Path(sound).exists():
            player = detect_player()
            if player and player[0] == "afplay":
                try:
                    subprocess.run(
                        ["afplay", "-v", "0.3", sound],
                        check=True, capture_output=True,
                    )
                    return
                except subprocess.CalledProcessError:
                    pass


def speak_announcement(text: str, persona: str = "claude-prime") -> None:
    """Speak a short announcement (daemon lifecycle messages)."""
    audio_file = Path("/tmp/tts_daemon_announce.wav")
    if daemon_generate_speech(text, persona, audio_file):
        persona_config = get_persona_config(persona)
        speed = persona_config.get("speed", 2.0)
        if persona_config.get("speed_method") == "playback":
            daemon_play_audio(audio_file, speed)
        else:
            daemon_play_audio(audio_file)
        audio_file.unlink(missing_ok=True)


# --- Control Messages ---


def handle_control_message(msg: dict) -> None:
    """Handle a control message with pre_action, speech, and post_action."""
    pre_action = msg.get("pre_action")
    post_action = msg.get("post_action")
    text = msg.get("text", "")
    persona = msg.get("persona", "claude-prime")

    log(f"Control message: pre={pre_action}, post={post_action}, text={text[:50]!r}")

    if pre_action == "drain":
        log("Control: drain (no-op in serial mode)")

    if text.strip():
        speak_announcement(text, persona)

    if post_action == "restart":
        VERSION_FILE.write_text("control-v1")
        log("Control: exiting for launchd restart")
        msg_file = msg.get("_file")
        if msg_file:
            Path(msg_file).unlink(missing_ok=True)
        RESPAWN_MARKER.write_text(str(time.time()))
        HEARTBEAT_FILE.unlink(missing_ok=True)
        release_lock()
        sys.exit(3)
    elif post_action == "reload_config":
        log("Control: reloading config")
    elif post_action == "stop":
        global _shutdown_requested
        _shutdown_requested = True
        log("Control: stop requested")


def write_control_message(
    text: str = "",
    pre_action: str | None = None,
    post_action: str | None = None,
) -> Path:
    """Write a control message to the queue directory."""
    TTS_QUEUE_DIR.mkdir(parents=True, exist_ok=True)

    msg: dict = {
        "id": secrets.token_hex(8),
        "timestamp": time.time(),
        "type": "control",
        "session_id": "system",
        "text": text,
    }
    if pre_action:
        msg["pre_action"] = pre_action
    if post_action:
        msg["post_action"] = post_action

    queue_file = TTS_QUEUE_DIR / f"{msg['timestamp']}_{msg['id']}.json"
    tmp_file = queue_file.with_suffix(".tmp")
    tmp_file.write_text(json.dumps(msg))
    tmp_file.rename(queue_file)
    log(f"Control message written: {queue_file.name}")
    return queue_file


# --- Queue Management ---


def get_queue_messages() -> list[dict]:
    """Get all messages in the queue, sorted by timestamp."""
    messages = []
    if not TTS_QUEUE_DIR.exists():
        return messages

    for f in TTS_QUEUE_DIR.glob("*.json"):
        try:
            with open(f) as fp:
                msg = json.load(fp)
                msg["_file"] = f
                messages.append(msg)
        except (json.JSONDecodeError, IOError) as e:
            log(f"Failed to read queue file {f}: {e}", "WARN")
            f.unlink(missing_ok=True)

    messages.sort(key=lambda m: m.get("timestamp", 0))
    return messages


def cleanup_old_messages(max_age_seconds: int) -> int:
    """Remove messages older than max_age. Returns count removed."""
    removed = 0
    cutoff = time.time() - max_age_seconds

    for f in TTS_QUEUE_DIR.glob("*.json"):
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
    """Remove oldest messages if queue exceeds max depth."""
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

    if not acquire_lock(lockpick=lockpick):
        log("Another daemon is already running. Exiting.", "ERROR")
        print("Another daemon is already running. Use --lockpick to force takeover.")
        sys.exit(1)

    _shutdown_by_signal = False

    def handle_shutdown(signum: int, _frame: object) -> None:
        global _shutdown_requested
        nonlocal _shutdown_by_signal
        _shutdown_requested = True
        _shutdown_by_signal = True
        log(f"Shutdown requested (signal {signum}), finishing current work...")

    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    log("Daemon starting...")
    PID_FILE.write_text(str(os.getpid()))
    TTS_QUEUE_DIR.mkdir(parents=True, exist_ok=True)

    config = get_queue_config()
    poll_interval = config["idle_poll_ms"] / 1000.0
    last_speaker: str | None = None

    log(
        f"Queue config: max_depth={config['max_depth']}, "
        f"max_age={config['max_age_seconds']}s, "
        f"transition={config['speaker_transition']}"
    )

    VERSION_FILE.write_text("control-v1")

    write_heartbeat()
    if RESPAWN_MARKER.exists():
        RESPAWN_MARKER.unlink(missing_ok=True)
        log("Quick respawn detected, skipping startup announcement")
    else:
        speak_announcement("Voice daemon online. Ready when you are.")
        log("Startup announcement complete")

    while not _shutdown_requested:
        try:
            write_heartbeat()
            cleanup_old_messages(config["max_age_seconds"])
            enforce_max_depth(config["max_depth"])

            state = read_playback_state()
            if state.get("paused"):
                time.sleep(poll_interval)
                continue

            # Check for interrupted message to replay first
            interrupted = get_interrupted_message()
            if interrupted:
                log(f"Replaying interrupted message: {interrupted.get('text', '')[:50]}...")
                session_id = interrupted.get("session_id", "unknown")
                persona = interrupted.get("persona", "claude-prime")
                persona_config = get_persona_config(persona)
                i_speed = interrupted.get("speed", persona_config.get("speed", 2.0))
                i_speed_method = interrupted.get(
                    "speed_method", persona_config.get("speed_method", "playback")
                )
                i_voice_kokoro = interrupted.get("voice_kokoro", "")
                i_voice_blend = interrupted.get("voice_kokoro_blend", "")

                audio_file = Path(f"/tmp/tts_queue_{session_id}.wav")
                if daemon_generate_speech(
                    interrupted.get("text", ""),
                    persona,
                    audio_file,
                    voice_kokoro_override=i_voice_kokoro,
                    voice_kokoro_blend_override=i_voice_blend,
                ):
                    write_playback_state(current_message=interrupted)
                    if i_speed_method == "playback":
                        _, was_killed = daemon_play_audio(audio_file, i_speed)
                    else:
                        _, was_killed = daemon_play_audio(audio_file)
                    audio_file.unlink(missing_ok=True)
                    if was_killed:
                        log("Interrupted message paused again")
                        continue
                    else:
                        clear_current_message()
                continue

            # Get pending messages
            messages = get_queue_messages()
            if not messages:
                time.sleep(poll_interval)
                continue

            msg = messages[0]
            msg_file = msg["_file"]

            # Control messages
            if msg.get("type") == "control":
                handle_control_message(msg)
                msg_file.unlink(missing_ok=True)
                continue

            session_id = msg.get("session_id", "unknown")
            project = msg.get("project", "unknown")
            text = msg.get("text", "")
            persona = msg.get("persona", "claude-prime")

            if not text.strip():
                log(f"Empty message from {project}, skipping")
                msg_file.unlink(missing_ok=True)
                continue

            log(f"Speaking for {project}: {text[:50]}...")
            audio_file = Path(f"/tmp/tts_queue_{session_id}.wav")
            persona_config = get_persona_config(persona)
            speed = msg.get("speed", persona_config.get("speed", 2.0))
            speed_method = msg.get("speed_method", persona_config.get("speed_method", "playback"))
            voice_kokoro = msg.get("voice_kokoro", "")
            voice_kokoro_blend = msg.get("voice_kokoro_blend", "")

            if not daemon_generate_speech(
                text,
                persona,
                audio_file,
                voice_kokoro_override=voice_kokoro,
                voice_kokoro_blend_override=voice_kokoro_blend,
            ):
                log(f"Failed to generate speech for message from {project}", "ERROR")
                msg_file.unlink(missing_ok=True)
                continue

            # Speaker transition
            speaker_key = f"{session_id}:{project}"
            if last_speaker and last_speaker != speaker_key:
                transition = config["speaker_transition"]
                if transition == "chime":
                    log(f"Speaker change: {last_speaker} -> {speaker_key}")
                    play_chime()
                elif transition == "announce":
                    log(f"Announcing speaker: {project}")
                    announce_file = Path("/tmp/tts_announce.wav")
                    if daemon_generate_speech(f"{project} says:", persona, announce_file):
                        announce_speed = speed if speed_method == "playback" else None
                        if announce_speed:
                            daemon_play_audio(announce_file, announce_speed)
                        else:
                            daemon_play_audio(announce_file)
                        announce_file.unlink(missing_ok=True)
                    time.sleep(0.3)

            last_speaker = speaker_key

            current_msg_info = {
                "session_id": session_id,
                "project": project,
                "text": text,
                "persona": persona,
                "speed": speed,
                "speed_method": speed_method,
                "voice_kokoro": voice_kokoro,
                "voice_kokoro_blend": voice_kokoro_blend,
            }
            write_playback_state(current_message=current_msg_info)

            if speed_method == "playback":
                _, was_killed = daemon_play_audio(audio_file, speed)
            else:
                _, was_killed = daemon_play_audio(audio_file)

            audio_file.unlink(missing_ok=True)

            if was_killed:
                log("Message interrupted, will replay on resume")
                msg_file.unlink(missing_ok=True)
                continue
            else:
                clear_current_message()
                msg_file.unlink(missing_ok=True)

        except KeyboardInterrupt:
            log("Received interrupt, shutting down...")
            break
        except Exception as e:
            log(f"Error in daemon loop: {e}", "ERROR")
            time.sleep(1)

    log("Shutting down gracefully...")
    if not _shutdown_by_signal:
        speak_announcement("Voice daemon shutting down. Catch you later.")
    HEARTBEAT_FILE.unlink(missing_ok=True)
    release_lock()
    log("Daemon stopped")


# --- Daemon Management ---


def is_daemon_running() -> tuple[bool, int | None]:
    """Check if daemon is running. Returns (is_running, pid)."""
    if not PID_FILE.exists():
        return False, None
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)
        return True, pid
    except (ValueError, ProcessLookupError, PermissionError):
        PID_FILE.unlink(missing_ok=True)
        return False, None


def start_daemon(lockpick: bool = False) -> bool:
    """Start the daemon in background via double-fork. Returns True on success."""
    if not lockpick:
        running, pid = is_daemon_running()
        if running:
            print(f"Daemon already running (PID: {pid})")
            print("Use --lockpick to force takeover")
            return False

    try:
        pid = os.fork()
        if pid > 0:
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

    # Child: become session leader
    os.setsid()

    # Second fork to prevent zombie
    try:
        pid = os.fork()
        if pid > 0:
            os._exit(0)
    except OSError:
        os._exit(1)

    os.chdir("/")
    os.umask(0)

    sys.stdin = open(os.devnull, "r")
    sys.stdout = open(os.devnull, "w")
    sys.stderr = open(os.devnull, "w")

    TTS_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))

    signal.signal(signal.SIGHUP, signal.SIG_IGN)

    import atexit
    atexit.register(lambda: PID_FILE.unlink(missing_ok=True))

    global _daemon_mode
    _daemon_mode = True

    daemon_loop(lockpick=lockpick)
    return True


def stop_daemon() -> bool:
    """Stop the daemon gracefully. Returns True on success."""
    running, pid = is_daemon_running()
    if not running:
        print("Daemon is not running")
        return False

    assert pid is not None

    try:
        os.kill(pid, signal.SIGTERM)
        for i in range(150):
            time.sleep(0.1)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                PID_FILE.unlink(missing_ok=True)
                print("Daemon stopped gracefully")
                return True
            if i > 0 and i % 30 == 0:
                print(f"  Waiting for daemon to finish... ({i // 10}s)")

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
            print("Stop it first with: claude-tts daemon stop")
            print("Or use --lockpick to force takeover")
            return

    print("Running in foreground (Ctrl+C to stop)...")
    print(f"Queue directory: {TTS_QUEUE_DIR}")
    print()

    global _daemon_mode
    _daemon_mode = False

    try:
        daemon_loop(lockpick=lockpick)
    except KeyboardInterrupt:
        print("\nStopped")


def daemon_restart(lockpick: bool = False) -> None:
    """Restart the daemon (stop then start)."""
    running, _ = is_daemon_running()
    if running:
        print("Stopping daemon...")
        stop_daemon()
    start_daemon(lockpick=lockpick)


def show_logs(follow: bool = False) -> None:
    """Show daemon log file."""
    if not LOG_FILE.exists():
        print(f"No log file found at {LOG_FILE}")
        return

    if follow:
        try:
            subprocess.run(["tail", "-f", str(LOG_FILE)])
        except KeyboardInterrupt:
            pass
    else:
        try:
            lines = LOG_FILE.read_text().strip().split("\n")
            for line in lines[-50:]:
                print(line)
        except Exception as e:
            print(f"Error reading log: {e}")


def install_service() -> None:
    """Install the daemon as a system service (launchd/systemd)."""
    from claude_code_tts.audio import detect_platform

    plat = detect_platform()
    if plat == "macos":
        _install_launchd()
    elif plat in ("linux", "wsl"):
        _install_systemd()
    else:
        print(f"Unsupported platform: {plat}")


def _install_launchd() -> None:
    """Install launchd plist for macOS."""
    import shutil

    claude_tts_bin = shutil.which("claude-tts")
    if not claude_tts_bin:
        print("claude-tts not found on PATH")
        print("Install with: uv tool install claude-code-tts")
        return

    plist_dir = Path.home() / "Library" / "LaunchAgents"
    plist_dir.mkdir(parents=True, exist_ok=True)
    plist_path = plist_dir / "com.claude-tts.daemon.plist"

    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.claude-tts.daemon</string>
  <key>ProgramArguments</key>
  <array>
    <string>{claude_tts_bin}</string>
    <string>daemon</string>
    <string>foreground</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <dict>
    <key>SuccessfulExit</key>
    <false/>
  </dict>
  <key>StandardOutPath</key>
  <string>{LOG_FILE}</string>
  <key>StandardErrorPath</key>
  <string>{LOG_FILE}</string>
</dict>
</plist>
"""
    plist_path.write_text(plist)
    print(f"Installed launchd plist: {plist_path}")
    print("")
    print("To load:")
    print(f"  launchctl load {plist_path}")
    print("To unload:")
    print(f"  launchctl unload {plist_path}")


def _install_systemd() -> None:
    """Install systemd user service for Linux."""
    import shutil

    claude_tts_bin = shutil.which("claude-tts")
    if not claude_tts_bin:
        print("claude-tts not found on PATH")
        print("Install with: uv tool install claude-code-tts")
        return

    service_dir = Path.home() / ".config" / "systemd" / "user"
    service_dir.mkdir(parents=True, exist_ok=True)
    service_path = service_dir / "claude-tts-daemon.service"

    unit = f"""[Unit]
Description=Claude Code TTS Daemon
After=default.target

[Service]
ExecStart={claude_tts_bin} daemon foreground
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""
    service_path.write_text(unit)
    print(f"Installed systemd service: {service_path}")
    print("")
    print("To enable and start:")
    print("  systemctl --user daemon-reload")
    print("  systemctl --user enable --now claude-tts-daemon")
