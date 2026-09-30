"""TTS daemon — multi-session message bus for queue mode.

Monitors ~/.claude-tts/queue/ and plays TTS messages in order,
preventing overlap between multiple Claude sessions.

Absorbed from scripts/tts-daemon.py into the Python CLI.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import wave
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from claude_code_tts.audio import _set_last_error as audio_set_last_error
from claude_code_tts.audio import (
    detect_player,
    reap_idle_workers,
    warm_mlx_workers,
    warm_sherpa_workers,
)
from claude_code_tts.audio import generate_speech as _generate_speech
from claude_code_tts.audio import last_error as audio_last_error
from claude_code_tts.bridge import (
    JOBS,
    Bridge,
    concat_wavs,
    estimated_marks,
    get_http_config,
    split_sentences,
    synthesize_with_marks,
    to_playback_ms,
)
from claude_code_tts.config import (
    DEFAULT_VOICE,
    TTS_CONFIG_DIR,
    TTS_QUEUE_DIR,
    VOICES_DIR,
    load_raw_config,
)
from claude_code_tts.handy import AnalyzerThread, save_speech_wav
from claude_code_tts.level import normalize as normalize_level
from claude_code_tts.mic_watcher import MicWatcher
from claude_code_tts.state import (
    UNSET,
    acquire_lock,
    clear_current_message,
    clear_heartbeat,
    clear_pid,
    get_interrupted_message,
    is_daemon_running,
    pid_alive,
    read_playback_state,
    release_lock,
    set_paused,
    take_respawn_marker,
    write_heartbeat,
    write_pid,
    write_playback_state,
    write_protocol_marker,
    write_release_marker,
    write_respawn_marker,
)
from claude_code_tts.tone import DEFAULT_TONE, ToneParams, classify_tone

# --- Daemon path constants (the state files live in state.py) ---

LOG_FILE = TTS_CONFIG_DIR / "daemon.log"
# Rotate once to daemon.log.1 past this size; the daemon is meant to run for weeks.
LOG_MAX_BYTES = 5 * 1024 * 1024
# While audio plays: refresh the heartbeat this often, and say so in the log this often.
HEARTBEAT_INTERVAL_S = 1.0
PLAYING_LOG_INTERVAL_S = 30.0
# How often the loop looks for model workers idle past worker_idle_unload_s.
WORKER_REAP_EVERY_S = 30.0

# Default voice model
# (persona, voice) pairs already reported as missing, so the log says it once.
_missing_voice_warned: set[tuple[str, str]] = set()


def resolve_piper_voice(
    persona: str, persona_config: dict, *, other_engine: bool = False
) -> tuple[str, bool]:
    """Return the Piper voice to use for a persona and whether it is the default standing in.

    A missing voice is logged once per persona, not per message: a silent
    substitution sounds like the wrong voice with no trail to follow (found
    2026-09-21 when a new persona's model was on another machine). When a
    voice warned about earlier turns up installed, that is logged once too,
    so the log can say which voice is playing without anyone listening for
    it (asked by the house room, 2026-09-23). With other_engine the persona
    speaks through Kokoro or sherpa and its Piper voice is not checked.
    """
    voice_name = persona_config.get("voice", DEFAULT_VOICE)
    if other_engine:
        return voice_name, False
    key = (persona, voice_name)
    voice_path = VOICES_DIR / f"{voice_name}.onnx"
    if voice_path.exists():
        if key in _missing_voice_warned:
            _missing_voice_warned.discard(key)
            log(
                f"Voice {voice_name} for persona {persona} is installed now at {voice_path}; using it"
            )
        return voice_name, False
    if key not in _missing_voice_warned:
        _missing_voice_warned.add(key)
        log(
            f"Voice {voice_name} for persona {persona} is not installed at {voice_path}; "
            f"using {DEFAULT_VOICE}. Fetch it with: claude-tts-install --voice {voice_name}",
            "WARN",
        )
    return DEFAULT_VOICE, True


def describe_voice(
    persona: str,
    persona_config: dict,
    voice_kokoro: str = "",
    voice_kokoro_blend: str = "",
    voice_mlx: str = "",
    speaker_mlx: str = "",
) -> str:
    """One token naming the engine and voice a message will play with, for the log.

    `kokoro:<voice>`, `mlx:<model>[#voice]`, `sherpa:<model>[#speaker]`, or
    the Piper voice name, with ` (fallback)` when the default is standing in
    for a missing model.
    """
    if voice_mlx:
        # The message chose mlx for itself (engine "mlx"): its voice, not the persona's.
        return f"mlx:{voice_mlx}" + (f"#{speaker_mlx}" if speaker_mlx else "")
    blend = voice_kokoro_blend or persona_config.get("voice_kokoro_blend", "")
    kokoro = voice_kokoro or persona_config.get("voice_kokoro", "")
    if blend:
        return f"kokoro:{blend}"
    if kokoro:
        return f"kokoro:{kokoro}"
    mlx = persona_config.get("voice_mlx", "")
    if mlx:
        speaker_mlx = persona_config.get("speaker_mlx", "")
        return f"mlx:{mlx}" + (f"#{speaker_mlx}" if speaker_mlx else "")
    sherpa = persona_config.get("voice_sherpa", "")
    if sherpa:
        speaker = int(persona_config.get("speaker_sherpa", -1))
        return f"sherpa:{sherpa}" + (f"#{speaker}" if speaker >= 0 else "")
    voice, fell_back = resolve_piper_voice(persona, persona_config)
    return f"{voice} (fallback)" if fell_back else voice


# Global state
_shutdown_requested = False
_daemon_mode = False
# log() is called from the synth, bridge and mic-watcher threads too.
_log_lock = threading.Lock()


# --- Logging ---


def log(msg: str, level: str = "INFO") -> None:
    """Log a message to the daemon log file."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] [{level}] {msg}\n"

    if not _daemon_mode:
        print(line.strip())

    try:
        with _log_lock:
            try:
                if LOG_FILE.stat().st_size > LOG_MAX_BYTES:
                    LOG_FILE.replace(LOG_FILE.with_suffix(".log.1"))
            except OSError:
                pass
            with open(LOG_FILE, "a") as f:
                f.write(line)
    except Exception:
        pass


# An interrupted message in the state file older than this is history, not a resume.
RESUME_AFTER_RESTART_S = 300.0
# stop_daemon forces only after this long with the daemon idle (not speaking) and still alive.
STOP_IDLE_GRACE_S = 15.0


def kill_orphan_player(pid: int | None) -> bool:
    """Stop a player process a previous daemon left running; True if one was killed.

    2026-09-29: a daemon force-killed mid-message left its afplay alive, and
    the next daemon replayed the same message over it. Two voices at once.
    """
    if not pid:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    for _ in range(10):
        time.sleep(0.1)
        try:
            os.kill(pid, 0)
        except OSError:
            return True
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    return True


# --- WAV position helpers ---

# If less than this many seconds of audio remain, skip replay entirely
NEAR_END_THRESHOLD = 2.0

# On resume, rewind this many REAL seconds (what you hear) from where
# we were interrupted. Converted to WAV-time using playback speed,
# so it feels like the same amount of re-listening regardless of speed.
# Default; "resume_rewind_seconds" in config.json overrides it.
REWIND_REAL_SECONDS = 3.0


def resume_rewind_seconds() -> float:
    """How many real seconds to rewind on resume: config.json or the default."""
    try:
        value = float(load_raw_config().get("resume_rewind_seconds", REWIND_REAL_SECONDS))
    except (TypeError, ValueError):
        return REWIND_REAL_SECONDS
    return max(0.0, value)


def get_wav_duration(wav_file: Path) -> float:
    """Get duration of a WAV file in seconds."""
    try:
        with wave.open(str(wav_file), "rb") as w:
            return w.getnframes() / w.getframerate()
    except Exception:
        return 0.0


def trim_wav(src: Path, dst: Path, start_seconds: float) -> bool:
    """Trim a WAV file, writing from start_seconds onward to dst.

    Returns True if the trimmed file has audio, False if nothing left.
    """
    try:
        with wave.open(str(src), "rb") as r:
            params = r.getparams()
            start_frame = int(start_seconds * params.framerate)
            if start_frame >= params.nframes:
                return False
            r.setpos(start_frame)
            remaining_frames = params.nframes - start_frame
            data = r.readframes(remaining_frames)

        with wave.open(str(dst), "wb") as w:
            w.setparams(params._replace(nframes=0))
            w.writeframes(data)
        return True
    except Exception as e:
        log(f"Failed to trim WAV: {e}", "ERROR")
        return False


def calculate_audio_position(elapsed_real: float, speed: float, speed_method: str) -> float:
    """Calculate how far into the original WAV we've played.

    With playback speed (afplay -r), real time and audio time differ:
    at 2x speed, 5 real seconds = 10 seconds of audio heard.
    With length_scale, speed is baked into the WAV, so real time = audio time.
    """
    if speed_method == "playback" and speed > 0:
        return elapsed_real * speed
    return elapsed_real


def rewind_amount(speed: float, speed_method: str) -> float:
    """Calculate WAV-time rewind amount for the current playback speed.

    We want REWIND_REAL_SECONDS of re-listening regardless of speed.
    At 2x playback, 3 real seconds = 6 WAV seconds to rewind.
    At 1x, it's just 3. At 0.5x, it's 1.5.
    With length_scale, speed is baked in, so real = WAV time.
    """
    real = resume_rewind_seconds()
    if speed_method == "playback" and speed > 0:
        return real * speed
    return real


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
        # A loaded model (sherpa, mlx) stays resident between messages and is
        # unloaded after this long unused; 0 keeps it for the daemon's life.
        "worker_idle_unload_s": 1800,
        # Synthesize the next message while the current one plays.
        "prefetch_next": True,
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


def normalize_target() -> float | None:
    """queue.normalize_dbfs: the speech level every WAV is brought to; None or false disables."""
    raw = load_raw_config().get("queue", {}).get("normalize_dbfs", DEFAULT_NORMALIZE_DBFS)
    if raw is None or raw is False:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def persona_gain_db(persona: str) -> float:
    """A persona's gain_db, added on top of the target for taste; 0 when unset."""
    try:
        return float(load_raw_config().get("personas", {}).get(persona, {}).get("gain_db", 0.0))
    except (TypeError, ValueError):
        return 0.0


def level_after_synthesis(output_file: Path, persona: str) -> None:
    """Even out loudness across engines: one line in the log per WAV, numbers first."""
    target = normalize_target()
    if target is None or not output_file.exists():
        return
    try:
        result = normalize_level(output_file, target, extra_db=persona_gain_db(persona))
    except Exception as e:  # a level problem must never cost the message
        log(f"Level [{persona}]: skipped ({e})", "WARN")
        return
    if result is None:
        return
    before, gain = result
    log(
        f"Level [{persona}]: speech {before.speech_dbfs:.1f} dBFS, peak {before.peak_dbfs:.1f}, "
        f"{before.seconds:.1f}s -> gain {gain:+.1f} dB (target {target:g})"
    )


def daemon_generate_speech(
    text: str, persona: str, output_file: Path, *args: Any, **kwargs: Any
) -> bool:
    """Synthesize, then level: the one funnel every engine and every path goes through."""
    ok = _generate_speech_unleveled(text, persona, output_file, *args, **kwargs)
    if ok:
        level_after_synthesis(output_file, persona)
    return ok


def _generate_speech_unleveled(
    text: str,
    persona: str,
    output_file: Path,
    voice_kokoro_override: str = "",
    voice_kokoro_blend_override: str = "",
    tone: ToneParams | None = None,
    voice_mlx_override: str = "",
    speaker_mlx_override: str = "",
    lang_mlx_override: str = "",
) -> bool:
    """Generate speech for daemon playback, resolving persona config.

    A non-empty voice_mlx_override means the message chose mlx for itself
    (`claude-tts speak --voice-mlx`): its model, voice and language play,
    and the persona's kokoro and sherpa voices stand aside for this call.

    If tone is provided, Piper's expressiveness parameters (noise_scale,
    noise_w_scale, sentence_silence) are set from the tone preset.

    Returns True on success.
    """
    import re

    # Clean punctuation clusters that make Piper produce noise artifacts.
    # Applied here so ALL text hitting Piper is safe, regardless of source.
    text = re.sub(r"([.!?])[)\]}>\"']+", r"\1", text)

    persona_config = get_persona_config(persona)

    kokoro_blend = voice_kokoro_blend_override or persona_config.get("voice_kokoro_blend", "")
    kokoro_voice = voice_kokoro_override or persona_config.get("voice_kokoro", "")
    speed = persona_config.get("speed", 2.0)
    speed_method = persona_config.get("speed_method", "playback")
    voice_sherpa = persona_config.get("voice_sherpa", "")
    speaker_sherpa = int(persona_config.get("speaker_sherpa", -1))
    voice_mlx = persona_config.get("voice_mlx", "")
    speaker_mlx = persona_config.get("speaker_mlx", "")
    lang_mlx = persona_config.get("lang_mlx", "")
    if voice_mlx_override:
        voice_mlx, speaker_mlx, lang_mlx = (
            voice_mlx_override,
            speaker_mlx_override,
            lang_mlx_override,
        )
        kokoro_voice = kokoro_blend = voice_sherpa = ""
    pitch_filter = persona_config.get("pitch_filter", "")
    voice_name, _ = resolve_piper_voice(
        persona, persona_config, other_engine=bool(voice_sherpa or voice_mlx or kokoro_voice)
    )
    voice_path = VOICES_DIR / f"{voice_name}.onnx"

    # Tone-aware generation parameters.
    # Only pass when tone is non-default — otherwise let Piper use its
    # own built-in defaults which are tuned per voice model.
    tone_kwargs: dict = {}
    if tone is not None and tone.name != "neutral":
        tone_kwargs["noise_scale"] = tone.noise_scale
        tone_kwargs["noise_w_scale"] = tone.noise_w_scale
        tone_kwargs["sentence_silence"] = tone.sentence_silence

    result = _generate_speech(
        text,
        voice_path=voice_path if voice_path.exists() else None,
        voice_kokoro=kokoro_voice,
        voice_kokoro_blend=kokoro_blend,
        voice_sherpa=voice_sherpa,
        speaker_sherpa=speaker_sherpa,
        voice_mlx=voice_mlx,
        speaker_mlx=speaker_mlx,
        lang_mlx=lang_mlx,
        speed=speed,
        speed_method=speed_method,
        output_path=output_file,
        pitch_filter=pitch_filter,
        **tone_kwargs,
    )
    return result is not None


def synthesize_message(
    text: str,
    persona: str,
    audio_file: Path,
    *,
    want_marks: bool,
    speed: float,
    speed_method: str,
    voice_kokoro_override: str = "",
    voice_kokoro_blend_override: str = "",
    tone: ToneParams | None = None,
    voice_mlx_override: str = "",
    speaker_mlx_override: str = "",
    lang_mlx_override: str = "",
) -> tuple[bool, dict | None]:
    """Generate the WAV for one queue message, with timing marks if asked.

    Marks come from sentence-by-sentence synthesis (see bridge.py). If that
    fails for any sentence we fall back to one-piece synthesis and estimated
    marks, so a page asking for timing still gets audio and a best guess.
    """

    gen = sentence_generator(
        persona,
        voice_kokoro=voice_kokoro_override,
        voice_kokoro_blend=voice_kokoro_blend_override,
        tone=tone,
        voice_mlx=voice_mlx_override,
        speaker_mlx=speaker_mlx_override,
        lang_mlx=lang_mlx_override,
    )

    playback_speed = speed if speed_method == "playback" else 1.0
    if want_marks:
        marks = synthesize_with_marks(text, gen, audio_file, playback_speed=playback_speed)
        if marks is not None:
            return True, marks
        log("Sentence-level synthesis failed, falling back to one piece", "WARN")
    if not gen(text, audio_file):
        return False, None
    if want_marks:
        return True, estimated_marks(
            text, get_wav_duration(audio_file), playback_speed=playback_speed
        )
    return True, None


def daemon_play_audio(
    wav_file: Path, speed: float = 1.0, job_id: str | None = None
) -> tuple[bool, bool, float]:
    """Play a WAV file with pause-aware polling.

    Returns (success, was_killed, elapsed_seconds).
    was_killed=True means audio was interrupted by pause.
    elapsed_seconds is real wall-clock time the audio played.
    job_id names a bridge job; a /stop for its source cancels playback here,
    which returns (False, False, elapsed) with the job marked cancelled.
    """
    player = detect_player()
    if not player:
        log("No audio player available", "ERROR")
        return (False, False, 0.0)

    try:
        cmd = list(player)
        if cmd[0] == "afplay" and speed != 1.0:
            cmd.extend(["-r", str(speed)])
        cmd.append(str(wav_file))

        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        write_playback_state(audio_pid=proc.pid)
        start_time = time.monotonic()
        log(f"Audio started (PID {proc.pid}), polling for pause...")

        # Heartbeat and the occasional log line are on the clock, not on a poll
        # count: a slow machine that takes 100 ms per poll must still refresh
        # the heartbeat every second or hooks will judge the daemon dead.
        last_heartbeat = start_time
        last_status_log = start_time
        while proc.poll() is None:
            state = read_playback_state()
            now = time.monotonic()
            if now - last_heartbeat >= HEARTBEAT_INTERVAL_S:
                write_heartbeat()
                last_heartbeat = now
            if now - last_status_log >= PLAYING_LOG_INTERVAL_S:
                log(
                    f"Still playing (PID {proc.pid}, {now - start_time:.0f}s, paused={state.get('paused')})"
                )
                last_status_log = now
            if JOBS.take_cancel(job_id):
                elapsed = time.monotonic() - start_time
                log(f"Audio cancelled by bridge (PID {proc.pid}) after {elapsed:.1f}s")
                try:
                    proc.terminate()
                    proc.wait(timeout=1)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                write_playback_state(audio_pid=None)
                JOBS.update(job_id, state="cancelled", position_ms=round(elapsed * 1000))
                return (False, False, elapsed)
            if state.get("paused"):
                elapsed = time.monotonic() - start_time
                log(f"Audio killed for pause (PID {proc.pid}) after {elapsed:.1f}s real time")
                try:
                    proc.terminate()
                    proc.wait(timeout=1)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                write_playback_state(audio_pid=None)
                return (False, True, elapsed)
            time.sleep(0.05)

        elapsed = time.monotonic() - start_time
        write_playback_state(audio_pid=None)
        return (proc.returncode == 0, False, elapsed)
    except Exception as e:
        log(f"Audio playback failed: {e}", "ERROR")
        write_playback_state(audio_pid=None)
        return (False, False, 0.0)


# --- Sentence streaming ---
#
# With speech_unit "sentence" a message is spoken one sentence at a time: the
# first sentence plays as soon as it is synthesized while the next ones are
# synthesized ahead in a thread. A pause lands on a sentence boundary and resume
# replays the cut sentence from its start, so no WAV is trimmed by the clock.


def speech_unit() -> str:
    """What the daemon synthesizes and plays as one piece: "message" or "sentence"."""
    unit = str(load_raw_config().get("speech_unit", "message")).strip().lower()
    return unit if unit in ("message", "sentence") else "message"


@dataclass
class StreamResult:
    """How a sentence stream ended.

    outcome is "done", "paused", "cancelled" or "failed". index is the sentence
    that was cut off (resume from it), or the one that failed to synthesize.
    parts are the sentence WAVs that exist; the caller keeps or deletes them.
    cut_played says the cut sentence had started playing (as opposed to a pause
    that landed while it was still being synthesized); remaining_s is then the
    WAV seconds left in it, for the near-end check. played_s is cumulative
    listening time across passes, seeded from played_before_s.
    """

    outcome: str
    index: int
    parts: list[Path] = field(default_factory=list)
    cut_played: bool = False
    remaining_s: float = 0.0
    played_s: float = 0.0


def sentence_generator(
    persona: str,
    *,
    voice_kokoro: str = "",
    voice_kokoro_blend: str = "",
    tone: ToneParams | None = None,
    voice_mlx: str = "",
    speaker_mlx: str = "",
    lang_mlx: str = "",
) -> Callable[[str, Path], bool]:
    """Bind a persona and voice overrides into the generate(sentence, path) call."""

    def gen(chunk: str, path: Path) -> bool:
        return daemon_generate_speech(
            chunk,
            persona,
            path,
            voice_kokoro_override=voice_kokoro,
            voice_kokoro_blend_override=voice_kokoro_blend,
            tone=tone,
            voice_mlx_override=voice_mlx,
            speaker_mlx_override=speaker_mlx,
            lang_mlx_override=lang_mlx,
        )

    return gen


def play_sentences(
    sentences: list[str],
    audio_file: Path,
    generate: Callable[[str, Path], bool],
    *,
    speed: float = 1.0,
    speed_method: str = "playback",
    start_index: int = 0,
    job_id: str | None = None,
    lookahead: int = 2,
    played_before_s: float = 0.0,
) -> StreamResult:
    """Speak sentences[start_index:] in order, synthesizing ahead while one plays.

    generate(sentence, path) is the daemon's synthesis call. Parts are written
    next to audio_file as <stem>_<pass>_sN.wav, the pass token keeping a worker
    abandoned by an earlier pass from writing over this one. lookahead bounds how
    many sentences the synthesis thread may run ahead of playback. The stream
    stops at a sentence boundary on pause, bridge cancel or daemon shutdown.
    """
    n = len(sentences)
    if start_index >= n:
        return StreamResult("done", n, played_s=played_before_s)

    token = secrets.token_hex(3)
    part_paths = [audio_file.with_name(f"{audio_file.stem}_{token}_s{i}.wav") for i in range(n)]
    ready = [threading.Event() for _ in range(n)]
    ok = [False] * n
    stop = threading.Event()
    # The worker takes one slot per sentence it synthesizes; playback returns
    # one per sentence spoken, so synthesis never runs more than lookahead ahead.
    slots = threading.Semaphore(max(1, lookahead) + 1)

    def worker() -> None:
        for i in range(start_index, n):
            while not slots.acquire(timeout=0.2):
                if stop.is_set():
                    return
            if stop.is_set():
                return
            try:
                ok[i] = generate(sentences[i], part_paths[i]) and part_paths[i].exists()
            except Exception as e:  # a synthesis crash ends the stream, not the daemon
                log(f"Sentence {i} synthesis raised: {e}", "ERROR")
                ok[i] = False
            if stop.is_set():
                # The stream ended while this ran; nobody will play or delete it.
                part_paths[i].unlink(missing_ok=True)
                return
            ready[i].set()
            if not ok[i]:
                return

    t = threading.Thread(target=worker, name="tts-synth", daemon=True)
    t.start()
    play_speed = speed if speed_method == "playback" else 1.0
    parts: list[Path] = []
    played_s = played_before_s

    def cancelled(i: int) -> StreamResult:
        JOBS.update(job_id, state="cancelled", position_ms=round(played_s * 1000))
        return StreamResult("cancelled", i, parts, played_s=played_s)

    try:
        for i in range(start_index, n):
            while not ready[i].wait(timeout=0.2):
                # A bridge /stop between plays marks the job cancelled without a
                # cancel request (there is no player to kill), so check both.
                if JOBS.take_cancel(job_id) or JOBS.state(job_id) == "cancelled":
                    return cancelled(i)
                if read_playback_state().get("paused") or _shutdown_requested:
                    return StreamResult("paused", i, parts, played_s=played_s)
            if _shutdown_requested:
                return StreamResult("paused", i, parts, played_s=played_s)
            if not ok[i]:
                return StreamResult("failed", i, parts, played_s=played_s)
            parts.append(part_paths[i])
            part_s = get_wav_duration(part_paths[i])
            if i == start_index:
                log(f"Sentence stream: first audio, sentence {i + 1}/{n}")
                JOBS.update(
                    job_id,
                    state="playing",
                    started_at=time.time(),
                    offset_ms=round(played_s * 1000),
                )
            _, was_killed, elapsed = daemon_play_audio(part_paths[i], play_speed, job_id)
            slots.release()
            if JOBS.state(job_id) == "cancelled":
                return StreamResult("cancelled", i, parts, played_s=played_s)
            if was_killed:
                pos = calculate_audio_position(elapsed, speed, speed_method)
                played_s += elapsed
                return StreamResult(
                    "paused",
                    i,
                    parts,
                    cut_played=True,
                    remaining_s=max(0.0, part_s - pos),
                    played_s=played_s,
                )
            played_s += part_s / (play_speed if play_speed > 0 else 1.0)
        return StreamResult("done", n, parts, played_s=played_s)
    finally:
        stop.set()
        # Do not hold the daemon loop for a slow backend; an abandoned worker
        # deletes its own output (see worker) and the pass token keeps it apart.
        t.join(timeout=2.0)
        for path in part_paths:
            if path not in parts:
                path.unlink(missing_ok=True)


def stream_message(
    msg_info: dict,
    generate: Callable[[str, Path], bool],
    *,
    start_index: int = 0,
    msg_file: Path | None = None,
) -> None:
    """Speak one queue message sentence by sentence and settle its state.

    msg_info is the current_message record (text, session_id, speed, ...); on a
    pause it is written back with sentence_index and played_s so the next loop
    pass resumes from the cut sentence. msg_file, the queue entry, is removed
    once the message is settled, so a daemon that dies mid-stream and is not
    respawned quickly still finds it in the queue.
    """
    session_id = msg_info.get("session_id", "unknown")
    project = msg_info.get("project", "unknown")
    speed = float(msg_info.get("speed", 1.0))
    speed_method = msg_info.get("speed_method", "playback")
    job_id = msg_info.get("id") if msg_info.get("source") else None
    sentences = split_sentences(msg_info.get("text", ""))
    n = len(sentences)
    audio_file = Path(f"/tmp/tts_queue_{session_id}.wav")
    played_before = float(msg_info.get("played_s", 0.0))

    msg_info = dict(msg_info)
    msg_info["sentence_index"] = start_index
    write_playback_state(current_message=msg_info)
    JOBS.update(job_id, state="synthesizing", speed=speed)
    if start_index:
        log(f"Resuming at sentence {start_index + 1}/{n}: {sentences[start_index][:50]}...")

    result = play_sentences(
        sentences,
        audio_file,
        generate,
        speed=speed,
        speed_method=speed_method,
        start_index=start_index,
        job_id=job_id,
        played_before_s=played_before,
    )
    position_ms = round(result.played_s * 1000)

    def save_history() -> None:
        """Keep what this pass spoke in speech history as one WAV."""
        spoken = sentences[start_index : start_index + len(result.parts)]
        if result.parts and concat_wavs(result.parts, audio_file):
            save_speech_wav(
                audio_file,
                session_id=session_id,
                project=project,
                persona=msg_info.get("persona", ""),
                text=" ".join(spoken),
                speed=speed,
                tone=msg_info.get("tone", "neutral"),
            )
            audio_file.unlink(missing_ok=True)

    try:
        if result.outcome == "paused":
            last = result.index == n - 1
            if last and result.cut_played and result.remaining_s <= NEAR_END_THRESHOLD:
                log(f"Interrupted near end ({result.remaining_s:.1f}s remaining), skipping replay")
                save_history()
                clear_current_message()
                JOBS.update(job_id, state="done", position_ms=position_ms)
                return
            msg_info["sentence_index"] = result.index
            msg_info["played_s"] = result.played_s
            write_playback_state(current_message=msg_info)
            JOBS.update(job_id, state="paused", position_ms=position_ms)
            why = "on unpause" if not _shutdown_requested else "after restart"
            log(
                f"Message interrupted at sentence {result.index + 1}/{n}, "
                f"will resume from its start {why}"
            )
            return
        if result.outcome == "cancelled":
            log(f"Cancelled by bridge: {project}")
            clear_current_message()
            return
        if result.outcome == "failed":
            log(
                f"Failed to generate sentence {result.index + 1}/{n} for {project}: "
                f"{audio_last_error()}",
                "ERROR",
            )
            save_history()
            clear_current_message()
            if not result.parts and start_index == 0:
                JOBS.update(job_id, state="failed", error=audio_last_error())
            else:
                # Degradation over failure: what was spoken counts as the message.
                JOBS.update(job_id, state="done", position_ms=position_ms)
            return
        save_history()
        clear_current_message()
        JOBS.update(job_id, state="done", position_ms=position_ms, duration_ms=position_ms)
    finally:
        for part in result.parts:
            part.unlink(missing_ok=True)
        if msg_file is not None:
            msg_file.unlink(missing_ok=True)


def speaker_transition(
    transition: str,
    last_speaker: str,
    speaker_key: str,
    project: str,
    persona: str,
    speed: float,
    speed_method: str,
) -> None:
    """Mark a change of speaking session with a chime or a spoken name."""
    if transition == "chime":
        log(f"Speaker change: {last_speaker} -> {speaker_key}")
        play_chime()
    elif transition == "announce":
        log(f"Announcing speaker: {project}")
        announce_file = Path("/tmp/tts_announce.wav")
        if daemon_generate_speech(f"{project} says:", persona, announce_file):
            if speed_method == "playback":
                daemon_play_audio(announce_file, speed)
            else:
                daemon_play_audio(announce_file)
            announce_file.unlink(missing_ok=True)
        time.sleep(0.3)


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
                        check=True,
                        capture_output=True,
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


def _supervised() -> bool:
    """True when launchd or systemd started us and will restart us on exit 3.

    launchd sets XPC_SERVICE_NAME to the job label for agents it runs (a plain
    Terminal shell has it unset or "0"); systemd sets INVOCATION_ID.
    """
    xpc = os.environ.get("XPC_SERVICE_NAME", "")
    return (xpc not in ("", "0")) or bool(os.environ.get("INVOCATION_ID"))


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
        write_protocol_marker()
        msg_file = msg.get("_file")
        if msg_file:
            Path(msg_file).unlink(missing_ok=True)
        write_respawn_marker()
        clear_heartbeat()
        release_lock()
        if _supervised():
            log("Control: exiting for the service manager to restart us")
            sys.exit(3)
        # Nobody will respawn us, so become a fresh daemon in place. execv keeps the
        # PID (so the pid file stays right) and runs no atexit handlers. Found
        # 2026-09-22 when a control restart quietly killed a fork-started daemon.
        log("Control: no service manager, re-executing in place")
        os.execv(
            sys.executable,
            [sys.executable, "-m", "claude_code_tts.cli", "daemon", "foreground", "--lockpick"],
        )
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
    messages: list[dict] = []
    if not TTS_QUEUE_DIR.exists():
        return messages

    for f in TTS_QUEUE_DIR.glob("*.json"):
        try:
            with open(f) as fp:
                msg = json.load(fp)
                msg["_file"] = f
                messages.append(msg)
        except (OSError, json.JSONDecodeError) as e:
            log(f"Failed to read queue file {f}: {e}", "WARN")
            f.unlink(missing_ok=True)

    messages.sort(key=lambda m: m.get("timestamp", 0))
    return messages


BACKGROUND_LANE = "background"
DEFAULT_NORMALIZE_DBFS = -16.0


def register_queued_bridge_jobs() -> int:
    """Put every queued bridge message back in the job registry after a restart.

    The registry lives in memory and the queue on disk, so a graceful restart
    keeps the blocks a page queued but forgot their ids: every poll answered
    404 while the blocks still played. Recreating the entries as "queued" from
    the files lets GET /jobs?source= and /jobs/<id> find them again. Returns the
    count.
    """
    count = 0
    for msg in get_queue_messages():
        if msg.get("source") and msg.get("id") and JOBS.get(str(msg["id"])) is None:
            JOBS.create(
                str(msg["id"]),
                source=msg["source"],
                project=msg.get("project", msg["source"]),
                persona=msg.get("persona", ""),
                lane=msg.get("lane", ""),
            )
            count += 1
    return count


def play_order(messages: list[dict]) -> list[dict]:
    """The order the loop speaks in: everything else first, then the background lane.

    A long read queued by a page marks its blocks lane "background" so a
    room's one-liner arriving mid-read goes next, at the block boundary, instead
    of behind every queued block. Within a lane the timestamp order holds, and
    control messages are never background, so they still come first. Ageing and
    depth trimming keep using timestamp order; this is only who speaks next.
    """
    return sorted(messages, key=lambda m: 1 if m.get("lane") == BACKGROUND_LANE else 0)


class PauseLedger:
    """Seconds the daemon has spent paused, so a held queue does not age.

    A pause means "hold everything", so time spent paused is subtracted from a
    message's age before the max_age check, and a message that waited through a
    pause is exempt from depth trimming until it plays. The ledger lives in
    memory: a daemon restart forgets it and wall-clock age applies again.
    """

    def __init__(self) -> None:
        self._closed: list[tuple[float, float]] = []
        self._open: float | None = None

    def mark(self, paused: bool, now: float | None = None) -> None:
        """Record the pause flag as seen on this pass of the loop."""
        t = time.time() if now is None else now
        if paused and self._open is None:
            self._open = t
        elif not paused and self._open is not None:
            self._closed.append((self._open, t))
            self._open = None

    @property
    def paused(self) -> bool:
        return self._open is not None

    def held_since(self, since: float, now: float | None = None) -> float:
        """Paused seconds between since and now."""
        t = time.time() if now is None else now
        intervals = list(self._closed)
        if self._open is not None:
            intervals.append((self._open, t))
        held = 0.0
        for start, end in intervals:
            held += max(0.0, min(end, t) - max(start, since))
        return held


def cleanup_old_messages(max_age_seconds: int, ledger: PauseLedger | None = None) -> int:
    """Remove messages older than max_age, not counting paused time. Returns count removed."""
    removed = 0
    now = time.time()

    for f in TTS_QUEUE_DIR.glob("*.json"):
        try:
            with open(f) as fp:
                msg = json.load(fp)
            ts = float(msg.get("timestamp", 0))
            held = ledger.held_since(ts, now) if ledger else 0.0
            if now - ts - held > max_age_seconds:
                f.unlink()
                removed += 1
                log(f"Removed stale message: {f.name}")
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            f.unlink(missing_ok=True)
            removed += 1

    return removed


def enforce_max_depth(max_depth: int, ledger: PauseLedger | None = None) -> int:
    """Remove oldest messages if queue exceeds max depth.

    Messages that waited through a pause are held, not trimmed: they neither
    count toward the depth nor get removed.
    """
    now = time.time()
    messages = [
        m
        for m in get_queue_messages()
        if not ledger or ledger.held_since(float(m.get("timestamp", 0) or 0), now) <= 0
    ]
    removed = 0

    while len(messages) > max_depth:
        oldest = messages.pop(0)
        oldest["_file"].unlink(missing_ok=True)
        removed += 1
        log(f"Queue overflow, removed: {oldest.get('project', 'unknown')}")

    return removed


# --- Main Daemon Loop ---


@dataclass
class PreparedMessage:
    """One queue message with every field the loop needs, resolved once.

    Built by prepare_message() for the message about to be spoken and for
    the one after it (see Prefetch), so both are spoken with the same rules.
    """

    msg: dict
    msg_file: Path
    session_id: str
    project: str
    text: str
    persona: str
    persona_config: dict
    job_id: str | None
    want_marks: bool
    tone: ToneParams
    speed: float
    speed_method: str
    effective_speed: float
    effective_speed_method: str
    voice_kokoro: str
    voice_kokoro_blend: str
    voice_mlx: str
    speaker_mlx: str
    lang_mlx: str
    voice_label: str
    speaker_key: str
    audio_file: Path
    current_msg_info: dict


def prepare_message(msg: dict, raw_config: dict) -> PreparedMessage:
    """Resolve a queue message against its persona and the daemon config."""
    session_id = msg.get("session_id", "unknown")
    project = msg.get("project", "unknown")
    text = msg.get("text", "")
    persona = msg.get("persona", "claude-prime")
    # Bridge messages carry a source; their id is a job the page polls.
    job_id = msg.get("id") if msg.get("source") else None
    want_marks = bool(msg.get("want_marks"))

    # Classify content tone for expressive speech.
    # Tone modulation is opt-in via config until parameter ranges
    # are validated with each voice model. noise_scale/noise_w_scale
    # values that work for one model can produce static on another.
    tone_enabled = raw_config.get("tone_modulation", False)
    tone = classify_tone(text) if tone_enabled else DEFAULT_TONE
    if tone_enabled and tone.name != "neutral":
        log(f"Tone: {tone.name} (noise={tone.noise_scale}, silence={tone.sentence_silence})")

    persona_config = get_persona_config(persona)
    speed = msg.get("speed", persona_config.get("speed", 2.0))
    speed_method = msg.get("speed_method", persona_config.get("speed_method", "playback"))
    voice_kokoro = msg.get("voice_kokoro", "")
    voice_kokoro_blend = msg.get("voice_kokoro_blend", "")
    # Hook messages carry the persona's mlx fields too, and the persona
    # is the authority for those; only a message that chose mlx for
    # itself ("engine": "mlx", from `speak --voice-mlx`) overrides.
    voice_mlx = msg.get("voice_mlx", "") if msg.get("engine") == "mlx" else ""
    speaker_mlx = msg.get("speaker_mlx", "") if voice_mlx else ""
    lang_mlx = msg.get("lang_mlx", "") if voice_mlx else ""
    voice_label = describe_voice(
        persona, persona_config, voice_kokoro, voice_kokoro_blend, voice_mlx, speaker_mlx
    )

    # Sherpa applies speed during synthesis: don't also apply at playback.
    sherpa_plays = bool(persona_config.get("voice_sherpa")) and not (
        voice_kokoro
        or voice_kokoro_blend
        or voice_mlx
        or persona_config.get("voice_kokoro")
        or persona_config.get("voice_kokoro_blend")
        or persona_config.get("voice_mlx")
    )
    effective_speed_method = "length_scale" if sherpa_plays else speed_method
    effective_speed = speed * tone.speed_factor

    # One WAV per message, named by the message: the next message is
    # synthesized while this one plays, and two in a row from one session
    # must not share a file.
    tag = "".join(ch for ch in str(msg.get("id") or "") if ch.isalnum())[:12] or secrets.token_hex(
        4
    )
    audio_file = Path(f"/tmp/tts_queue_{session_id}_{tag}.wav")

    current_msg_info = {
        "session_id": session_id,
        "project": project,
        "text": text,
        "persona": persona,
        "speed": effective_speed,
        "speed_method": effective_speed_method,
        "voice_kokoro": voice_kokoro,
        "voice_kokoro_blend": voice_kokoro_blend,
        "voice_mlx": voice_mlx,
        "speaker_mlx": speaker_mlx,
        "lang_mlx": lang_mlx,
        "pitch_filter": msg.get("pitch_filter", ""),
    }
    if job_id:
        current_msg_info["id"] = job_id
        current_msg_info["source"] = msg.get("source")
        current_msg_info["want_marks"] = want_marks

    return PreparedMessage(
        msg=msg,
        msg_file=msg["_file"],
        session_id=session_id,
        project=project,
        text=text,
        persona=persona,
        persona_config=persona_config,
        job_id=job_id,
        want_marks=want_marks,
        tone=tone,
        speed=speed,
        speed_method=speed_method,
        effective_speed=effective_speed,
        effective_speed_method=effective_speed_method,
        voice_kokoro=voice_kokoro,
        voice_kokoro_blend=voice_kokoro_blend,
        voice_mlx=voice_mlx,
        speaker_mlx=speaker_mlx,
        lang_mlx=lang_mlx,
        voice_label=voice_label,
        speaker_key=f"{session_id}:{project}",
        audio_file=audio_file,
        current_msg_info=current_msg_info,
    )


def synthesize_prepared(p: PreparedMessage) -> tuple[bool, dict | None]:
    """The one-piece synthesis of a prepared message into its audio_file."""
    return synthesize_message(
        p.text,
        p.persona,
        p.audio_file,
        want_marks=p.want_marks,
        speed=p.effective_speed,
        speed_method=p.effective_speed_method,
        voice_kokoro_override=p.voice_kokoro,
        voice_kokoro_blend_override=p.voice_kokoro_blend,
        tone=p.tone,
        voice_mlx_override=p.voice_mlx,
        speaker_mlx_override=p.speaker_mlx,
        lang_mlx_override=p.lang_mlx,
    )


def next_speakable(messages: list[dict], current: Path) -> dict | None:
    """The message the loop will pick after `current`, if it is one worth synthesizing ahead.

    None when the queue is empty past `current`, or when a control message
    comes first: the loop handles control before anything else.
    """
    for m in messages:
        if m.get("_file") == current:
            continue
        if m.get("type") == "control":
            return None
        if str(m.get("text", "")).strip():
            return m
    return None


class Prefetch:
    """Synthesizes the next queue message while the current one plays.

    One slot. start() runs synthesize_prepared in a thread; take(msg_file)
    waits for it and hands the result back when it is for that very file,
    otherwise discards it (the message was flushed, trimmed or expired
    meanwhile) and deletes its WAV. wait() is for anything about to
    synthesize on the loop's thread: the model workers serialise callers and
    a caller kept waiting falls through to Piper, so the loop never
    synthesizes while a prefetch is in flight.
    """

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._prepared: PreparedMessage | None = None
        self._result: tuple[bool, dict | None] = (False, None)

    @property
    def pending(self) -> Path | None:
        return self._prepared.msg_file if self._prepared is not None else None

    def start(self, prepared: PreparedMessage) -> None:
        self.discard()
        if not prepared.msg_file.exists():
            return  # flushed between the queue read and now
        # Compare-and-set: a /stop that cancelled the job meanwhile must win.
        if prepared.job_id and not JOBS.advance(prepared.job_id, when="queued", to="synthesizing"):
            return
        self._prepared = prepared
        self._result = (False, None)

        def run() -> None:
            try:
                self._result = synthesize_prepared(prepared)
            except Exception as e:  # noqa: BLE001  a prefetch must never take the loop down
                log(f"Prefetch of the next message ({prepared.project}) failed: {e}", "WARN")
                audio_set_last_error(f"prefetch failed: {e}")
                self._result = (False, None)

        self._thread = threading.Thread(target=run, name="tts-prefetch", daemon=True)
        self._thread.start()

    def wait(self) -> None:
        if self._thread is not None:
            self._thread.join()
            self._thread = None

    def discard(self) -> None:
        """Forget the slot: wait for its synthesis, delete its WAV, put a live job back to queued."""
        self.wait()
        prepared, self._prepared = self._prepared, None
        if prepared is None:
            return
        prepared.audio_file.unlink(missing_ok=True)
        if prepared.job_id:
            if prepared.msg_file.exists():
                JOBS.advance(
                    prepared.job_id, when="synthesizing", to="queued"
                )  # it will be picked later
            else:
                JOBS.advance(prepared.job_id, when="synthesizing", to="cancelled")

    def discard_if_gone(self) -> None:
        """Drop a prefetch whose queue file no longer exists (flushed, trimmed, expired).

        take() would discard it too, but only when another message is picked;
        with an empty queue the WAV would otherwise sit until one arrives.
        """
        if self._prepared is not None and not self._prepared.msg_file.exists():
            self.discard()

    def take(self, msg_file: Path) -> tuple[PreparedMessage, bool, dict | None] | None:
        """The prefetched synthesis of msg_file, or None; anything else is discarded."""
        self.wait()
        if self._prepared is None:
            return None
        if self._prepared.msg_file != msg_file:
            self.discard()
            return None
        prepared, result = self._prepared, self._result
        self._prepared = None
        return prepared, result[0], result[1]


def daemon_loop(lockpick: bool = False) -> None:
    """Main daemon processing loop."""
    global _shutdown_requested

    if not acquire_lock(lockpick=lockpick, log=log):
        log("Another daemon is already running. Exiting.", "ERROR")
        print("Another daemon is already running. Use --lockpick to force takeover.")
        sys.exit(1)

    _shutdown_by_signal = False
    # WAVs a previous daemon left behind (killed mid-message, or mid-prefetch).
    for leftover in Path("/tmp").glob("tts_queue_*.wav"):
        leftover.unlink(missing_ok=True)

    def handle_shutdown(signum: int, _frame: object) -> None:
        global _shutdown_requested
        nonlocal _shutdown_by_signal
        _shutdown_requested = True
        _shutdown_by_signal = True
        log(f"Shutdown requested (signal {signum}), finishing current work...")

    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    log("Daemon starting...")
    write_pid()
    TTS_QUEUE_DIR.mkdir(parents=True, exist_ok=True)

    config = get_queue_config()
    poll_interval = config["idle_poll_ms"] / 1000.0
    last_speaker: str | None = None

    log(
        f"Queue config: max_depth={config['max_depth']}, "
        f"max_age={config['max_age_seconds']}s, "
        f"transition={config['speaker_transition']}"
    )

    write_protocol_marker()
    write_release_marker()

    # Loopback HTTP bridge for browser pages (off unless http.enabled in config).
    bridge: Bridge | None = None
    http_config = get_http_config()
    if http_config.get("enabled"):
        bridge = Bridge(
            log_fn=log,
            read_playback_state=read_playback_state,
            clear_current_message=clear_current_message,
            set_paused=set_paused,
        )
        if not bridge.start(http_config):
            bridge = None
    else:
        log("HTTP bridge disabled (claude-tts bridge enable to turn it on)")

    # --- Detect restart type ---
    # Respawn marker with recent timestamp = controlled restart (upgrade/config).
    # Missing or old marker = cold start (reboot, crash, manual start).
    is_respawn = take_respawn_marker()

    # --- Clear stale state from previous daemon run ---
    # audio_pid and mic-pause are always stale (process is dead, mic isn't
    # recording across restarts). current_message is only preserved on
    # controlled respawn — on cold start it's from a previous session.
    startup_state = read_playback_state()
    reregistered = register_queued_bridge_jobs()
    if reregistered:
        log(f"Re-registered {reregistered} queued bridge job(s) left by the previous daemon")
    stale_fields: list[str] = []
    if startup_state.get("audio_pid") is not None:
        if kill_orphan_player(startup_state.get("audio_pid")):
            log(
                f"Killed the previous daemon's player (PID {startup_state['audio_pid']}) still speaking"
            )
        stale_fields.append("audio_pid")
    if startup_state.get("paused") and startup_state.get("paused_by") == "mic":
        stale_fields.append("mic-pause")
    # Shutdown lets a message finish (graceful above all: never lose the place,
    # never two voices), so a current_message left behind is either a sentence
    # stream stopped at a boundary (sentence_index), a pause (audio_position),
    # or a crash. The first two resume if recent. One with neither never
    # started to play; its queue file is still there and will speak it, so
    # keeping it would speak it twice.
    interrupted_recently = (
        isinstance(startup_state.get("current_message"), dict)
        and (
            startup_state["current_message"].get("audio_position") is not None
            or startup_state["current_message"].get("sentence_index") is not None
        )
        and time.time() - float(startup_state.get("updated_at") or 0) <= RESUME_AFTER_RESTART_S
    )
    if (
        startup_state.get("current_message") is not None
        and not is_respawn
        and not interrupted_recently
    ):
        stale_fields.append("current_message")
    elif interrupted_recently and not is_respawn:
        log("Resuming the message the previous daemon stopped in")
    if stale_fields:
        write_playback_state(
            audio_pid=None,
            current_message=None if "current_message" in stale_fields else UNSET,
            paused=False if "mic-pause" in stale_fields else None,
            paused_by=None if "mic-pause" in stale_fields else UNSET,
        )
        log(f"Cleared stale state from previous run: {', '.join(stale_fields)}")

    write_heartbeat()

    # Start mic-aware pause watcher BEFORE the startup announcement.
    # If Handy is actively recording, the watcher will pause us before
    # we start talking over the user.
    mic_watcher: MicWatcher | None = None
    raw_config = load_raw_config()
    if raw_config.get("mic_aware_pause", False):
        resume_delay = raw_config.get("mic_resume_delay_ms", 800)
        mic_watcher = MicWatcher(
            log_fn=log,
            read_playback_state=read_playback_state,
            write_playback_state=write_playback_state,
            resume_delay_ms=resume_delay,
        )
        if not mic_watcher.start():
            log("Mic watcher failed to start (Handy log not found)", "WARN")
            mic_watcher = None
    else:
        log("Mic-aware pause disabled (set mic_aware_pause: true in config.json to enable)")

    # Start Handy voice analyzer if recordings directory exists
    handy_analyzer: AnalyzerThread | None = None
    if raw_config.get("handy_analyzer", True):
        handy_analyzer = AnalyzerThread(log_fn=log)
        if not handy_analyzer.start():
            handy_analyzer = None
    else:
        log("Handy analyzer disabled (set handy_analyzer: true in config.json to enable)")

    if is_respawn:
        log("Quick respawn detected, skipping startup announcement")
    else:
        speak_announcement("Voice daemon online. Ready when you are.")
        log("Startup announcement complete")

    # Pre-warm sherpa workers after startup announcement so the first real
    # speech request isn't blocked on model load. Runs synchronously here;
    # the daemon is idle at this point so blocking is fine.
    personas = raw_config.get("personas", {})
    if any(p.get("voice_sherpa") for p in personas.values()):
        log("Pre-warming sherpa worker(s)...")
        warm_sherpa_workers(personas)
        log("Sherpa worker(s) ready")
    if any(p.get("voice_mlx") for p in personas.values()):
        # A model load takes seconds from the cache and minutes on a first
        # download; playback must not wait for it, so warm in the background.
        def _warm_mlx() -> None:
            ready = warm_mlx_workers(personas)
            log(f"mlx worker(s) ready: {', '.join(ready) if ready else 'none (see debug.log)'}")

        threading.Thread(target=_warm_mlx, name="mlx-warm", daemon=True).start()

    ledger = PauseLedger()
    if startup_state.get("paused") and startup_state.get("paused_by") != "mic":
        # A restart while paused (an upgrade mid-meeting) keeps the hold from the
        # moment of the pause, which is the last write to the state file.
        try:
            ledger.mark(True, now=float(startup_state.get("updated_at") or time.time()))
        except (TypeError, ValueError):
            ledger.mark(True)
        log("Started paused; holding the queue since the pause")
    last_reap = time.monotonic()
    prefetch = Prefetch()
    while not _shutdown_requested:
        try:
            write_heartbeat()
            if time.monotonic() - last_reap >= WORKER_REAP_EVERY_S:
                last_reap = time.monotonic()
                idle_s = float(config.get("worker_idle_unload_s", 1800))
                for name in reap_idle_workers(idle_s):
                    log(
                        f"Unloaded {name} worker: unused for {idle_s / 60:.0f} min; it reloads on the next message"
                    )
            state = read_playback_state()
            was_paused = ledger.paused
            ledger.mark(bool(state.get("paused")))
            if state.get("paused"):
                # Paused holds the queue: nothing expires, nothing is trimmed.
                if not was_paused:
                    log(f"Paused by {state.get('paused_by') or 'user'}; holding the queue")
                time.sleep(poll_interval)
                continue
            if was_paused:
                log(f"Resumed with {len(get_queue_messages())} message(s) waiting")
            cleanup_old_messages(config["max_age_seconds"], ledger)
            enforce_max_depth(config["max_depth"], ledger)
            JOBS.evict_finished()

            # Check for interrupted message to replay first
            interrupted = get_interrupted_message()
            if interrupted:
                prefetch.wait()  # never synthesize on this thread while a prefetch runs
                session_id = interrupted.get("session_id", "unknown")
                persona = interrupted.get("persona", "claude-prime")
                persona_config = get_persona_config(persona)
                i_speed = interrupted.get("speed", persona_config.get("speed", 2.0))
                i_speed_method = interrupted.get(
                    "speed_method", persona_config.get("speed_method", "playback")
                )
                i_voice_kokoro = interrupted.get("voice_kokoro", "")
                i_voice_blend = interrupted.get("voice_kokoro_blend", "")
                i_voice_mlx = interrupted.get("voice_mlx", "")
                i_speaker_mlx = interrupted.get("speaker_mlx", "")
                i_lang_mlx = interrupted.get("lang_mlx", "")
                prev_audio_pos = interrupted.get("audio_position", 0.0)
                i_job_id = interrupted.get("id") if interrupted.get("source") else None

                if "sentence_index" in interrupted:
                    i_tone = (
                        classify_tone(interrupted.get("text", ""))
                        if raw_config.get("tone_modulation", False)
                        else DEFAULT_TONE
                    )

                    stream_message(
                        interrupted,
                        sentence_generator(
                            persona,
                            voice_kokoro=i_voice_kokoro,
                            voice_kokoro_blend=i_voice_blend,
                            tone=i_tone,
                            voice_mlx=i_voice_mlx,
                            speaker_mlx=i_speaker_mlx,
                            lang_mlx=i_lang_mlx,
                        ),
                        start_index=int(interrupted.get("sentence_index", 0)),
                    )
                    continue

                # Regenerate the full WAV, sentence by sentence if the page
                # wants marks, so its offsets line up with the first pass.
                audio_file = Path(f"/tmp/tts_queue_{session_id}.wav")
                i_ok, _ = synthesize_message(
                    interrupted.get("text", ""),
                    persona,
                    audio_file,
                    want_marks=bool(interrupted.get("want_marks")),
                    speed=i_speed,
                    speed_method=i_speed_method,
                    voice_kokoro_override=i_voice_kokoro,
                    voice_kokoro_blend_override=i_voice_blend,
                    voice_mlx_override=i_voice_mlx,
                    speaker_mlx_override=i_speaker_mlx,
                    lang_mlx_override=i_lang_mlx,
                )
                if i_ok:
                    wav_duration = get_wav_duration(audio_file)
                    remaining = wav_duration - prev_audio_pos

                    # Near the end? Skip replay entirely (ghost interruption)
                    if prev_audio_pos > 0 and remaining <= NEAR_END_THRESHOLD:
                        log(
                            f"Skipping replay: {remaining:.1f}s remaining "
                            f"(threshold {NEAR_END_THRESHOLD}s), "
                            f"position {prev_audio_pos:.1f}s / {wav_duration:.1f}s"
                        )
                        clear_current_message()
                        audio_file.unlink(missing_ok=True)
                        continue

                    # Resume with rewind (scaled to playback speed)
                    play_file = audio_file
                    rw = rewind_amount(i_speed, i_speed_method)
                    resume_from = max(0.0, prev_audio_pos - rw)
                    if resume_from > 0:
                        trimmed = audio_file.with_name(f"tts_queue_{session_id}_trim.wav")
                        if trim_wav(audio_file, trimmed, resume_from):
                            log(
                                f"Resuming from {resume_from:.1f}s "
                                f"(was at {prev_audio_pos:.1f}s, "
                                f"rewound {rw:.1f}s wav-time = "
                                f"{resume_rewind_seconds()}s real)"
                            )
                            play_file = trimmed
                        else:
                            log("Trim failed, replaying from start")
                            resume_from = 0.0
                    else:
                        log(
                            f"Replaying interrupted message from start: "
                            f"{interrupted.get('text', '')[:50]}..."
                        )

                    write_playback_state(current_message=interrupted)
                    JOBS.update(
                        i_job_id,
                        state="playing",
                        started_at=time.time(),
                        offset_ms=to_playback_ms(resume_from, i_speed, i_speed_method),
                    )
                    if i_speed_method == "playback":
                        _, was_killed, elapsed = daemon_play_audio(play_file, i_speed, i_job_id)
                    else:
                        _, was_killed, elapsed = daemon_play_audio(play_file, job_id=i_job_id)

                    audio_file.unlink(missing_ok=True)
                    if play_file != audio_file:
                        play_file.unlink(missing_ok=True)

                    if JOBS.state(i_job_id) == "cancelled":
                        clear_current_message()
                    elif was_killed:
                        # Accumulate position: where we resumed + how far we got
                        new_pos = resume_from + calculate_audio_position(
                            elapsed, i_speed, i_speed_method
                        )
                        interrupted["audio_position"] = new_pos
                        write_playback_state(current_message=interrupted)
                        JOBS.update(
                            i_job_id,
                            state="paused",
                            position_ms=to_playback_ms(new_pos, i_speed, i_speed_method),
                        )
                        log(f"Interrupted again at audio position {new_pos:.1f}s")
                        continue
                    else:
                        clear_current_message()
                        JOBS.update(i_job_id, state="done")
                else:
                    clear_current_message()
                    JOBS.update(i_job_id, state="failed")
                continue

            # Get pending messages
            prefetch.discard_if_gone()
            messages = play_order(get_queue_messages())
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

            if not str(msg.get("text", "")).strip():
                log(f"Empty message from {msg.get('project', 'unknown')}, skipping")
                msg_file.unlink(missing_ok=True)
                JOBS.update(
                    msg.get("id") if msg.get("source") else None, state="failed", error="empty text"
                )
                continue

            # The message after the one that just played may be synthesized already.
            taken = prefetch.take(msg_file)
            if taken is not None:
                p, ok, marks = taken
                prefetched = True
            else:
                p = prepare_message(msg, raw_config)
                ok, marks, prefetched = False, None, False
            session_id, project, text, persona = p.session_id, p.project, p.text, p.persona
            persona_config, job_id, want_marks, tone = (
                p.persona_config,
                p.job_id,
                p.want_marks,
                p.tone,
            )
            audio_file, speed, speed_method = p.audio_file, p.speed, p.speed_method
            effective_speed, effective_speed_method = p.effective_speed, p.effective_speed_method
            voice_kokoro, voice_kokoro_blend = p.voice_kokoro, p.voice_kokoro_blend
            voice_mlx, speaker_mlx, lang_mlx = p.voice_mlx, p.speaker_mlx, p.lang_mlx
            current_msg_info, speaker_key = p.current_msg_info, p.speaker_key
            # The line `daemon stats` counts messages by; the bracket says which
            # voice is about to play, so the log can answer that without an ear.
            log(f"Speaking for {project} [{persona}, {p.voice_label}]: {text[:50]}...")

            # Sentence streaming: first audio after the first sentence, pause on a
            # sentence boundary. Pages asking for marks keep the one-piece path,
            # since marks need the whole WAV before playback starts.
            if speech_unit() == "sentence" and not want_marks:
                if last_speaker and last_speaker != speaker_key:
                    speaker_transition(
                        config["speaker_transition"],
                        last_speaker,
                        speaker_key,
                        project,
                        persona,
                        speed,
                        speed_method,
                    )
                last_speaker = speaker_key
                current_msg_info["tone"] = tone.name
                if prefetched:
                    audio_file.unlink(missing_ok=True)  # speech_unit changed under us; stream anew

                stream_message(
                    current_msg_info,
                    sentence_generator(
                        persona,
                        voice_kokoro=voice_kokoro,
                        voice_kokoro_blend=voice_kokoro_blend,
                        tone=tone,
                        voice_mlx=voice_mlx,
                        speaker_mlx=speaker_mlx,
                        lang_mlx=lang_mlx,
                    ),
                    msg_file=msg_file,
                )
                continue

            if prefetched and not ok:
                log(
                    f"Prefetch of {project} failed ({audio_last_error()}); synthesizing it now",
                    "WARN",
                )
                prefetched = False
            if not prefetched:
                JOBS.update(job_id, state="synthesizing")
                ok, marks = synthesize_prepared(p)
            if not ok:
                log(
                    f"Failed to generate speech for message from {project}: {audio_last_error()}",
                    "ERROR",
                )
                audio_file.unlink(missing_ok=True)
                msg_file.unlink(missing_ok=True)
                JOBS.update(job_id, state="failed", error=audio_last_error())
                continue
            if marks is not None:
                JOBS.update(job_id, marks=marks)

            # Speaker transition
            if last_speaker and last_speaker != speaker_key:
                speaker_transition(
                    config["speaker_transition"],
                    last_speaker,
                    speaker_key,
                    project,
                    persona,
                    speed,
                    speed_method,
                )
            last_speaker = speaker_key

            # Start on the message after this one now, so it is ready the moment this
            # one ends instead of costing its synthesis time at the boundary. After the
            # speaker transition: an announce is synthesized on this thread and must
            # not race the prefetch for a model worker.
            if config.get("prefetch_next", True):
                nxt = next_speakable(play_order(get_queue_messages()), msg_file)
                if nxt is not None and (speech_unit() != "sentence" or nxt.get("want_marks")):
                    prefetch.start(prepare_message(nxt, raw_config))

            write_playback_state(current_message=current_msg_info)

            # Save WAV to speech history before playback
            save_speech_wav(
                audio_file,
                session_id=session_id,
                project=project,
                persona=persona,
                text=text,
                speed=effective_speed,
                tone=tone.name,
            )

            wav_duration = get_wav_duration(audio_file)

            JOBS.update(
                job_id,
                state="playing",
                started_at=time.time(),
                offset_ms=0,
                speed=effective_speed,
                duration_ms=to_playback_ms(wav_duration, effective_speed, effective_speed_method),
            )
            if effective_speed_method == "playback":
                _, was_killed, elapsed = daemon_play_audio(audio_file, effective_speed, job_id)
            else:
                _, was_killed, elapsed = daemon_play_audio(audio_file, job_id=job_id)

            audio_file.unlink(missing_ok=True)

            if JOBS.state(job_id) == "cancelled":
                log(f"Cancelled by bridge: {project}")
                clear_current_message()
                msg_file.unlink(missing_ok=True)
            elif was_killed:
                audio_pos = calculate_audio_position(elapsed, effective_speed, effective_speed_method)
                remaining = wav_duration - audio_pos
                if remaining <= NEAR_END_THRESHOLD:
                    log(f"Interrupted near end ({remaining:.1f}s remaining), skipping replay")
                    clear_current_message()
                    msg_file.unlink(missing_ok=True)
                    JOBS.update(job_id, state="done")
                    continue
                current_msg_info["audio_position"] = audio_pos
                write_playback_state(current_message=current_msg_info)
                JOBS.update(
                    job_id,
                    state="paused",
                    position_ms=to_playback_ms(audio_pos, effective_speed, effective_speed_method),
                )
                log(
                    f"Message interrupted at {audio_pos:.1f}s / {wav_duration:.1f}s, "
                    f"will resume on unpause"
                )
                msg_file.unlink(missing_ok=True)
                continue
            else:
                clear_current_message()
                msg_file.unlink(missing_ok=True)
                JOBS.update(job_id, state="done")

        except KeyboardInterrupt:
            log("Received interrupt, shutting down...")
            break
        except Exception as e:
            log(f"Error in daemon loop: {e}", "ERROR")
            time.sleep(1)

    prefetch.discard()  # a synthesis for a message nobody will play now; its WAV goes too
    if mic_watcher:
        mic_watcher.stop()
    if handy_analyzer:
        handy_analyzer.stop()
    if bridge:
        bridge.stop()
    log("Shutting down gracefully...")
    if not _shutdown_by_signal:
        speak_announcement("Voice daemon shutting down. Catch you later.")
    clear_heartbeat()
    release_lock()
    log("Daemon stopped")


# --- Daemon Management ---


def service_path_env(claude_tts_bin: str) -> str:
    """PATH for the service: tool bins first, then the usual system dirs."""
    dirs = [str(Path(claude_tts_bin).parent)]
    piper = shutil.which("piper")
    if piper:
        dirs.append(str(Path(piper).parent))
    dirs += ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]
    return ":".join(dict.fromkeys(dirs))


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

    sys.stdin = open(os.devnull)
    sys.stdout = open(os.devnull, "w")
    sys.stderr = open(os.devnull, "w")

    TTS_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    write_pid()

    signal.signal(signal.SIGHUP, signal.SIG_IGN)

    import atexit

    atexit.register(clear_pid)

    global _daemon_mode
    _daemon_mode = True

    daemon_loop(lockpick=lockpick)
    return True


def stop_daemon() -> bool:
    """Stop the daemon gracefully. Returns True on success.

    Graceful above all (JMO, 2026-09-29): a daemon that is speaking finishes
    the message, however long that takes, so nobody loses their place and no
    two voices overlap. The wait is bounded only while the daemon is idle:
    STOP_IDLE_GRACE_S of not playing and still not exiting means it is stuck,
    and then it is killed along with any player it left behind.
    """
    running, pid = is_daemon_running()
    if not running:
        print("Daemon is not running")
        return False

    assert pid is not None

    try:
        os.kill(pid, signal.SIGTERM)
        idle_since: float | None = None
        started = time.monotonic()
        last_note = started
        while True:
            time.sleep(0.1)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                clear_pid()
                print("Daemon stopped gracefully")
                return True
            now = time.monotonic()
            player_pid = read_playback_state().get("audio_pid")
            speaking = isinstance(player_pid, int) and player_pid > 0 and pid_alive(player_pid)
            if speaking:
                idle_since = None
                if now - last_note >= 3:
                    print(f"  Waiting for the current message to finish... ({now - started:.0f}s)")
                    last_note = now
                continue
            idle_since = idle_since if idle_since is not None else now
            if now - last_note >= 3:
                print(f"  Waiting for daemon to exit... ({now - started:.0f}s)")
                last_note = now
            if now - idle_since >= STOP_IDLE_GRACE_S:
                break

        print("Daemon is not speaking and did not exit, forcing...")
        player_pid = read_playback_state().get("audio_pid")
        os.kill(pid, signal.SIGKILL)
        if kill_orphan_player(player_pid):
            print(f"Stopped its player too (PID {player_pid})")
        clear_pid()
        clear_heartbeat()
        print("Daemon killed")
        return True
    except ProcessLookupError:
        clear_pid()
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


def _log_parts(line: str) -> tuple[str, str] | None:
    """(timestamp, message) from a daemon log line, or None for a line without the stamp."""
    if len(line) < 22 or line[0] != "[" or line[20] != "]":
        return None
    rest = line[22:]
    if rest.startswith("["):
        _, _, rest = rest.partition("] ")
    return line[1:20], rest


def _collapse_digits(text: str) -> str:
    out: list[str] = []
    in_digits = False
    for ch in text:
        if ch.isdigit():
            if not in_digits:
                out.append("N")
            in_digits = True
        else:
            in_digits = False
            out.append(ch)
    return "".join(out)


def log_stats(lines: Iterable[str]) -> dict:
    """Digest of the daemon log: messages, first-audio latency, pauses, errors, line kinds.

    First-audio latency is the gap from "Speaking for" to the next "Audio started" or
    "Sentence stream: first audio", which is the delay the listener feels. Line kinds are
    the messages with digit runs collapsed to N, ranked by count, so log noise stands out.
    """
    count = 0
    first: str | None = None
    last: str | None = None
    messages = streams = mic_pauses = errors = 0
    pending: datetime | None = None
    gaps: list[float] = []
    kinds: dict[str, int] = {}
    for line in lines:
        count += 1
        parts = _log_parts(line)
        if parts is None:
            continue
        stamp, msg = parts
        first = first or stamp
        last = stamp
        kinds[_collapse_digits(msg)[:60]] = kinds.get(_collapse_digits(msg)[:60], 0) + 1
        if "[ERROR]" in line[:30]:
            errors += 1
        if msg.startswith("Speaking for "):
            messages += 1
            pending = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
        elif msg.startswith("Sentence stream: first audio"):
            streams += 1
            if pending is not None:
                gaps.append(
                    (datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S") - pending).total_seconds()
                )
                pending = None
        elif msg.startswith("Audio started") and pending is not None:
            gaps.append((datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S") - pending).total_seconds())
            pending = None
        elif msg.startswith("Mic watcher: paused"):
            mic_pauses += 1
    gaps.sort()

    def pick(p: float) -> float:
        return gaps[min(len(gaps) - 1, int(p * len(gaps)))] if gaps else 0.0

    return {
        "lines": count,
        "first": first,
        "last": last,
        "messages": messages,
        "streams": streams,
        "mic_pauses": mic_pauses,
        "errors": errors,
        "latency": {
            "n": len(gaps),
            "median": pick(0.5),
            "p90": pick(0.9),
            "max": gaps[-1] if gaps else 0.0,
        },
        "kinds": sorted(((n, k) for k, n in kinds.items()), reverse=True)[:8],
    }


def format_log_stats(stats: dict, name: str, size_bytes: int) -> str:
    """The digest as a short report."""
    size = (
        f"{size_bytes / 1024:.1f} KB"
        if size_bytes < 1024 * 1024
        else f"{size_bytes / 1024 / 1024:.1f} MB"
    )
    span = f", {stats['first']} to {stats['last']}" if stats["first"] else ""
    lat = stats["latency"]
    out = [
        f"{name}: {stats['lines']} lines, {size}{span}",
        f"messages spoken: {stats['messages']}   streamed: {stats['streams']}   "
        f"mic pauses: {stats['mic_pauses']}   errors: {stats['errors']}",
        f"queue to first audio: median {lat['median']:.1f}s  p90 {lat['p90']:.1f}s  "
        f"max {lat['max']:.1f}s  (n={lat['n']})",
        "line kinds:",
    ]
    out += [f"  {n:>6}  {k}" for n, k in stats["kinds"]]
    return "\n".join(out)


def print_log_stats() -> None:
    """`claude-tts daemon stats`: the digest of the current daemon log."""
    if not LOG_FILE.exists():
        print(f"No log file found at {LOG_FILE}")
        return
    with open(LOG_FILE, errors="replace") as f:
        stats = log_stats(f.read().splitlines())
    print(format_log_stats(stats, LOG_FILE.name, LOG_FILE.stat().st_size))


def show_logs(follow: bool = False) -> None:
    """Show daemon log file."""
    if not LOG_FILE.exists():
        print(f"No log file found at {LOG_FILE}")
        return

    if follow:
        try:
            subprocess.run(["tail", "-F", str(LOG_FILE)])
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
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>{service_path_env(claude_tts_bin)}</string>
    <key>HOME</key>
    <string>{Path.home()}</string>
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
Environment=PATH={service_path_env(claude_tts_bin)}
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
