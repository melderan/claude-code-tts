"""Audio generation and playback for Claude Code TTS.

Handles Piper, Kokoro (swift-kokoro), sherpa-onnx (additive backend),
and system audio players. Replaces tts_speak() from tts-lib.sh.
"""

import json
import os
import platform
import secrets
import select
import shutil
import subprocess
import time
from pathlib import Path

from claude_code_tts.config import (
    TTSConfig,
    TTS_QUEUE_DIR,
    SHERPA_VENV_DIR,
    SHERPA_MODELS_DIR,
    debug,
)


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



def _sherpa_python() -> Path:
    """Path to the Python interpreter inside the sherpa venv."""
    return SHERPA_VENV_DIR / "bin" / "python"


def _sherpa_available() -> bool:
    """Return True iff the sherpa venv is bootstrapped and usable.

    We do NOT auto-bootstrap from this hot path — bootstrap is an explicit
    step run via `claude-tts-install --enable-sherpa` (or similar). This
    keeps the speak path fast and predictable.
    """
    py = _sherpa_python()
    return py.is_file()


def _sherpa_env() -> dict[str, str]:
    """Build env with PYTHONPATH so the sherpa venv can find our package."""
    import claude_code_tts as _self_pkg
    pkg_parent = str(Path(_self_pkg.__file__).resolve().parent.parent)
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{pkg_parent}:{existing}" if existing else pkg_parent
    return env


class _SherpaWorker:
    """Persistent sherpa-onnx subprocess — model loaded once, reused per request.

    Keeps one long-lived Python process alive in the isolated sherpa venv so
    the ONNX model stays in memory. Each call to generate() sends a JSON-line
    request and reads a JSON-line response. Auto-restarts if the process dies.
    """

    def __init__(self, model_id: str) -> None:
        self.model_id = model_id
        self._proc: subprocess.Popen | None = None

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _start(self) -> bool:
        model_dir = SHERPA_MODELS_DIR / self.model_id
        if not model_dir.is_dir():
            debug(f"sherpa worker: model dir missing: {model_dir}")
            return False
        if not _sherpa_available():
            debug(f"sherpa worker: venv not bootstrapped at {SHERPA_VENV_DIR}")
            return False

        try:
            proc = subprocess.Popen(
                [
                    str(_sherpa_python()),
                    "-m", "claude_code_tts.sherpa_speak",
                    "--serve",
                    "--model-dir", str(model_dir),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=_sherpa_env(),
            )
            # Wait up to 120s for model load — no-data after that means a hang.
            ready = select.select([proc.stdout], [], [], 120.0)[0]  # type: ignore[union-attr]
            if not ready:
                debug("sherpa worker: timed out waiting for ready signal (>120s)")
                proc.terminate()
                return False
            ready_line = proc.stdout.readline()  # type: ignore[union-attr]
            try:
                resp = json.loads(ready_line)
            except (json.JSONDecodeError, TypeError):
                debug(f"sherpa worker: unexpected ready response: {ready_line!r}")
                proc.terminate()
                return False
            if not resp.get("ready"):
                debug(f"sherpa worker: failed to start: {resp.get('error')}")
                proc.terminate()
                return False
            self._proc = proc
            debug(f"sherpa worker started for model {self.model_id} (PID {proc.pid})")
            return True
        except OSError as e:
            debug(f"sherpa worker start failed: {e}")
            return False

    def generate(self, text: str, *, speaker: int, speed: float, output_path: Path) -> bool:
        if not self._alive():
            if not self._start():
                return False

        req = json.dumps({
            "text": text,
            "output": str(output_path),
            "speaker": speaker,
            "speed": speed,
        })
        try:
            assert self._proc and self._proc.stdin and self._proc.stdout
            self._proc.stdin.write(req + "\n")
            self._proc.stdin.flush()
            resp_line = self._proc.stdout.readline()
            resp = json.loads(resp_line)
            if resp.get("ok"):
                return True
            debug(f"sherpa worker: generation failed: {resp.get('error')}")
            return False
        except (OSError, json.JSONDecodeError, AssertionError) as e:
            debug(f"sherpa worker: communication error: {e}")
            if self._proc:
                try:
                    self._proc.terminate()
                except Exception:
                    pass
                self._proc = None
            return False


_sherpa_workers: dict[str, _SherpaWorker] = {}


def _get_sherpa_worker(model_id: str) -> _SherpaWorker:
    if model_id not in _sherpa_workers:
        _sherpa_workers[model_id] = _SherpaWorker(model_id)
    return _sherpa_workers[model_id]


def warm_sherpa_workers(personas: dict) -> None:
    """Pre-start sherpa workers for all personas that have voice_sherpa set.

    Call at daemon startup so the first speech request doesn't block on model
    load. Blocking here is intentional — better to wait at startup than to
    freeze mid-session.
    """
    seen: set[str] = set()
    for persona_config in personas.values():
        model_id = persona_config.get("voice_sherpa", "")
        if model_id and model_id not in seen:
            seen.add(model_id)
            worker = _get_sherpa_worker(model_id)
            if not worker._alive():
                debug(f"warming sherpa worker for model: {model_id}")
                worker._start()  # blocks until model is loaded


def _generate_sherpa(
    text: str,
    *,
    model_id: str,
    speaker: int,
    speed: float,
    output_path: Path,
) -> Path | None:
    """Generate speech via the persistent sherpa worker (model stays in memory)."""
    model_dir = SHERPA_MODELS_DIR / model_id
    if not model_dir.is_dir():
        debug(f"sherpa: model dir missing: {model_dir}")
        return None
    if not _sherpa_available():
        debug(f"sherpa: venv not bootstrapped at {SHERPA_VENV_DIR}")
        return None

    worker = _get_sherpa_worker(model_id)
    sid = speaker if speaker >= 0 else 0
    if worker.generate(text, speaker=sid, speed=speed, output_path=output_path):
        if output_path.exists():
            return output_path
    return None


def _apply_pitch_filter(path: Path, pitch_filter: str) -> None:
    """Apply ffmpeg pitch filter in-place. Silent no-op on any failure."""
    if not pitch_filter or not path.exists() or not shutil.which("ffmpeg"):
        return
    tmp = path.with_suffix(".pf.wav")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error",
             "-i", str(path), "-af", pitch_filter, str(tmp)],
            capture_output=True, check=True, timeout=15,
        )
        tmp.replace(path)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
        debug(f"pitch_filter ffmpeg failed: {e}")
        tmp.unlink(missing_ok=True)


def generate_speech(
    text: str,
    *,
    voice_path: Path | None = None,
    voice_kokoro: str = "",
    voice_kokoro_blend: str = "",
    voice_sherpa: str = "",
    speaker_sherpa: int = -1,
    speed: float = 2.0,
    speed_method: str = "",
    speaker: int | None = None,
    output_path: Path | None = None,
    noise_scale: float | None = None,
    noise_w_scale: float | None = None,
    sentence_silence: float | None = None,
    pitch_filter: str = "",
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
                _apply_pitch_filter(output_path, pitch_filter)
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
                _apply_pitch_filter(output_path, pitch_filter)
                return output_path
        except (subprocess.TimeoutExpired, OSError):
            pass

    # Priority 3: Sherpa-onnx (opt-in per persona via voice_sherpa).
    # Existing personas have voice_sherpa="" and never enter this branch —
    # they continue to use Piper / Kokoro exactly as before. New personas
    # set voice_sherpa to a model dir name under SHERPA_MODELS_DIR.
    if voice_sherpa:
        wav = _generate_sherpa(
            text,
            model_id=voice_sherpa,
            speaker=speaker_sherpa,
            speed=speed,
            output_path=output_path,
        )
        if wav:
            _apply_pitch_filter(wav, pitch_filter)
            return wav

    # Priority 4: Piper
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
                _apply_pitch_filter(output_path, pitch_filter)
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

    # Sherpa applies speed during synthesis — don't also apply it at playback
    effective_method = "length_scale" if config.voice_sherpa else method

    wav = generate_speech(
        text,
        voice_path=config.voice_path,
        voice_kokoro=config.voice_kokoro,
        voice_kokoro_blend=config.voice_kokoro_blend,
        voice_sherpa=config.voice_sherpa,
        speaker_sherpa=config.speaker_sherpa,
        speed=config.speed,
        speed_method=effective_method,
        pitch_filter=config.pitch_filter,
    )
    if wav:
        play_audio(wav, speed=config.speed, speed_method=effective_method, background=True)
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
        "voice_sherpa": config.voice_sherpa,
        "speaker_sherpa": config.speaker_sherpa,
        "pitch_filter": config.pitch_filter,
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
