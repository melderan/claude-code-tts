"""Audio generation and playback for Claude Code TTS.

Handles Piper, Kokoro (swift-kokoro), and system audio players.
Replaces tts_speak() from tts-lib.sh.
"""

import json
import os
import platform
import secrets
import shutil
import subprocess
import time
from pathlib import Path

from claude_code_tts.config import TTSConfig, TTS_QUEUE_DIR, debug


def detect_platform() -> str:
    """Detect platform: 'macos', 'linux', or 'wsl'."""
    system = platform.system()
    if system == "Darwin":
        return "macos"
    if system == "Linux":
        try:
            with open("/proc/version") as f:
                if "microsoft" in f.read().lower():
                    return "wsl"
        except (FileNotFoundError, PermissionError):
            pass
        return "linux"
    return "unknown"


def detect_player() -> list[str] | None:
    """Return the audio player command prefix, or None if none found."""
    if shutil.which("afplay"):
        return ["afplay"]
    if shutil.which("paplay"):
        return ["paplay"]
    if shutil.which("aplay"):
        return ["aplay", "-q"]
    return None


def generate_speech(
    text: str,
    *,
    voice_path: Path | None = None,
    voice_kokoro: str = "",
    voice_kokoro_blend: str = "",
    speed: float = 2.0,
    speed_method: str = "",
    speaker: int | None = None,
    output_path: Path | None = None,
    noise_scale: float | None = None,
    noise_w_scale: float | None = None,
    sentence_silence: float | None = None,
) -> Path | None:
    """Generate a WAV file from text using Kokoro or Piper.

    Piper-specific parameters for expressive speech:
        noise_scale: Prosody variation (0.0-1.0, default 0.667).
            Higher = more animated intonation. Lower = monotone/grave.
        noise_w_scale: Timing variation (0.0-1.0, default 0.8).
            Higher = more natural rhythm variation between phonemes.
        sentence_silence: Seconds of silence between sentences (default 0.0).

    Returns the path to the generated WAV, or None on failure.
    """
    if output_path is None:
        slot = int(time.time()) % 5
        output_path = Path(f"/tmp/claude_tts_{slot}.wav")

    # Priority 1: Kokoro blend
    if shutil.which("swift-kokoro") and voice_kokoro_blend:
        try:
            subprocess.run(
                ["swift-kokoro", "--blend", voice_kokoro_blend, "--output", str(output_path)],
                input=text, text=True, capture_output=True, timeout=30,
            )
            if output_path.exists():
                return output_path
        except (subprocess.TimeoutExpired, OSError):
            pass

    # Priority 2: Kokoro single voice
    if shutil.which("swift-kokoro") and voice_kokoro:
        try:
            subprocess.run(
                ["swift-kokoro", "--voice", voice_kokoro, "--output", str(output_path)],
                input=text, text=True, capture_output=True, timeout=30,
            )
            if output_path.exists():
                return output_path
        except (subprocess.TimeoutExpired, OSError):
            pass

    # Priority 3: Piper
    if shutil.which("piper") and voice_path and voice_path.exists():
        cmd = ["piper", "--model", str(voice_path), "--output_file", str(output_path)]
        if speed_method == "length_scale" and speed > 0:
            length_scale = f"{1.0 / speed:.2f}"
            cmd.extend(["--length_scale", length_scale])
        if speaker is not None:
            cmd.extend(["--speaker", str(speaker)])
        # Expressive speech parameters
        if noise_scale is not None:
            cmd.extend(["--noise_scale", f"{noise_scale:.3f}"])
        if noise_w_scale is not None:
            cmd.extend(["--noise_w", f"{noise_w_scale:.3f}"])
        if sentence_silence is not None:
            cmd.extend(["--sentence_silence", f"{sentence_silence:.2f}"])
        try:
            subprocess.run(
                cmd, input=text, text=True, capture_output=True, timeout=30,
            )
            if output_path.exists():
                return output_path
        except (subprocess.TimeoutExpired, OSError):
            pass

    return None


def play_audio(
    wav_path: Path,
    *,
    speed: float = 1.0,
    speed_method: str = "playback",
    background: bool = True,
) -> subprocess.Popen | None:
    """Play a WAV file using the system audio player.

    Returns the Popen process if background=True, else None after completion.
    """
    player = detect_player()
    if not player:
        # Fallback: macOS say
        if shutil.which("say"):
            # Can't play WAV with say, but this is a last resort
            return None
        return None

    cmd = list(player)
    # afplay supports playback speed
    if cmd[0] == "afplay" and speed_method == "playback" and speed != 1.0:
        cmd.extend(["-r", str(speed)])
    cmd.append(str(wav_path))

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not background:
            proc.wait()
            return None
        return proc
    except OSError:
        return None


def speak_direct(text: str, config: TTSConfig) -> None:
    """Direct mode: generate WAV and play immediately in background."""
    plat = detect_platform()
    method = config.speed_method
    if not method:
        method = "playback" if plat == "macos" else "length_scale"

    wav = generate_speech(
        text,
        voice_path=config.voice_path,
        voice_kokoro=config.voice_kokoro,
        voice_kokoro_blend=config.voice_kokoro_blend,
        speed=config.speed,
        speed_method=method,
    )
    if wav:
        play_audio(wav, speed=config.speed, speed_method=method, background=True)
    elif shutil.which("say"):
        # Last resort fallback
        rate = int(config.speed * 200)
        subprocess.Popen(
            ["say", "-r", str(rate), text],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )


def write_queue_message(text: str, config: TTSConfig) -> Path:
    """Write a queue message JSON file for the daemon."""
    TTS_QUEUE_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = f"{time.time():.6f}"
    msg_id = secrets.token_hex(8)
    queue_file = TTS_QUEUE_DIR / f"{timestamp}_{msg_id}.json"

    method = config.speed_method or "playback"

    message = {
        "id": msg_id,
        "timestamp": float(timestamp),
        "session_id": config.session_id,
        "project": config.project_name,
        "text": text,
        "persona": config.active_persona,
        "speed": config.speed,
        "speed_method": method,
        "voice_kokoro": config.voice_kokoro,
        "voice_kokoro_blend": config.voice_kokoro_blend,
    }

    with open(queue_file, "w") as f:
        json.dump(message, f)

    debug(f"Wrote to queue: {queue_file} (speed={config.speed})")
    return queue_file


def daemon_healthy() -> bool:
    """Check if the TTS daemon is running and healthy."""
    pid_file = Path.home() / ".claude-tts" / "daemon.pid"
    heartbeat_file = Path.home() / ".claude-tts" / "daemon.heartbeat"

    if not pid_file.exists():
        return False

    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 0)  # Check if process exists
    except (ValueError, OSError):
        return False

    if heartbeat_file.exists():
        try:
            last_beat = float(heartbeat_file.read_text().strip())
            if time.time() - last_beat > 30:
                return False
        except (ValueError, OSError):
            pass

    return True


def speak(text: str, config: TTSConfig) -> None:
    """Speak text using the configured mode (direct or queue)."""
    # Truncate to max chars
    if len(text) > config.max_chars:
        text = text[:config.max_chars] + "..."

    if config.mode == "queue":
        if daemon_healthy():
            debug("Queue mode: writing to daemon queue")
            write_queue_message(text, config)
        else:
            debug("Daemon not healthy, skipping speech")
    else:
        speak_direct(text, config)
