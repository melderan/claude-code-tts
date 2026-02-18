"""
The main speak() function - the heart of ai_tts.

This is what everyone calls. It handles:
- Session resolution
- Mute checking
- Persona resolution
- Text filtering
- TTS invocation
- Queue vs direct mode
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from ai_tts.core.config import get_config
from ai_tts.core.session import get_current_session, Session
from ai_tts.core.persona import resolve_persona, Persona
from ai_tts.core.filters import filter_text_for_speech


def speak(
    text: str,
    *,
    persona: str | None = None,
    session: str | Session | None = None,
    speed: float | None = None,
    skip_filter: bool = False,
    force: bool = False,
) -> bool:
    """
    Speak text aloud using TTS.

    Args:
        text: The text to speak.
        persona: Persona name override. If None, uses session/project/global default.
        session: Session ID or Session object. If None, auto-detects.
        speed: Speed override. If None, uses persona default.
        skip_filter: If True, don't filter out code blocks etc.
        force: If True, speak even if session is muted.

    Returns:
        True if speech was played, False if muted or failed.
    """
    config = get_config()

    # Resolve session
    if session is None:
        current_session = get_current_session()
    elif isinstance(session, str):
        current_session = Session.from_config(session)
    else:
        current_session = session

    # Check mute (unless forced)
    if current_session.muted and not force:
        return False

    # Resolve persona (session override > passed arg > session effective > default)
    persona_name = persona or current_session.get_effective_persona()
    active_persona = resolve_persona(persona_name)

    # Resolve speed
    effective_speed = speed or current_session.speed or active_persona.speed

    # Filter text unless skipped
    if not skip_filter:
        text = filter_text_for_speech(text)

    if not text.strip():
        return False

    # Truncate if needed
    if len(text) > config.max_chars:
        text = text[: config.max_chars] + "..."

    # Route to appropriate playback method
    if config.mode == "queue":
        return _speak_queued(text, active_persona, effective_speed, current_session.id)
    else:
        return _speak_direct(text, active_persona, effective_speed)


def _speak_direct(text: str, persona: Persona, speed: float) -> bool:
    """Speak directly using piper + audio player."""
    config = get_config()
    voice_path = persona.get_voice_path()

    if not voice_path.exists():
        # Try to find any voice as fallback
        voices = list(config.voices_dir.glob("*.onnx"))
        if not voices:
            return False
        voice_path = voices[0]

    # Build piper command
    piper_cmd = ["piper", "--model", str(voice_path), "--output-raw"]

    # Add speaker for multi-speaker models
    if persona.speaker is not None:
        piper_cmd.extend(["--speaker", str(persona.speaker)])

    # Handle speed method
    if persona.speed_method == "length_scale":
        # length_scale: lower = faster (0.5 = 2x speed)
        length_scale = 1.0 / speed
        piper_cmd.extend(["--length-scale", str(length_scale)])
    elif persona.speed_method == "hybrid" and persona.length_scale:
        piper_cmd.extend(["--length-scale", str(persona.length_scale)])

    # Detect platform and audio player
    import platform

    system = platform.system()

    if system == "Darwin":
        # macOS: use afplay with speed adjustment
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            wav_path = f.name

        # Generate WAV file
        piper_cmd_wav = piper_cmd.copy()
        piper_cmd_wav[piper_cmd_wav.index("--output-raw")] = "--output_file"
        piper_cmd_wav.append(wav_path)

        try:
            subprocess.run(
                piper_cmd_wav,
                input=text.encode(),
                check=True,
                capture_output=True,
            )

            # Play with speed (if using playback method)
            afplay_cmd = ["afplay", wav_path]
            if persona.speed_method in ("playback", "hybrid"):
                playback_speed = (
                    persona.playback_boost if persona.speed_method == "hybrid" else speed
                )
                afplay_cmd.extend(["-r", str(playback_speed)])

            subprocess.run(afplay_cmd, check=True)
            return True

        except subprocess.CalledProcessError:
            return False
        finally:
            Path(wav_path).unlink(missing_ok=True)

    else:
        # Linux/WSL: pipe to paplay or aplay
        audio_player = "paplay" if _command_exists("paplay") else "aplay"
        play_cmd = [audio_player, "--raw", "--rate=22050", "--format=s16le", "--channels=1"]

        try:
            piper_proc = subprocess.Popen(
                piper_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
            )
            subprocess.run(
                play_cmd,
                stdin=piper_proc.stdout,
                check=True,
            )
            piper_proc.wait()
            return True

        except subprocess.CalledProcessError:
            return False

    return False


def _speak_queued(text: str, persona: Persona, speed: float, session_id: str) -> bool:
    """Queue speech for the daemon to play."""
    import json
    import time

    config = get_config()
    queue_dir = config.config_dir / "queue"
    queue_dir.mkdir(parents=True, exist_ok=True)

    # Create queue message
    message = {
        "text": text,
        "persona": persona.name,
        "speed": speed,
        "session": session_id,
        "timestamp": time.time(),
    }

    # Write to queue file with timestamp name
    queue_file = queue_dir / f"{time.time():.6f}.json"
    with open(queue_file, "w") as f:
        json.dump(message, f)

    return True


def _command_exists(cmd: str) -> bool:
    """Check if a command exists in PATH."""
    import shutil

    return shutil.which(cmd) is not None
