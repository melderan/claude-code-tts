"""Audio generation and playback for Claude Code TTS.

Handles Piper, Kokoro (swift-kokoro), sherpa-onnx (additive backend),
and system audio players. Replaces tts_speak() from tts-lib.sh.
"""

import json
import os
import platform
import re
import secrets
import select
import shutil
import subprocess
import threading
import time
from pathlib import Path

from claude_code_tts.config import (
    MLX_VENV_DIR,
    SHERPA_MODELS_DIR,
    SHERPA_VENV_DIR,
    TTS_CONFIG_DIR,
    TTS_QUEUE_DIR,
    TTSConfig,
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


def _venv_env() -> dict[str, str]:
    """Build env with PYTHONPATH so an isolated venv's Python can find our package."""
    import claude_code_tts as _self_pkg
    pkg_parent = str(Path(_self_pkg.__file__).resolve().parent.parent)
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{pkg_parent}:{existing}" if existing else pkg_parent
    return env


_sherpa_env = _venv_env


class _JsonLineWorker:
    """A long-lived helper process in an isolated venv, spoken to in JSON lines.

    The subclass names the command; this class starts it, waits for the
    ready line, sends one request per line and reads one response per
    line, and restarts the process when it has died. Three things keep the
    daemon's play loop safe from a slow or broken child (clean-room review,
    2026-09-26): the child's stderr goes to a log file, never to a pipe
    nobody drains; every read has a timeout, after which the child is
    killed; and a failed start is not retried for `start_backoff` seconds.
    A lock serialises callers, and a caller that cannot take it within
    `lock_wait` seconds (someone else is loading a model) gives up and
    falls through to the next engine instead of waiting.
    """

    label = "worker"
    ready_timeout = 120.0
    request_timeout = 120.0
    lock_wait = 5.0
    start_backoff = 60.0

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._retry_after = 0.0

    def _command(self) -> list[str] | None:
        """The argv to start the process, or None (already logged) when it cannot start."""
        raise NotImplementedError

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def log_path(self) -> Path:
        """Where the child's stderr accumulates (model load messages, download progress, tracebacks)."""
        slug = re.sub(r"[^a-z0-9]+", "-", self.label.lower()).strip("-")
        return TTS_CONFIG_DIR / "workers" / f"{slug}.log"

    def _open_stderr(self):  # noqa: ANN202  (a file object or subprocess.DEVNULL)
        path = self.log_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > 5 * 1024 * 1024:
                path.write_text("")
            return open(path, "a")
        except OSError:
            return subprocess.DEVNULL

    def _stderr_tail(self, lines: int = 5) -> str:
        try:
            tail = self.log_path().read_text(errors="replace").splitlines()[-lines:]
        except OSError:
            return ""
        return (" | " + " / ".join(line.strip() for line in tail if line.strip())) if tail else ""

    @staticmethod
    def _stop(proc: subprocess.Popen) -> None:
        """Terminate and reap; kill if it will not go."""
        try:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
        except Exception:
            pass

    def _fail_start(self, proc: subprocess.Popen | None, why: str) -> bool:
        if proc is not None:
            self._stop(proc)
        self._retry_after = time.monotonic() + self.start_backoff
        debug(f"{self.label}: {why}; next start attempt in {self.start_backoff:.0f}s{self._stderr_tail()}")
        return False

    def _start(self) -> bool:
        remaining = self._retry_after - time.monotonic()
        if remaining > 0:
            debug(f"{self.label}: last start failed, not trying again for {remaining:.0f}s")
            return False
        cmd = self._command()
        if cmd is None:
            return False
        err = self._open_stderr()
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=err,
                text=True,
                env=_venv_env(),
            )
        except OSError as e:
            return self._fail_start(None, f"start failed: {e}")
        finally:
            if err is not subprocess.DEVNULL:
                err.close()
        assert proc.stdout is not None
        if not select.select([proc.stdout], [], [], self.ready_timeout)[0]:
            return self._fail_start(proc, f"timed out waiting for ready signal (>{self.ready_timeout:.0f}s)")
        ready_line = proc.stdout.readline()
        try:
            resp = json.loads(ready_line)
        except (json.JSONDecodeError, TypeError):
            return self._fail_start(proc, f"unexpected ready response: {ready_line!r}")
        if not isinstance(resp, dict) or not resp.get("ready"):
            error = resp.get("error") if isinstance(resp, dict) else resp
            return self._fail_start(proc, f"failed to start: {error}")
        self._proc = proc
        self._retry_after = 0.0
        debug(f"{self.label} started (PID {proc.pid}); its stderr is {self.log_path()}")
        return True

    def ensure_started(self) -> bool:
        with self._lock:
            return self._alive() or self._start()

    def request(self, req: dict) -> dict | None:
        """Send one request and return its response, or None on any failure or when busy."""
        if not self._lock.acquire(timeout=self.lock_wait):
            debug(f"{self.label}: busy for {self.lock_wait:.0f}s (another caller is loading or speaking); giving up this request")
            return None
        try:
            if not self._alive() and not self._start():
                return None
            proc = self._proc
            assert proc is not None and proc.stdin is not None and proc.stdout is not None
            try:
                proc.stdin.write(json.dumps(req) + "\n")
                proc.stdin.flush()
                if not select.select([proc.stdout], [], [], self.request_timeout)[0]:
                    debug(f"{self.label}: no response in {self.request_timeout:.0f}s; killing it{self._stderr_tail()}")
                    self._stop(proc)
                    self._proc = None
                    return None
                resp = json.loads(proc.stdout.readline())
                return resp if isinstance(resp, dict) else None
            except (OSError, json.JSONDecodeError) as e:
                debug(f"{self.label}: communication error: {e}{self._stderr_tail()}")
                self._stop(proc)
                self._proc = None
                return None
        finally:
            self._lock.release()


class _SherpaWorker(_JsonLineWorker):
    """Persistent sherpa-onnx subprocess: model loaded once, reused per request."""

    label = "sherpa worker"

    def __init__(self, model_id: str) -> None:
        super().__init__()
        self.model_id = model_id

    def _command(self) -> list[str] | None:
        model_dir = SHERPA_MODELS_DIR / self.model_id
        if not model_dir.is_dir():
            debug(f"sherpa worker: model dir missing: {model_dir}")
            return None
        if not _sherpa_available():
            debug(f"sherpa worker: venv not bootstrapped at {SHERPA_VENV_DIR}")
            return None
        return [
            str(_sherpa_python()),
            "-m", "claude_code_tts.sherpa_speak",
            "--serve",
            "--model-dir", str(model_dir),
        ]

    def generate(self, text: str, *, speaker: int, speed: float, output_path: Path) -> bool:
        resp = self.request({"text": text, "output": str(output_path), "speaker": speaker, "speed": speed})
        if resp is None:
            return False
        if resp.get("ok"):
            return True
        debug(f"sherpa worker: generation failed: {resp.get('error')}")
        return False


_sherpa_workers: dict[str, _SherpaWorker] = {}


def _get_sherpa_worker(model_id: str) -> _SherpaWorker:
    return _sherpa_workers.setdefault(model_id, _SherpaWorker(model_id))


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


def _mlx_python() -> Path:
    """Path to the Python interpreter inside the mlx venv."""
    return MLX_VENV_DIR / "bin" / "python"


def _mlx_available() -> bool:
    """True iff `claude-tts-install --enable-mlx` has run here. Never bootstraps from the speak path."""
    return _mlx_python().is_file()


class _MlxWorker(_JsonLineWorker):
    """Persistent mlx-audio subprocess: one model held in memory per Hugging Face id.

    The first start of a model not yet in the Hugging Face cache downloads
    it, which can take minutes; `claude-tts mlx pull <id>` does that ahead
    of time, so the long ready timeout is a last resort, not the plan.
    """

    label = "mlx worker"
    ready_timeout = 600.0

    def __init__(self, model_id: str) -> None:
        super().__init__()
        self.model_id = model_id

    def _command(self) -> list[str] | None:
        if not _mlx_available():
            debug(f"mlx worker: venv not bootstrapped at {MLX_VENV_DIR} (claude-tts-install --enable-mlx)")
            return None
        return [
            str(_mlx_python()),
            "-m", "claude_code_tts.mlx_speak",
            "--serve",
            "--model", self.model_id,
        ]

    def generate(self, text: str, *, voice: str, speed: float, lang_code: str, output_path: Path) -> bool:
        resp = self.request({
            "text": text, "output": str(output_path),
            "voice": voice, "speed": speed, "lang_code": lang_code,
        })
        if resp is None:
            return False
        if resp.get("ok"):
            return True
        debug(f"mlx worker: generation failed: {resp.get('error')}")
        return False


_mlx_workers: dict[str, _MlxWorker] = {}


def _get_mlx_worker(model_id: str) -> _MlxWorker:
    # setdefault, not check-then-assign: the warm-up thread and the play loop
    # may ask for the same model at the same moment, and two workers would
    # mean two model processes.
    return _mlx_workers.setdefault(model_id, _MlxWorker(model_id))


def warm_mlx_workers(personas: dict) -> list[str]:
    """Start an mlx worker for every persona with voice_mlx set; returns the models that came up.

    Meant for a background thread at daemon start: a model load takes
    seconds from the cache and minutes on first download, and the play
    loop must not wait on either.
    """
    if not _mlx_available():
        return []
    ready: list[str] = []
    seen: set[str] = set()
    for persona_config in personas.values():
        model_id = persona_config.get("voice_mlx", "")
        if model_id and model_id not in seen:
            seen.add(model_id)
            debug(f"warming mlx worker for model: {model_id}")
            if _get_mlx_worker(model_id).ensure_started():
                ready.append(model_id)
    return ready


def _generate_mlx(
    text: str,
    *,
    model_id: str,
    voice: str,
    speed: float,
    lang_code: str,
    output_path: Path,
) -> Path | None:
    """Generate speech via the persistent mlx worker (model stays in memory)."""
    if not _mlx_available():
        _set_last_error(f"mlx backend not enabled at {MLX_VENV_DIR}")
        return None
    worker = _get_mlx_worker(model_id)
    if worker.generate(text, voice=voice, speed=speed, lang_code=lang_code, output_path=output_path):
        if output_path.exists():
            return output_path
    _set_last_error(f"mlx worker for {model_id} produced no audio (see {worker.log_path()})")
    return None


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


_LAST_ERROR = ""


def _set_last_error(msg: str) -> None:
    global _LAST_ERROR
    _LAST_ERROR = msg
    if msg:
        debug(f"speech generation: {msg}")


def last_error() -> str:
    """Why the most recent generate_speech() returned None, or empty."""
    return _LAST_ERROR


def generate_speech(
    text: str,
    *,
    voice_path: Path | None = None,
    voice_kokoro: str = "",
    voice_kokoro_blend: str = "",
    voice_sherpa: str = "",
    speaker_sherpa: int = -1,
    voice_mlx: str = "",
    speaker_mlx: str = "",
    lang_mlx: str = "",
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
    _set_last_error("")

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

    # Priority 3: mlx-audio (opt-in per persona via voice_mlx; Apple silicon).
    # Speed follows the Piper rule: synthesised into the audio only when the
    # persona's speed_method is length_scale, otherwise applied at playback,
    # because not every mlx model honours a speed argument.
    if voice_mlx:
        wav = _generate_mlx(
            text,
            model_id=voice_mlx,
            voice=speaker_mlx,
            speed=speed if speed_method == "length_scale" and speed > 0 else 1.0,
            lang_code=lang_mlx,
            output_path=output_path,
        )
        if wav:
            _apply_pitch_filter(wav, pitch_filter)
            return wav

    # Priority 4: Sherpa-onnx (opt-in per persona via voice_sherpa).
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

    # Priority 5: Piper
    if not shutil.which("piper"):
        if not last_error():  # an engine asked for above already said what went wrong
            _set_last_error(f"piper not on PATH ({os.environ.get('PATH', '')})")
    elif not (voice_path and voice_path.exists()):
        if not last_error():
            _set_last_error(f"voice model missing: {voice_path}")
    else:
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
            proc = subprocess.run(
                cmd, input=text, text=True, capture_output=True, timeout=30,
            )
            if output_path.exists():
                _apply_pitch_filter(output_path, pitch_filter)
                _set_last_error("")
                return output_path
            _set_last_error(f"piper exit {proc.returncode}: {proc.stderr.strip()[-300:]}")
        except (subprocess.TimeoutExpired, OSError) as e:
            _set_last_error(f"piper failed to run: {e}")

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

    # Sherpa applies speed during synthesis, so it is not applied again at
    # playback; only when sherpa is the engine that actually plays.
    sherpa_plays = bool(config.voice_sherpa) and not (config.voice_kokoro or config.voice_kokoro_blend or config.voice_mlx)
    effective_method = "length_scale" if sherpa_plays else method

    wav = generate_speech(
        text,
        voice_path=config.voice_path,
        voice_kokoro=config.voice_kokoro,
        voice_kokoro_blend=config.voice_kokoro_blend,
        voice_sherpa=config.voice_sherpa,
        speaker_sherpa=config.speaker_sherpa,
        voice_mlx=config.voice_mlx,
        speaker_mlx=config.speaker_mlx,
        lang_mlx=config.lang_mlx,
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
        "voice_mlx": config.voice_mlx,
        "speaker_mlx": config.speaker_mlx,
        "lang_mlx": config.lang_mlx,
        "pitch_filter": config.pitch_filter,
    }

    # Write-then-rename so the daemon never globs a half-written file.
    tmp_file = queue_file.with_suffix(".tmp")
    with open(tmp_file, "w") as f:
        json.dump(message, f)
    tmp_file.rename(queue_file)

    debug(f"Wrote to queue: {queue_file} (speed={config.speed})")
    return queue_file


def daemon_healthy() -> bool:
    """Check if the TTS daemon is running and healthy."""
    pid_file = Path.home() / ".claude-tts" / "daemon.pid"
    heartbeat_file = Path.home() / ".claude-tts" / "daemon.heartbeat"

    # A fresh heartbeat is proof of life even where the daemon's pid is not
    # visible (a container or sandbox sharing ~/.claude-tts with the host).
    if heartbeat_file.exists():
        try:
            last_beat = float(heartbeat_file.read_text().strip())
            return time.time() - last_beat <= 30
        except (ValueError, OSError):
            pass

    if not pid_file.exists():
        return False

    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 0)  # Check if process exists
    except (ValueError, OSError):
        return False

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
