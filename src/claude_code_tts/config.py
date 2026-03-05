"""Configuration loading and session management for Claude Code TTS.

Replaces all jq-based config loading from tts-lib.sh with native Python.
Handles the session resolution chain: session.d file > project_personas > global config.
"""

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from claude_code_tts.session import get_session_id

logger = logging.getLogger("claude-tts")

# --- Paths ---

HOME = Path.home()
TTS_CONFIG_DIR = HOME / ".claude-tts"
TTS_CONFIG_FILE = TTS_CONFIG_DIR / "config.json"
TTS_SESSIONS_DIR = TTS_CONFIG_DIR / "sessions.d"
TTS_QUEUE_DIR = TTS_CONFIG_DIR / "queue"
VOICES_DIR = HOME / ".local" / "share" / "piper-voices"
PROJECTS_DIR = HOME / ".claude" / "projects"
DEBUG_LOG = Path("/tmp/claude_tts_debug.log")

# --- Debug logging ---


def debug(msg: str) -> None:
    """Append a timestamped debug line to the TTS debug log."""
    try:
        with open(DEBUG_LOG, "a") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except OSError:
        pass


# --- Data classes ---


@dataclass
class TTSConfig:
    """Merged TTS configuration (session + persona + global)."""

    mode: str = "direct"
    muted: bool = False
    intermediate: bool = True
    speed: float = 2.0
    speed_method: str = ""
    voice: str = "en_US-hfc_male-medium"
    voice_kokoro: str = ""
    voice_kokoro_blend: str = ""
    max_chars: int = 10000
    active_persona: str = "claude-prime"
    session_id: str = ""
    project_name: str = ""
    default_muted: bool = True
    global_muted: bool = False
    # Raw dicts for commands that need more detail
    raw_config: dict = field(default_factory=dict, repr=False)
    raw_session: dict = field(default_factory=dict, repr=False)

    @property
    def voice_path(self) -> Path:
        """Resolved .onnx path for the Piper voice."""
        return VOICES_DIR / f"{self.voice}.onnx"


# --- Config file I/O ---


def atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON atomically via temp file + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    try:
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def load_raw_config() -> dict:
    """Load config.json as a raw dict. Returns empty dict if missing."""
    if TTS_CONFIG_FILE.exists():
        try:
            with open(TTS_CONFIG_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            debug(f"Failed to read config: {e}")
    return {}


def save_raw_config(config: dict) -> None:
    """Write config.json atomically."""
    atomic_write_json(TTS_CONFIG_FILE, config)


# --- Session file helpers ---


def session_file(session_id: str) -> Path:
    """Return the path to a session's JSON file in sessions.d/."""
    return TTS_SESSIONS_DIR / f"{session_id}.json"


def session_read(session_id: str) -> dict:
    """Read a session file. Returns empty dict if missing."""
    sf = session_file(session_id)
    if sf.exists():
        try:
            with open(sf) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def session_set(session_id: str, key: str, value: object) -> None:
    """Set a key=value in a session file (creates file and dir if needed)."""
    sf = session_file(session_id)
    existing = session_read(session_id)
    existing[key] = value
    atomic_write_json(sf, existing)


def session_del(session_id: str, key: str) -> None:
    """Delete a key from a session file. No-op if file/key missing."""
    sf = session_file(session_id)
    if not sf.exists():
        return
    data = session_read(session_id)
    if key in data:
        del data[key]
        atomic_write_json(sf, data)


def migrate_session(session_id: str) -> bool:
    """Migrate a single session from legacy config.json .sessions to sessions.d/.

    Returns True if migration happened, False if no legacy data or already migrated.
    """
    sf = session_file(session_id)
    if sf.exists():
        return False

    config = load_raw_config()
    sessions = config.get("sessions", {})
    legacy = sessions.get(session_id)
    if not legacy:
        return False

    TTS_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_json(sf, legacy)

    # Remove from config.json
    del sessions[session_id]
    if not sessions:
        config.pop("sessions", None)
    else:
        config["sessions"] = sessions
    save_raw_config(config)
    debug(f"Migrated session {session_id} from config.json to sessions.d/")
    return True


def maybe_cleanup() -> None:
    """Auto-cleanup stale session files. Throttled to once per hour.

    Logs removed sessions with full JSON for restoration.
    """
    marker = TTS_SESSIONS_DIR / ".last_cleanup"
    now = int(time.time())

    if marker.exists():
        try:
            last = int(marker.read_text().strip())
            if now - last < 3600:
                return
        except (ValueError, OSError):
            pass

    TTS_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    marker.write_text(str(now))

    if not PROJECTS_DIR.is_dir():
        return

    for sf in TTS_SESSIONS_DIR.glob("*.json"):
        sid = sf.stem
        if not (PROJECTS_DIR / sid).is_dir():
            try:
                content = sf.read_text()
                debug(f"Auto-cleanup: removing {sid} | restore: echo '{content}' > {sf}")
                sf.unlink()
            except OSError as e:
                debug(f"Auto-cleanup failed for {sid}: {e}")


# --- Merged config loading ---


def load_config(session_id: str | None = None) -> TTSConfig:
    """Load fully merged TTS config for a session.

    Resolution chain:
        1. Session file (sessions.d/<id>.json)
        2. Global config (config.json) — persona values, project_personas, etc.
        3. Environment variable overrides
    """
    if session_id is None:
        session_id = get_session_id()
    assert isinstance(session_id, str)

    sid: str = session_id
    cfg = TTSConfig(session_id=sid)
    cfg.project_name = f"session-{sid[:8]}"

    # Step 1: Read session file (try migration first if needed)
    sf = session_file(sid)
    if not sf.exists():
        try:
            migrate_session(sid)
        except Exception:
            pass

    session_data = session_read(sid)
    cfg.raw_session = session_data

    # Step 2: Read global config
    config = load_raw_config()
    cfg.raw_config = config

    if not config:
        return cfg

    cfg.mode = config.get("mode", "direct")
    cfg.default_muted = config.get("default_muted", True)
    cfg.global_muted = config.get("muted", False)

    # Determine effective persona: session > project > global
    session_persona = session_data.get("persona", "")
    if session_persona:
        cfg.active_persona = session_persona
    else:
        project_personas = config.get("project_personas", {})
        cfg.active_persona = project_personas.get(
            session_id,
            config.get("active_persona", "claude-prime"),
        )

    # Get persona settings
    personas = config.get("personas", {})
    persona = personas.get(cfg.active_persona, personas.get("claude-prime", {}))

    if persona:
        cfg.speed = float(persona.get("speed", 2.0))
        cfg.speed_method = persona.get("speed_method", "")
        cfg.voice = persona.get("voice", "en_US-hfc_male-medium")
        cfg.max_chars = int(persona.get("max_chars", 10000))
        cfg.voice_kokoro = persona.get("voice_kokoro", "")
        cfg.voice_kokoro_blend = persona.get("voice_kokoro_blend", "")

    # Step 3: Determine mute state
    # Priority: session-level > global mute > default_muted for new sessions
    session_muted = session_data.get("muted")
    if session_muted is not None:
        cfg.muted = bool(session_muted)
    elif cfg.global_muted:
        cfg.muted = True
    elif cfg.default_muted and not sf.exists():
        cfg.muted = True

    # Session overrides for speed and intermediate
    session_speed = session_data.get("speed")
    if session_speed is not None:
        cfg.speed = float(session_speed)

    session_intermediate = session_data.get("intermediate")
    if session_intermediate is not None:
        cfg.intermediate = bool(session_intermediate)

    # Environment variable overrides
    if env_speed := os.environ.get("CLAUDE_TTS_SPEED"):
        cfg.speed = float(env_speed)
    if env_method := os.environ.get("CLAUDE_TTS_SPEED_METHOD"):
        cfg.speed_method = env_method
    if env_voice := os.environ.get("CLAUDE_TTS_VOICE"):
        cfg.voice = env_voice
    if env_max := os.environ.get("CLAUDE_TTS_MAX_CHARS"):
        cfg.max_chars = int(env_max)

    return cfg
