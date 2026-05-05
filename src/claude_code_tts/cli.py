"""Claude Code TTS - Unified CLI entry point.

Usage: claude-tts <command> [args]

Replaces all individual bash scripts with a single Python CLI.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import sys
import time
from pathlib import Path

from claude_code_tts import __version__
from claude_code_tts.config import (
    TTS_CONFIG_FILE,
    TTS_SESSIONS_DIR,
    VOICES_DIR,
    atomic_write_json,
    debug,
    load_config,
    load_raw_config,
    save_raw_config,
    session_del,
    session_read,
    session_set,
)
from claude_code_tts.session import get_session_id


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> None:
    """Show TTS status for current session."""
    sid = get_session_id()
    cfg = load_config(sid)

    # Determine mute source
    session_data = cfg.raw_session
    if "muted" in session_data:
        mute_source = "session"
    elif cfg.global_muted:
        mute_source = "global"
    elif cfg.default_muted:
        mute_source = "default_muted"
    else:
        mute_source = "default"

    # Daemon status
    pid_file = Path.home() / ".claude-tts" / "daemon.pid"
    daemon_status = "not running"
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)
            daemon_status = f"running (PID {pid})"
        except (ValueError, OSError):
            pass

    # Playback state
    playback_file = Path.home() / ".claude-tts" / "playback.json"
    pause_state = "false"
    paused_by = ""
    audio_pid = ""
    if playback_file.exists():
        try:
            pb = json.loads(playback_file.read_text())
            pause_state = str(pb.get("paused", False)).lower()
            paused_by = pb.get("paused_by", "") or ""
            audio_pid = str(pb.get("audio_pid", ""))
        except (json.JSONDecodeError, OSError):
            pass

    # Mic-aware pause
    raw_config = cfg.raw_config
    mic_aware = raw_config.get("mic_aware_pause", False)

    print(f"Session:  {sid}")
    print(f"Muted:    {str(cfg.muted).lower()} ({mute_source})")
    pause_detail = pause_state
    if pause_state == "true" and paused_by:
        pause_detail = f"true (by {paused_by})"
    print(f"Paused:   {pause_detail}")
    if audio_pid and audio_pid != "None":
        try:
            os.kill(int(audio_pid), 0)
            print(f"Playing:  yes (PID {audio_pid})")
        except (ValueError, OSError):
            pass
    print(f"Persona:  {cfg.active_persona}")
    print(f"Mode:     {cfg.mode}")
    print(f"Daemon:   {daemon_status}")
    print(f"Mic-aware: {'enabled' if mic_aware else 'disabled'}")


def cmd_mute(args: argparse.Namespace) -> None:
    """Mute TTS for this session or all sessions."""
    config = load_raw_config()
    if not config:
        print(f"Config file not found: {TTS_CONFIG_FILE}")
        print("Run claude-tts-install first")
        sys.exit(1)

    if getattr(args, "all", False):
        config["default_muted"] = True
        config["muted"] = True
        save_raw_config(config)

        count = 0
        if TTS_SESSIONS_DIR.is_dir():
            for sf in TTS_SESSIONS_DIR.glob("*.json"):
                try:
                    data = json.loads(sf.read_text())
                    data["muted"] = True
                    atomic_write_json(sf, data)
                    count += 1
                except (json.JSONDecodeError, OSError):
                    pass

        print(f"All sessions muted ({count} session files + global default).")
        print("")
        print("Every Claude session is now silent.")
        print("")
        print("Use /tts-unmute to restore voice for a specific session.")
        return

    sid = get_session_id()
    session_set(sid, "muted", True)
    print(f"Session muted: {sid}")
    print("")
    print("This session will no longer speak.")
    print("Other Claude sessions are unaffected.")
    print("")
    print("Use /tts-unmute to restore voice.")


def cmd_unmute(args: argparse.Namespace) -> None:
    """Unmute TTS for this session."""
    if not TTS_CONFIG_FILE.exists():
        print(f"Config file not found: {TTS_CONFIG_FILE}")
        print("Run claude-tts-install first")
        sys.exit(1)

    sid = get_session_id()
    session_set(sid, "muted", False)
    print(f"Session unmuted: {sid}")
    print("")
    print("This session will now speak.")
    print("Other Claude sessions are unaffected.")


def cmd_speed(args: argparse.Namespace) -> None:
    """Show or set speech speed."""
    sid = get_session_id()
    config = load_raw_config()

    value = args.value
    if not value:
        # Show current speed
        session_data = session_read(sid)
        session_speed = session_data.get("speed")
        session_persona = session_data.get("persona", "")

        if not session_persona:
            project_personas = config.get("project_personas", {})
            effective_persona = project_personas.get(
                sid, config.get("active_persona", "default")
            )
        else:
            effective_persona = session_persona

        personas = config.get("personas", {})
        persona_speed = personas.get(effective_persona, {}).get("speed", 2.0)

        if session_speed is not None:
            print(f"Speed:   {session_speed}x (session override)")
            print(f"Default: {persona_speed}x (from {effective_persona})")
        else:
            print(f"Speed: {persona_speed}x (from {effective_persona})")
        print("")
        print("Usage: /tts-speed <value|reset>")

    elif value == "reset":
        session_del(sid, "speed")

        session_data = session_read(sid)
        session_persona = session_data.get("persona", "")
        if not session_persona:
            project_personas = config.get("project_personas", {})
            effective_persona = project_personas.get(
                sid, config.get("active_persona", "default")
            )
        else:
            effective_persona = session_persona

        personas = config.get("personas", {})
        persona_speed = personas.get(effective_persona, {}).get("speed", 2.0)
        print(f"Speed reset to persona default: {persona_speed}x")

    else:
        try:
            speed_val = float(value)
        except ValueError:
            print(f"Invalid speed: {value} (must be a number)")
            sys.exit(1)

        if speed_val < 0.5 or speed_val > 4.0:
            print(f"Speed out of range: {value} (must be 0.5-4.0)")
            sys.exit(1)

        session_set(sid, "speed", speed_val)
        print(f"Speed set to {value}x")


def cmd_persona(args: argparse.Namespace) -> None:
    """Show or set voice persona."""
    sid = get_session_id()
    config = load_raw_config()
    if not config:
        print("No config file found")
        sys.exit(1)

    personas = config.get("personas", {})
    name = args.name

    if args.project:
        if not name:
            project_persona = config.get("project_personas", {}).get(sid, "")
            if project_persona:
                print(f"Project persona: {project_persona}")
            else:
                print("No project persona set")
                print("Use: /tts-persona --project <name>")
            return

        if name == "reset":
            pp = config.get("project_personas", {})
            pp.pop(sid, None)
            if pp:
                config["project_personas"] = pp
            else:
                config.pop("project_personas", None)
            save_raw_config(config)
            print("Project persona cleared")
            return

        if name not in personas:
            print(f"Persona not found: {name}")
            print("")
            print("Available personas:")
            for p in sorted(personas):
                print(f"  {p}")
            sys.exit(1)

        pp = config.get("project_personas", {})
        pp[sid] = name
        config["project_personas"] = pp
        save_raw_config(config)

        if args.session:
            session_set(sid, "persona", name)
            print(f"Project and session persona set to: {name}")
        else:
            print(f"Project persona set to: {name}")
            print("(This will be used by default for all sessions in this project)")
        return

    if not name or name == "list":
        # List personas
        print("Personas:")
        for key, val in personas.items():
            desc = val.get("description", "No description")
            print(f"  {key}: {desc}")
        print("")

        global_persona = config.get("active_persona", "default")
        project_persona = config.get("project_personas", {}).get(sid, "")
        session_data = session_read(sid)
        session_persona = session_data.get("persona", "")

        print(f"Global:  {global_persona}")
        print(f"Project: {project_persona or '(none)'}")
        print(f"Session: {session_persona or '(using project or global)'}")

        effective = global_persona
        if project_persona:
            effective = project_persona
        if session_persona:
            effective = session_persona
        print("")
        print(f"Effective: {effective}")

    elif name == "reset":
        session_del(sid, "persona")

        project_persona = config.get("project_personas", {}).get(sid, "")
        global_persona = config.get("active_persona", "default")

        if project_persona:
            print("Session persona cleared")
            print(f"Effective: {project_persona} (project default)")
        else:
            print("Session persona cleared")
            print(f"Effective: {global_persona} (global default)")

    else:
        if name not in personas:
            print(f"Persona not found: {name}")
            print("")
            print("Available personas:")
            for p in sorted(personas):
                print(f"  {p}")
            sys.exit(1)

        session_set(sid, "persona", name)
        print(f"Persona set to: {name}")


def cmd_intermediate(args: argparse.Namespace) -> None:
    """Toggle intermediate speech."""
    if not TTS_CONFIG_FILE.exists():
        print(f"Config file not found: {TTS_CONFIG_FILE}")
        print("Run claude-tts-install first")
        sys.exit(1)

    sid = get_session_id()

    if args.action == "on":
        session_set(sid, "intermediate", True)
        print(f"Intermediate speech enabled for: {sid}")
        print("")
        print("You will hear narration between tool calls.")
    elif args.action == "off":
        session_set(sid, "intermediate", False)
        print(f"Intermediate speech disabled for: {sid}")
        print("")
        print("Only final responses will be spoken.")
        print("Use /tts-intermediate on to restore.")
    else:
        session_data = session_read(sid)
        intermediate = session_data.get("intermediate", True)
        if intermediate:
            print("Intermediate speech: ENABLED (hearing all narration)")
        else:
            print("Intermediate speech: DISABLED (final responses only)")
        print("")
        print("Commands:")
        print("  /tts-intermediate on   - Hear narration between tool calls")
        print("  /tts-intermediate off  - Only hear final responses")


def cmd_cleanup(args: argparse.Namespace) -> None:
    """Remove stale session entries."""
    projects_dir = Path.home() / ".claude" / "projects"
    if not projects_dir.is_dir():
        print("No Claude projects directory found")
        return

    stale = []
    active = []
    if TTS_SESSIONS_DIR.is_dir():
        for sf in TTS_SESSIONS_DIR.glob("*.json"):
            sid = sf.stem
            if (projects_dir / sid).is_dir():
                active.append(sid)
            else:
                stale.append((sid, sf))

    # Check legacy config.json entries
    legacy_stale = []
    config = load_raw_config()
    for sid in config.get("sessions", {}):
        if not (projects_dir / sid).is_dir():
            legacy_stale.append(sid)

    print(f"Active sessions: {len(active)}")
    print(f"Stale sessions:  {len(stale)} (sessions.d) + {len(legacy_stale)} (legacy config.json)")

    total = len(stale) + len(legacy_stale)
    if total == 0:
        print("")
        print("Nothing to clean up.")
        return

    print("")
    print("Stale sessions (project directories no longer exist):")
    for sid, _ in stale:
        print(f"  - {sid} (sessions.d)")
    for sid in legacy_stale:
        print(f"  - {sid} (config.json legacy)")

    if args.dry_run:
        print("")
        print("(dry run - no changes made)")
        return

    print("")
    try:
        reply = input(f"Remove these {total} stale session(s)? [y/N] ")
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled")
        return

    if reply.strip().lower() != "y":
        print("Cancelled")
        return

    for sid, sf in stale:
        try:
            content = sf.read_text()
            debug(f"Cleanup: removing {sid} | restore: echo '{content}' > {sf}")
            sf.unlink()
        except OSError:
            pass

    if legacy_stale:
        sessions = config.get("sessions", {})
        for sid in legacy_stale:
            sessions.pop(sid, None)
        if sessions:
            config["sessions"] = sessions
        else:
            config.pop("sessions", None)
        save_raw_config(config)

    print(f"Removed {total} stale session(s)")

    # Also sweep stale session pin files (claude PIDs that no longer exist)
    from claude_code_tts.session import cleanup_stale_pins
    pin_count = cleanup_stale_pins()
    if pin_count:
        print(f"Removed {pin_count} stale session pin(s)")


def cmd_sounds(args: argparse.Namespace) -> None:
    """Toggle or configure notification sounds."""
    config = load_raw_config()
    if not config:
        print(f"No config file found at {TTS_CONFIG_FILE}")
        print("Run claude-tts-install first")
        sys.exit(1)

    sounds = config.get("sounds", {})
    play_sound = Path.home() / ".claude" / "hooks" / "play-sound.sh"

    if args.action == "on":
        sounds["enabled"] = True
        config["sounds"] = sounds
        save_raw_config(config)
        print("Notification sounds enabled")
        if play_sound.exists() and os.access(play_sound, os.X_OK):
            subprocess.Popen([str(play_sound), "unmuted"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    elif args.action == "off":
        sounds["enabled"] = False
        config["sounds"] = sounds
        save_raw_config(config)
        print("Notification sounds disabled")

    elif args.action == "test":
        if play_sound.exists() and os.access(play_sound, os.X_OK):
            subprocess.run([str(play_sound), "unmuted"])
            print("Test sound played")
        else:
            print(f"Sound player not found at {play_sound}")

    else:
        enabled = sounds.get("enabled", False)
        volume = sounds.get("volume", 0.5)
        if enabled:
            print(f"Sounds: ENABLED (volume: {volume})")
        else:
            print("Sounds: DISABLED")
        print("")
        events = sounds.get("events", {})
        if events:
            print("Event sounds:")
            for key, val in events.items():
                print(f"  {key}: {val or 'none'}")
        print("")
        print("Commands:")
        print("  /tts-sounds on    - Enable notification sounds")
        print("  /tts-sounds off   - Disable notification sounds")
        print("  /tts-sounds test  - Play a test beep")


def cmd_random(args: argparse.Namespace) -> None:
    """Generate a random TTS persona."""
    config = load_raw_config()
    if not config:
        print("No config file found")
        sys.exit(1)

    if not VOICES_DIR.is_dir():
        print(f"No voices directory found at {VOICES_DIR}")
        sys.exit(1)

    voices = [f.stem for f in VOICES_DIR.glob("*.onnx")]
    if not voices:
        print(f"No voices found in {VOICES_DIR}")
        sys.exit(1)

    random_voice = random.choice(voices)
    speed_options = [1.8, 1.9, 2.0, 2.1, 2.2]
    random_speed = random.choice(speed_options)
    persona_name = f"random-{int(time.time())}"

    if args.preview:
        print("Would create random persona:")
        print(f"  Voice: {random_voice}")
        print(f"  Speed: {random_speed}x")
        print("")
        print("Available voices:")
        for v in sorted(voices):
            print(f"  {v}")
        return

    personas = config.get("personas", {})
    personas[persona_name] = {
        "description": "Randomly generated persona",
        "voice": random_voice,
        "speed": random_speed,
        "speed_method": "playback",
        "max_chars": 10000,
        "ai_type": "claude",
    }
    config["personas"] = personas
    save_raw_config(config)

    sid = get_session_id()
    session_set(sid, "persona", persona_name)

    print("Random persona applied:")
    print(f"  Voice: {random_voice}")
    print(f"  Speed: {random_speed}x")
    print(f"  Persona: {persona_name}")


def cmd_discover(args: argparse.Namespace) -> None:
    """Gather repo context for persona discovery."""
    config = load_raw_config()
    sid = get_session_id()

    print("=== REPO CONTEXT ===")
    print("")
    print(f"Directory: {os.getcwd()}")
    print(f"Session: {sid}")
    print("")

    for fname in [
        "CLAUDE.md", "README.md", "README", "package.json", "pyproject.toml",
        "Cargo.toml", "go.mod", "build.gradle", "pom.xml", "Makefile",
    ]:
        p = Path(fname)
        if p.is_file():
            print(f"--- {fname} (first 30 lines) ---")
            try:
                lines = p.read_text().splitlines()[:30]
                print("\n".join(lines))
            except OSError:
                pass
            print("")

    print("--- Directory structure ---")
    try:
        entries = sorted(Path(".").iterdir())[:20]
        for e in entries:
            print(e.name)
    except OSError:
        pass
    print("")

    print("=== AVAILABLE PERSONAS ===")
    print("")
    personas = config.get("personas", {})
    if personas:
        for key, val in personas.items():
            voice = val.get("voice", "default")
            speed = val.get("speed", 1.0)
            ai_type = val.get("ai_type", "unset")
            print(f"- {key}: voice={voice}, speed={speed}, ai_type={ai_type}")
    else:
        print("No personas configured")

    print("")
    print("=== CURRENT STATE ===")
    print("")

    session_data = session_read(sid)
    session_persona = session_data.get("persona", "")
    if session_persona:
        current = session_persona
    else:
        project_personas = config.get("project_personas", {})
        current = project_personas.get(sid, config.get("active_persona", "default"))

    print(f"Current persona: {current}")
    project_persona = config.get("project_personas", {}).get(sid, "not set")
    print(f"Project persona: {project_persona}")


def _detect_sherpa_layout(model_dir: Path) -> str:
    """Best-effort layout family detection. Mirrors sherpa_speak._build_tts logic.

    Returns one of: "vits", "kokoro", "matcha", "incomplete".
    """
    has_model = (model_dir / "model.onnx").is_file()
    has_tokens = (model_dir / "tokens.txt").is_file()
    has_voices = (model_dir / "voices.bin").is_file()
    has_am = (model_dir / "am.onnx").is_file()
    has_vocoder = (model_dir / "vocoder.onnx").is_file()

    if has_voices and has_model and has_tokens:
        return "kokoro"
    if has_am and has_vocoder and has_tokens:
        return "matcha"
    if has_model and has_tokens:
        return "vits"
    return "incomplete"


def _dir_size_mb(path: Path) -> int:
    """Sum file sizes under `path`, in megabytes. Returns -1 on error."""
    try:
        total = 0
        for p in path.rglob("*"):
            if p.is_file():
                total += p.stat().st_size
        return total // (1024 * 1024)
    except OSError:
        return -1


def cmd_sherpa(args: argparse.Namespace) -> None:
    """Manage sherpa-onnx models and venv."""
    from claude_code_tts.config import SHERPA_MODELS_DIR, SHERPA_VENV_DIR

    sub = getattr(args, "sherpa_command", None) or "list"

    if sub == "list":
        # Header
        print(f"Sherpa models directory: {SHERPA_MODELS_DIR}")
        venv_status = "ready" if (SHERPA_VENV_DIR / "bin" / "python").is_file() \
            else "NOT bootstrapped (run: claude-tts-install --enable-sherpa)"
        print(f"Sherpa venv:             {venv_status}")
        print()

        if not SHERPA_MODELS_DIR.is_dir():
            print("No models installed.")
            print()
            print("To install a model:")
            print(f"  1. Create directory: {SHERPA_MODELS_DIR}/<model-id>/")
            print("  2. Drop the model artifacts in. Minimum: model.onnx + tokens.txt")
            print("  3. Re-run `claude-tts sherpa list` to verify it's recognized")
            print()
            print("(Curated picklist + auto-download is on the roadmap.)")
            return

        entries = sorted([p for p in SHERPA_MODELS_DIR.iterdir() if p.is_dir()])
        if not entries:
            print("No models installed.")
            print()
            print(f"Drop a model directory under {SHERPA_MODELS_DIR}/<model-id>/")
            print("(Minimum: model.onnx + tokens.txt)")
            return

        # Column widths
        name_w = max(len("MODEL ID"), max(len(p.name) for p in entries))
        print(f"{'MODEL ID':<{name_w}}  {'LAYOUT':<11}  {'SIZE':<8}  STATUS")
        print(f"{'-' * name_w}  {'-' * 11}  {'-' * 8}  {'-' * 6}")
        for p in entries:
            layout = _detect_sherpa_layout(p)
            size = _dir_size_mb(p)
            size_str = f"{size} MB" if size >= 0 else "?"
            status = "ready" if layout != "incomplete" else "incomplete (missing files)"
            print(f"{p.name:<{name_w}}  {layout:<11}  {size_str:<8}  {status}")
        print()
        print("Test a voice:")
        print('  claude-tts speak --voice-sherpa <id> --speaker-sherpa <n> "hello"')
        return

    print(f"Unknown sherpa subcommand: {sub}")
    print("Available: list")


def cmd_mode(args: argparse.Namespace) -> None:
    """Show or set TTS mode."""
    config = load_raw_config()

    if not args.value:
        mode = config.get("mode", "direct")
        print(f"Mode: {mode}")
        print("")
        print("Usage: /tts-mode direct|queue")
        return

    config["mode"] = args.value
    save_raw_config(config)
    print(f"Mode set to: {args.value}")


def cmd_pause(args: argparse.Namespace) -> None:
    """Toggle TTS playback pause/resume."""
    playback_file = Path.home() / ".claude-tts" / "playback.json"

    state = {"paused": False, "audio_pid": None}
    if playback_file.exists():
        try:
            state = json.loads(playback_file.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    if state.get("paused", False):
        state["paused"] = False
        state["paused_by"] = None
        state["updated_at"] = time.time()
        atomic_write_json(playback_file, state)
        # macOS notification
        subprocess.run(
            ["osascript", "-e", 'display notification "Playback resumed" with title "Claude TTS"'],
            capture_output=True,
        )
        print("Resumed")
    else:
        state["paused"] = True
        state["paused_by"] = "user"
        state["updated_at"] = time.time()

        audio_pid = state.get("audio_pid")
        atomic_write_json(playback_file, state)

        if audio_pid:
            try:
                os.kill(int(audio_pid), 0)
                os.kill(int(audio_pid), 15)  # SIGTERM
            except (ValueError, OSError):
                pass

        subprocess.run(
            ["osascript", "-e", 'display notification "Playback paused" with title "Claude TTS"'],
            capture_output=True,
        )
        print("Paused")


def cmd_test(args: argparse.Namespace) -> None:
    """Test TTS with a realistic workflow sample."""
    from claude_code_tts.audio import generate_speech, play_audio

    cfg = load_config()

    quick = "Hello! I'm ready to help you with your project today."
    table = (
        "Here's a summary of the changes. The first item is authentication, "
        "which is complete. The second item is database migrations, currently "
        "in progress. The third item is API endpoints, still pending review."
    )
    full = (
        "Alright, I've analyzed the codebase and here's what I found.\n\n"
        "The main issue is in the authentication module. There are three files "
        "we need to update: the user controller, the auth middleware, and the "
        "session handler.\n\n"
        "For the user controller, we need to add input validation before "
        "processing the login request. This prevents injection attacks and "
        "ensures data integrity.\n\n"
        "The auth middleware needs a small fix. Currently it's not properly "
        "checking token expiration, which could allow stale sessions to persist.\n\n"
        "Finally, the session handler should implement the new refresh token "
        "logic we discussed.\n\n"
        "I'll start with the user controller since that's the most critical "
        "security fix. Does this plan sound good to you?"
    )

    if args.quick:
        text = quick
        test_type = "quick"
    elif args.table:
        text = table
        test_type = "table"
    else:
        text = full
        test_type = "full"

    method = cfg.speed_method or "playback"

    print(f"Testing persona: {cfg.active_persona}")
    print(f"  Voice: {cfg.voice}")
    print(f"  Speed: {cfg.speed}x ({method})")
    print("")
    print(f"Playing {test_type} test...")

    wav = generate_speech(
        text,
        voice_path=cfg.voice_path,
        voice_kokoro=cfg.voice_kokoro,
        voice_kokoro_blend=cfg.voice_kokoro_blend,
        speed=cfg.speed,
        speed_method=method,
    )
    if wav:
        play_audio(wav, speed=cfg.speed, speed_method=method, background=False)
    else:
        print("Failed to generate speech")
        return

    print("")
    print("Test complete. How did that sound?")
    print("")
    print(f"Adjust settings with:")
    print(f"  /tts-speed <value>     Change speed (current: {cfg.speed}x)")
    print(f"  /tts-persona <name>    Switch persona")
    print(f"  /tts-random            Try a random voice")


def cmd_speak(args: argparse.Namespace) -> None:
    """Speak text (standalone tool or from hook)."""
    from claude_code_tts.audio import generate_speech, play_audio

    if args.from_hook:
        _speak_from_hook(args)
        return

    # --from-file mode: read file, filter for speech, speak it
    if args.from_file:
        from claude_code_tts.filter import read_and_filter
        file_path = Path(args.from_file).expanduser()
        try:
            text = read_and_filter(file_path)
        except FileNotFoundError as e:
            print(str(e))
            sys.exit(1)
        if not text:
            print("File produced no speakable content after filtering.")
            sys.exit(0)
        if args.reader:
            original = file_path.read_text(encoding="utf-8", errors="replace")
            _print_reader(original, text)
            if args.preview:
                return
            # Show reader then speak
            print()
            from claude_code_tts.audio import speak
            cfg = load_config()
            words = text.split()
            print(f"Reading {len(words)} words via {'queue' if cfg.mode == 'queue' else 'direct'} mode...")
            speak(text, cfg)
            return
        if args.preview:
            _print_preview(text)
            return
        # Use the queue so we don't stomp on other speech
        from claude_code_tts.audio import speak
        cfg = load_config()
        words = text.split()
        print(f"Reading {len(words)} words via {'queue' if cfg.mode == 'queue' else 'direct'} mode...")
        speak(text, cfg)
        return
    else:
        # Standalone mode: claude-tts speak "text" [--voice V] [--speed S]
        text = args.text
        if not text:
            print("Usage: claude-tts speak \"text to speak\"")
            print("       claude-tts speak --from-file <path> [--preview]")
            print("       claude-tts speak --from-hook --hook-type stop")
            sys.exit(1)

    cfg = load_config()
    voice_path = cfg.voice_path
    voice_kokoro = cfg.voice_kokoro
    voice_kokoro_blend = cfg.voice_kokoro_blend
    voice_sherpa = cfg.voice_sherpa
    speaker_sherpa = cfg.speaker_sherpa
    speed = cfg.speed
    speed_method = cfg.speed_method or "playback"
    speaker = None

    # CLI overrides for sherpa take precedence and disable other backends
    # for this one-shot call.
    if getattr(args, "voice_sherpa", None):
        voice_sherpa = args.voice_sherpa
        speaker_sherpa = getattr(args, "speaker_sherpa", -1)
        voice_kokoro = ""
        voice_kokoro_blend = ""
        voice_path = None

    if args.voice:
        voice_path = VOICES_DIR / f"{args.voice}.onnx"
        voice_kokoro = ""
        voice_kokoro_blend = ""
        voice_sherpa = ""
    if args.speed:
        speed = args.speed
    if args.speaker is not None:
        speaker = args.speaker
    if getattr(args, "random", False):
        # Random speaker from multi-speaker model
        voice_json = voice_path.with_suffix(".onnx.json")
        if voice_json.exists():
            try:
                meta = json.loads(voice_json.read_text())
                num_speakers = meta.get("num_speakers", 1)
                if num_speakers > 1:
                    speaker = random.randint(0, num_speakers - 1)
                    print(f"Random speaker: {speaker}/{num_speakers}")
            except (json.JSONDecodeError, OSError):
                pass

    if voice_sherpa:
        print(f"Voice: sherpa/{voice_sherpa}")
        if speaker_sherpa >= 0:
            print(f"Speaker: {speaker_sherpa}")
    else:
        print(f"Voice: {voice_path.stem if voice_path else 'kokoro'}")
        if speaker is not None:
            print(f"Speaker: {speaker}")
    print(f"Speed: {speed}x ({speed_method})")
    print()

    wav = generate_speech(
        text,
        voice_path=voice_path if voice_path and voice_path.exists() else None,
        voice_kokoro=voice_kokoro,
        voice_kokoro_blend=voice_kokoro_blend,
        voice_sherpa=voice_sherpa,
        speaker_sherpa=speaker_sherpa,
        speed=speed,
        speed_method=speed_method,
        speaker=speaker,
    )
    if wav:
        play_audio(wav, speed=speed, speed_method=speed_method, background=False)
    else:
        print("Failed to generate speech")
        sys.exit(1)


def _print_preview(text: str) -> None:
    """Print a word-wrapped preview of what would be spoken."""
    print("--- Preview (what would be spoken) ---")
    print()
    words = text.split()
    line = ""
    for word in words:
        if len(line) + len(word) + 1 > 80:
            print(line)
            line = word
        else:
            line = f"{line} {word}" if line else word
    if line:
        print(line)
    print()
    print(f"--- {len(words)} words, ~{len(words) // 150 + 1} min at 2x ---")


def _print_reader(original: str, filtered: str) -> None:
    """Print side-by-side view: original (left) | filtered (right).

    Gives you the visual follow-along while the voice reads the filtered version.
    If something in the filtered text doesn't make sense, glance left for context.

    Aligns by paragraph so related content stays roughly at the same vertical
    position on both sides.
    """
    try:
        term_width = os.get_terminal_size().columns
    except OSError:
        term_width = 120

    # Each side gets half the width minus the divider
    col_width = (term_width - 3) // 2  # 3 for " | " divider
    if col_width < 20:
        # Terminal too narrow for side-by-side, fall back to sequential
        print("=== Original ===")
        print(original)
        print()
        print("=== Filtered (what will be spoken) ===")
        print(filtered)
        return

    import textwrap

    def wrap_paragraph(text: str, width: int) -> list[str]:
        """Wrap a paragraph into lines, preserving blank lines as separators."""
        if not text.strip():
            return [""]
        return textwrap.wrap(text, width=width) or [""]

    # Split both sides into paragraphs (blank-line separated)
    left_paras = re.split(r"\n\n+", original)
    right_paras = re.split(r"\n\n+", filtered) if filtered.strip() else [""]

    # Print header
    header_l = "ORIGINAL".center(col_width)
    header_r = "SPOKEN".center(col_width)
    print(f"{header_l} | {header_r}")
    print(f"{'-' * col_width}-+-{'-' * col_width}")

    # Walk through paragraphs, aligning them vertically
    # Each original paragraph gets paired with the next available spoken paragraph
    ri = 0  # right paragraph index
    for left_para in left_paras:
        # Wrap the left paragraph into lines
        left_lines: list[str] = []
        for line in left_para.split("\n"):
            if not line.strip():
                left_lines.append("")
            else:
                left_lines.extend(textwrap.wrap(line, width=col_width) or [""])

        # Get the matching right paragraph (if any remain)
        right_lines: list[str] = []
        if ri < len(right_paras):
            right_lines = wrap_paragraph(right_paras[ri], col_width)
            ri += 1

        # Pad to equal height
        height = max(len(left_lines), len(right_lines))
        left_lines.extend([""] * (height - len(left_lines)))
        right_lines.extend([""] * (height - len(right_lines)))

        for left, right in zip(left_lines, right_lines):
            print(f"{left:<{col_width}} | {right:<{col_width}}")

        # Separator between paragraph groups
        print(f"{'':<{col_width}} | {'':<{col_width}}")

    # Print any remaining right paragraphs
    while ri < len(right_paras):
        right_lines = wrap_paragraph(right_paras[ri], col_width)
        ri += 1
        for right in right_lines:
            print(f"{'':<{col_width}} | {right:<{col_width}}")
        print(f"{'':<{col_width}} | {'':<{col_width}}")


def _speak_from_hook(args: argparse.Namespace) -> None:
    """Handle --from-hook mode: read hook JSON from stdin, process transcript."""
    from claude_code_tts.audio import speak
    from claude_code_tts.config import debug
    from claude_code_tts.filter import filter_text

    hook_type = args.hook_type or "stop"
    debug(f"=== {hook_type} hook triggered (Python) ===")

    # Read hook JSON from stdin
    hook_input = sys.stdin.read()
    if not hook_input:
        debug("No input received")
        return

    try:
        hook_data = json.loads(hook_input)
    except json.JSONDecodeError:
        debug("Invalid JSON input")
        return

    transcript_path = hook_data.get("transcript_path", "")
    if not transcript_path or not Path(transcript_path).is_file():
        debug(f"No transcript or file not found: {transcript_path}")
        return

    # Check CLAUDE_TTS_ENABLED env
    if os.environ.get("CLAUDE_TTS_ENABLED", "1") != "1":
        debug("TTS disabled via env var")
        return

    # Detect session from transcript path
    session_id = os.environ.get("CLAUDE_TTS_SESSION", "")
    if not session_id:
        import re
        m = re.search(r"/projects/([^/]+)/", transcript_path)
        if m:
            session_id = m.group(1)
            debug(f"Auto-detected session: {session_id}")

    if not session_id:
        debug("Could not detect session")
        return

    # Pin the canonical session ID so CLI subprocesses (slash commands) under
    # the same `claude` parent can resolve the same ID via process-tree lookup.
    # This is the single source of truth — see session.py for details.
    from claude_code_tts.session import pin_session
    pin_session(session_id)

    cfg = load_config(session_id)

    if cfg.muted:
        debug(f"{hook_type}: muted, skipping")
        return

    # Skip intermediate speech if disabled
    if hook_type == "post_tool_use":
        if not cfg.intermediate:
            debug("PostToolUse: intermediate disabled, skipping")
            return
        # Skip boilerplate tools
        tool_name = hook_data.get("tool_name", "")
        if tool_name in ("Task", "TodoWrite"):
            debug(f"PostToolUse: skipping tool type {tool_name}")
            return

    # Watermark handling.
    # Keyed by transcript UUID, not session_id, so two `claude` instances
    # opened in the same folder don't thrash each other's watermark. Folder-
    # scoped session_id stays the right grain for mute/persona; per-transcript
    # is the right grain for "what have I already spoken from this file".
    transcript = Path(transcript_path)
    transcript_key = transcript.stem or session_id
    state_file = Path(f"/tmp/claude_tts_spoken_{transcript_key}.state")
    lock_dir = Path(f"/tmp/claude_tts_wm_{transcript_key}.lock")

    watermark = _read_watermark(state_file, lock_dir, transcript)
    current_lines = _count_lines(transcript)
    debug(f"{hook_type}: watermark={watermark} current={current_lines}")

    if hook_type == "stop":
        # Brief yield to let concurrent PostToolUse finish
        time.sleep(0.1)
        watermark = _read_watermark(state_file, lock_dir, transcript)
        current_lines = _count_lines(transcript)
        _write_watermark(state_file, lock_dir, current_lines)

    if current_lines <= watermark:
        debug(f"{hook_type}: no new lines since last speak")
        return

    # Scan transcript for assistant text
    text = _extract_assistant_text(transcript, watermark, hook_type)
    if not text:
        debug(f"{hook_type}: no assistant text found")
        return

    debug(f"{hook_type}: extracted {len(text)} chars: {text[:100]}...")

    # Filter and check length
    cliff_notes = filter_text(text)
    if not cliff_notes or len(cliff_notes) < 10:
        debug(f"{hook_type}: text too short after filtering")
        return

    # Update watermark before speaking (for post_tool_use)
    if hook_type == "post_tool_use":
        _write_watermark(state_file, lock_dir, current_lines)
        debug(f"PostToolUse: watermark updated to {current_lines}")

    speak(cliff_notes, cfg)
    debug(f"{hook_type}: speech queued/played")


def _count_lines(path: Path) -> int:
    """Count lines in a file."""
    try:
        with open(path, "rb") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def _read_watermark(state_file: Path, lock_dir: Path, transcript: Path) -> int:
    """Read watermark with mkdir-based locking."""
    _watermark_lock(lock_dir)
    try:
        wm = 0
        if state_file.exists():
            try:
                wm = int(state_file.read_text().strip())
            except (ValueError, OSError):
                wm = 0

        # Auto-reset stale watermark
        current = _count_lines(transcript)
        if wm > current:
            from claude_code_tts.config import debug
            debug(f"Watermark reset: was {wm} but transcript only has {current} lines")
            wm = 0
            try:
                state_file.write_text("0")
            except OSError:
                pass
        return wm
    finally:
        _watermark_unlock(lock_dir)


def _write_watermark(state_file: Path, lock_dir: Path, line_count: int) -> None:
    """Write watermark with mkdir-based locking."""
    _watermark_lock(lock_dir)
    try:
        state_file.write_text(str(line_count))
    except OSError:
        pass
    finally:
        _watermark_unlock(lock_dir)


def _watermark_lock(lock_dir: Path) -> None:
    """Acquire mkdir-based lock (works on macOS and Linux)."""
    for attempt in range(20):
        try:
            lock_dir.mkdir()
            return
        except FileExistsError:
            if attempt >= 19:
                # Break stale lock
                import shutil
                shutil.rmtree(lock_dir, ignore_errors=True)
                try:
                    lock_dir.mkdir()
                except FileExistsError:
                    pass
                return
            time.sleep(0.05)


def _watermark_unlock(lock_dir: Path) -> None:
    """Release mkdir-based lock."""
    import shutil
    shutil.rmtree(lock_dir, ignore_errors=True)


def _extract_assistant_text(transcript: Path, watermark: int, hook_type: str) -> str:
    """Extract assistant text from transcript lines after watermark."""
    try:
        with open(transcript) as f:
            all_lines = f.readlines()
    except OSError:
        return ""

    if hook_type == "stop" and watermark == 0:
        # No watermark: scan in reverse for last assistant message with text
        for line in reversed(all_lines):
            text = _parse_assistant_text(line)
            if text:
                return text
        return ""

    # Scan new lines (after watermark) for the last assistant text
    new_lines = all_lines[watermark:]
    last_text = ""
    for line in new_lines:
        text = _parse_assistant_text(line)
        if text:
            last_text = text
            if hook_type == "stop":
                continue  # Want the last one
            # For post_tool_use, also want the last one
    return last_text


def _parse_assistant_text(line: str) -> str:
    """Parse a transcript JSONL line for assistant text content."""
    try:
        data = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return ""

    if data.get("type") != "assistant":
        return ""

    content = data.get("message", {}).get("content")
    if not content:
        return ""

    if isinstance(content, str):
        return content

    if isinstance(content, list):
        texts = [
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        return " ".join(texts) if texts else ""

    return ""


def cmd_audition(args: argparse.Namespace) -> None:
    """Interactive voice audition tool."""
    import shutil
    import tty
    import termios

    from claude_code_tts.audio import generate_speech, detect_player, write_queue_message, daemon_healthy

    temp_file = Path(f"/tmp/tts_audition_{os.getpid()}.wav")
    use_queue = getattr(args, "queue", False)

    speed = getattr(args, "speed", None) or 1.5
    method = "playback"
    custom_text = getattr(args, "text", None)
    text = custom_text or (
        "Three hidden keys open three secret gates, wherein the errant "
        "will be tested for worthy traits. And those with the skill to "
        "survive these straits will reach the end where the prize awaits."
    )

    def cleanup() -> None:
        temp_file.unlink(missing_ok=True)

    import atexit
    atexit.register(cleanup)

    def queue_speak(
        clip_text: str,
        voice_kokoro: str = "",
        voice_kokoro_blend: str = "",
        voice_piper: str = "",
        speaker: int = -1,
    ) -> None:
        """Send text through daemon queue with specific voice settings."""
        if not daemon_healthy():
            print("  Daemon not running (start with: claude-tts daemon start)")
            return
        from claude_code_tts.config import TTSConfig
        cfg = TTSConfig(
            mode="queue",
            speed=speed,
            speed_method=method,
            voice_kokoro=voice_kokoro,
            voice_kokoro_blend=voice_kokoro_blend,
            voice=voice_piper or "en_US-hfc_male-medium",
            session_id="audition",
            project_name="audition",
        )
        if speaker >= 0:
            # The daemon reads speaker from the queue JSON directly
            pass
        write_queue_message(clip_text, cfg)
        # Brief pause so queue messages don't pile up
        time.sleep(0.5)

    def _kokoro_display_name(voice: str) -> str:
        """Convert 'am_adam' to 'Adam'."""
        raw = voice.split("_", 1)[1] if "_" in voice else voice
        return " ".join(w.capitalize() for w in raw.replace("_", " ").split())

    def play_clip(wav: Path, rate: float = 1.0) -> subprocess.Popen | None:
        """Play WAV and return the process."""
        player = detect_player()
        if not player:
            return None
        cmd = list(player)
        if cmd[0] == "afplay" and rate != 1.0:
            cmd.extend(["-r", str(rate)])
        cmd.append(str(wav))
        return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def wait_with_skip(proc: subprocess.Popen | None) -> bool:
        """Wait for process, return True if skipped via space."""
        if not proc:
            return False
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            while proc.poll() is None:
                # Check for space key (non-blocking read)
                import select
                if select.select([sys.stdin], [], [], 0.1)[0]:
                    ch = sys.stdin.read(1)
                    if ch == " ":
                        proc.terminate()
                        try:
                            proc.wait(timeout=1)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                        return True
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        return False

    def read_key() -> str:
        """Read a single keypress."""
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = sys.stdin.read(1)
            return ch
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    def prompt_action(_voice: str = "", _speaker: str = "", _is_kokoro: bool = False) -> str:
        """Prompt for next action. Returns 'next', 'keep', 'replay', 'quit'."""
        print()
        print("[Enter] Next  [k] Keep  [r] Replay  [q] Quit")
        while True:
            key = read_key()
            if key in ("\r", "\n", ""):
                return "next"
            if key in ("k", "K"):
                return "keep"
            if key in ("r", "R"):
                return "replay"
            if key in ("q", "Q"):
                return "quit"

    def save_persona(name: str, voice: str, speaker: str = "", is_kokoro: bool = False) -> None:
        """Save a voice as a named persona."""
        config = load_raw_config()
        personas = config.get("personas", {})
        if is_kokoro:
            personas[name] = {
                "description": f"Audition - Kokoro {voice}",
                "voice_kokoro": voice,
                "speed": speed,
                "speed_method": "playback",
                "max_chars": 10000,
                "ai_type": "claude",
            }
        elif speaker:
            personas[name] = {
                "description": f"Audition - {voice} speaker {speaker}",
                "voice": voice,
                "speaker": int(speaker),
                "speed": speed,
                "speed_method": method,
                "max_chars": 10000,
                "ai_type": "claude",
            }
        else:
            personas[name] = {
                "description": f"Audition - {voice}",
                "voice": voice,
                "speed": speed,
                "speed_method": method,
                "max_chars": 10000,
                "ai_type": "claude",
            }
        config["personas"] = personas
        save_raw_config(config)
        print(f"Saved persona: {name}")

    def kokoro_audition_sequence(voice: str) -> None:
        """Full Kokoro audition: intro at 1x, text at 1x, text at 2x, outro at 1x."""
        display = _kokoro_display_name(voice)

        intro = f"Hi there, my name is {display}, and I'm auditioning for the role of your AI assistant today."
        outro = f"That was me, {display}. Thanks for listening."

        for label, clip_text, clip_speed in [
            ("1x intro", intro, 1.0),
            ("1x text", text, 1.0),
            ("2x text", text, 2.0),
            ("1x outro", outro, 1.0),
        ]:
            print(f"  [{label}]")
            if use_queue:
                queue_speak(clip_text, voice_kokoro=voice)
            else:
                wav = generate_speech(
                    clip_text,
                    voice_kokoro=voice,
                    output_path=temp_file,
                )
                if wav:
                    proc = play_clip(wav, clip_speed)
                    skipped = wait_with_skip(proc)
                    if skipped:
                        print("  (skipped)")

    print()
    print("========================================")
    print("    TTS Broadway Auditions")
    print("========================================")
    print()
    print(f"Speed: {speed}x ({method})")
    if use_queue:
        print("Playback: daemon queue")
    print(f"Text: \"{text[:50]}...\"")
    print()

    blend_arg = getattr(args, "blend", None)
    if blend_arg:
        # Interactive voice blending mode
        if not shutil.which("swift-kokoro"):
            print("swift-kokoro not found")
            sys.exit(1)

        voices = [v.strip() for v in blend_arg.split(",")]
        if len(voices) < 2:
            print("Blend requires 2 voices (e.g., --blend am_adam,af_heart)")
            sys.exit(1)

        v1, v2 = voices[0], voices[1]
        name1 = _kokoro_display_name(v1)
        name2 = _kokoro_display_name(v2)

        print(f"Mixing: {name1} ({v1}) + {name2} ({v2})")
        print()

        # Play each voice solo first
        for label, voice in [("Solo: " + name1, v1), ("Solo: " + name2, v2)]:
            print(f"--- {label} ---")
            print(f"  [Enter] Play  [s] Skip")
            key = read_key()
            if key in ("s", "S"):
                print("  Skipped")
            elif use_queue:
                queue_speak(text, voice_kokoro=voice)
            else:
                wav = generate_speech(
                    text, voice_kokoro=voice, output_path=temp_file,
                )
                if wav:
                    proc = play_clip(wav, speed)
                    wait_with_skip(proc)
            print()

        # Sweep blend ratios
        print("========================================")
        print("    Blend Ratios")
        print("========================================")
        print()

        ratios = [(80, 20), (60, 40), (50, 50), (40, 60), (20, 80)]
        for w1, w2 in ratios:
            blend_spec = f"{v1}:{w1},{v2}:{w2}"
            print(f">>> {name1} {w1}% + {name2} {w2}% <<<")
            print("  [Enter] Play  [s] Skip  [q] Quit")
            key = read_key()
            if key in ("s", "S"):
                print("  Skipped")
                continue
            if key in ("q", "Q"):
                break

            if use_queue:
                queue_speak(text, voice_kokoro_blend=blend_spec)
            else:
                wav = generate_speech(
                    text, voice_kokoro_blend=blend_spec, output_path=temp_file,
                )
                if wav:
                    proc = play_clip(wav, speed)
                    wait_with_skip(proc)

            print()
            print("  [Enter] Next  [k] Keep  [r] Replay  [q] Quit")
            while True:
                key = read_key()
                if key in ("\r", "\n", ""):
                    break
                if key in ("k", "K"):
                    print("  Save as persona name (Enter to skip): ", end="", flush=True)
                    fd = sys.stdin.fileno()
                    old = termios.tcgetattr(fd)
                    termios.tcsetattr(fd, termios.TCSADRAIN, old)
                    pname = input()
                    if pname:
                        config = load_raw_config()
                        personas = config.get("personas", {})
                        personas[pname] = {
                            "description": f"Blend: {blend_spec}",
                            "voice_kokoro_blend": blend_spec,
                            "speed": speed,
                            "speed_method": "playback",
                            "max_chars": 10000,
                            "ai_type": "claude",
                        }
                        config["personas"] = personas
                        save_raw_config(config)
                        print(f"  Saved persona: {pname}")
                    break
                if key in ("r", "R"):
                    if use_queue:
                        queue_speak(text, voice_kokoro_blend=blend_spec)
                    else:
                        wav = generate_speech(
                            text, voice_kokoro_blend=blend_spec, output_path=temp_file,
                        )
                        if wav:
                            proc = play_clip(wav, speed)
                            wait_with_skip(proc)
                if key in ("q", "Q"):
                    print()
                    print("Blend auditions complete!")
                    return
            print()

        print("Blend auditions complete!")
        return

    if use_queue and not (getattr(args, "kokoro", False) or blend_arg):
        print("Note: --queue only supported with Kokoro voices (--kokoro or --blend)")
        print("Falling back to direct playback.")
        print()
        use_queue = False

    if getattr(args, "kokoro", False):
        # Kokoro voice auditions
        if not shutil.which("swift-kokoro"):
            print("swift-kokoro not found")
            sys.exit(1)

        try:
            result = subprocess.run(
                ["swift-kokoro", "--list-voices"],
                capture_output=True, text=True, timeout=10,
            )
            voices = [v.strip() for v in result.stdout.strip().split("\n") if v.strip()]
        except (subprocess.TimeoutExpired, OSError):
            print("Failed to list Kokoro voices")
            sys.exit(1)

        if not voices:
            print("No Kokoro voices found")
            sys.exit(1)

        # Apply --filter if specified (comma-separated prefixes)
        kokoro_filter = getattr(args, "filter", None)
        if kokoro_filter:
            prefixes = [p.strip() for p in kokoro_filter.split(",")]
            voices = [v for v in voices if any(v.startswith(p) for p in prefixes)]
            if not voices:
                print(f"No voices matching filter: {kokoro_filter}")
                sys.exit(1)
            print(f"Found {len(voices)} Kokoro voices (filter: {kokoro_filter})")
        else:
            print(f"Found {len(voices)} Kokoro voices")
        print("Press Enter to begin...")
        input()

        for i, voice in enumerate(voices):
            remaining = len(voices) - i
            raw = voice.split("_", 1)[1] if "_" in voice else voice
            display = " ".join(w.capitalize() for w in raw.replace("_", " ").split())
            print()
            print(f">>> {display} ({voice}) [{remaining} remaining] <<<")
            print("  [Enter] Play  [s] Skip  [q] Quit")
            key = read_key()
            if key in ("s", "S"):
                print("  Skipped")
                continue
            if key in ("q", "Q"):
                break

            kokoro_audition_sequence(voice)
            action = prompt_action(voice, _is_kokoro=True)
            if action == "keep":
                print("  Save as persona name (Enter to skip): ", end="", flush=True)
                # Restore normal terminal for line input
                fd = sys.stdin.fileno()
                old = termios.tcgetattr(fd)
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
                name = input()
                if name:
                    save_persona(name, voice, "", True)
            elif action == "replay":
                kokoro_audition_sequence(voice)
            elif action == "quit":
                break

    elif getattr(args, "voice", None):
        # Audition speakers within a model
        voice = args.voice
        voice_path = VOICES_DIR / f"{voice}.onnx"
        if not voice_path.exists():
            print(f"Voice not found: {voice}")
            sys.exit(1)

        voice_json = voice_path.with_suffix(".onnx.json")
        num_speakers = 1
        if voice_json.exists():
            try:
                meta = json.loads(voice_json.read_text())
                num_speakers = meta.get("num_speakers", 1)
            except (json.JSONDecodeError, OSError):
                pass

        range_arg = getattr(args, "range", None)
        if range_arg and "-" in range_arg:
            lo, hi = range_arg.split("-", 1)
            lo_i = max(0, int(lo))
            hi_i = min(num_speakers - 1, int(hi))
            speakers = list(range(lo_i, hi_i + 1))
        else:
            num = getattr(args, "speakers", None) or 10
            speakers = [random.randint(0, num_speakers - 1) for _ in range(num)]

        print(f"Model: {voice} ({num_speakers} speakers)")
        print(f"Auditioning {len(speakers)} speakers...")
        print("Press Enter to begin...")
        input()

        for spk in speakers:
            print()
            print(f">>> Speaker #{spk} <<<")
            if use_queue:
                queue_speak(text, voice_piper=voice, speaker=spk)
            else:
                wav = generate_speech(
                    text,
                    voice_path=voice_path,
                    speed=speed,
                    speed_method=method,
                    speaker=spk,
                    output_path=temp_file,
                )
                if wav:
                    proc = play_clip(wav, speed if method == "playback" else 1.0)
                    wait_with_skip(proc)

            action = prompt_action(voice, str(spk))
            if action == "keep":
                print("  Save as persona name (Enter to skip): ", end="", flush=True)
                name = input()
                if name:
                    save_persona(name, voice, str(spk))
            elif action == "replay":
                if use_queue:
                    queue_speak(text, voice_piper=voice, speaker=spk)
                else:
                    wav = generate_speech(
                        text,
                        voice_path=voice_path,
                        speed=speed,
                        speed_method=method,
                        speaker=spk,
                        output_path=temp_file,
                    )
                    if wav:
                        proc = play_clip(wav, speed if method == "playback" else 1.0)
                        wait_with_skip(proc)
            elif action == "quit":
                break

    else:
        # Audition all installed Piper voices
        if not VOICES_DIR.is_dir():
            print(f"No voices directory: {VOICES_DIR}")
            sys.exit(1)

        voices = sorted(f.stem for f in VOICES_DIR.glob("*.onnx"))
        if not voices:
            print("No voices installed")
            sys.exit(1)

        print(f"Found {len(voices)} voices")
        print("Press Enter to begin...")
        input()

        for voice in voices:
            print()
            print(f">>> {voice} <<<")
            if use_queue:
                queue_speak(text, voice_piper=voice)
            else:
                wav = generate_speech(
                    text,
                    voice_path=VOICES_DIR / f"{voice}.onnx",
                    speed=speed,
                    speed_method=method,
                    output_path=temp_file,
                )
                if wav:
                    proc = play_clip(wav, speed if method == "playback" else 1.0)
                    wait_with_skip(proc)

            action = prompt_action(voice)
            if action == "keep":
                print("  Save as persona name (Enter to skip): ", end="", flush=True)
                name = input()
                if name:
                    save_persona(name, voice)
            elif action == "replay":
                if use_queue:
                    queue_speak(text, voice_piper=voice)
                else:
                    wav = generate_speech(
                        text,
                        voice_path=VOICES_DIR / f"{voice}.onnx",
                        speed=speed,
                        speed_method=method,
                        output_path=temp_file,
                    )
                    if wav:
                        proc = play_clip(wav, speed if method == "playback" else 1.0)
                        wait_with_skip(proc)
            elif action == "quit":
                break

    print()
    print("Auditions complete!")


def cmd_daemon(args: argparse.Namespace) -> None:
    """Manage the TTS daemon."""
    from claude_code_tts.daemon import (
        daemon_restart,
        daemon_status as _daemon_status,
        install_service,
        run_foreground,
        show_logs,
        start_daemon,
        stop_daemon,
        write_control_message,
    )

    dc = getattr(args, "daemon_command", None)
    lockpick = getattr(args, "lockpick", False)

    if dc == "start":
        start_daemon(lockpick=lockpick)
    elif dc == "stop":
        stop_daemon()
    elif dc == "restart":
        daemon_restart(lockpick=lockpick)
    elif dc == "status":
        _daemon_status()
    elif dc == "logs":
        show_logs(follow=getattr(args, "follow", False))
    elif dc == "install":
        install_service()
    elif dc == "foreground":
        run_foreground(lockpick=lockpick)
    elif dc == "control":
        qf = write_control_message(
            text=getattr(args, "text", ""),
            pre_action=getattr(args, "pre_action", None),
            post_action=getattr(args, "post_action", None),
        )
        print(f"Control message written: {qf.name}")
    else:
        print("Usage: claude-tts daemon <start|stop|restart|status|logs|install|foreground>")
        sys.exit(1)


def cmd_release(args: argparse.Namespace) -> None:
    """Create a release."""
    from claude_code_tts.release import main as release_main

    release_args = []
    if args.check:
        release_args.append("--check")
    elif args.bump:
        release_args.append(args.bump)
    release_main(release_args if release_args else None)


def cmd_handy(args: argparse.Namespace) -> None:
    """Manage the Handy voice analyzer."""
    from claude_code_tts.handy import (
        ANALYSIS_DB,
        HANDY_RECORDINGS_DIR,
        HandyWatcher,
        analyze_all_recordings,
        analyze_recording,
        get_recent_analysis,
        summarize_tone,
    )

    subcmd = getattr(args, "handy_command", None)

    if subcmd == "watch":
        watcher = HandyWatcher()
        try:
            watcher.run()
        except KeyboardInterrupt:
            watcher.stop()

    elif subcmd == "analyze":
        target = getattr(args, "path", None)
        if target:
            target_path = Path(target)
            if not target_path.exists():
                print(f"File not found: {target_path}")
                sys.exit(1)
            result = analyze_recording(target_path)
            if result:
                f = result.features
                tone = summarize_tone(f)
                print(f"File:           {result.file_name}")
                print(f"Duration:       {f.duration_seconds}s")
                print(f"Energy:         {f.rms_energy:.4f} ({f.energy_label})")
                print(f"Pitch:          {f.pitch_mean_hz:.1f} Hz (range: {f.pitch_range_hz:.1f} Hz)")
                print(f"Speaking rate:  {f.speaking_rate_wps:.2f} wps ({f.pace_label})")
                print(f"Pauses:         {f.pause_count} ({f.pause_total_seconds:.1f}s, {f.pause_ratio:.1%})")
                print(f"Expressiveness: {f.expressiveness_label}")
                print(f"Tone:           {tone}")
                if result.transcript:
                    print(f"Transcript:     {result.transcript[:120]}...")
            else:
                print("Analysis failed")
                sys.exit(1)
        else:
            # Analyze all unprocessed recordings
            results = analyze_all_recordings()
            if results:
                for r in results:
                    tone = summarize_tone(r.features)
                    print(f"  {r.file_name}: {tone}")
                print(f"\nAnalyzed {len(results)} new recording(s)")
            else:
                print("No new recordings to analyze")

    elif subcmd == "recent":
        max_age = getattr(args, "age", 60.0)
        result = get_recent_analysis(max_age_seconds=max_age)
        if result:
            f = result.features
            tone = summarize_tone(f)
            print(f"Tone: {tone}")
            print(f"Pitch: {f.pitch_mean_hz:.1f} Hz | Energy: {f.energy_label} | "
                  f"Rate: {f.speaking_rate_wps:.1f} wps | Pauses: {f.pause_count}")
            if result.transcript:
                print(f"Said: \"{result.transcript[:120]}...\"")
        else:
            print(f"No analysis within the last {max_age}s")

    elif subcmd == "tone-context":
        # Called by UserPromptSubmit hook to inject tone into Claude's context.
        # Works like a mutating webhook: matches Handy transcripts to segments
        # in the user's message and injects per-segment tone metadata.
        # Falls back to aggregated tone if no message text available.
        from claude_code_tts.voice_context import enrich_message
        from claude_code_tts.handy import get_aggregated_tone
        max_age = getattr(args, "age", 120.0)

        # Try to read the user's message from stdin (hook passes it)
        message = ""
        if not sys.stdin.isatty():
            try:
                import json as _json
                hook_data = _json.load(sys.stdin)
                # UserPromptSubmit hook provides the message in the input
                message = hook_data.get("input", {}).get("message", "")
            except (ValueError, AttributeError):
                pass

        if message:
            result = enrich_message(message, max_age_seconds=max_age)
            if result:
                print(result)
        else:
            # Fallback: no message text, use aggregated tone
            tone = get_aggregated_tone(max_age_seconds=max_age)
            if tone:
                print(f"[Voice context: JMO is {tone}]")

    elif subcmd == "status":
        print(f"Recordings dir: {HANDY_RECORDINGS_DIR}")
        print(f"Analysis DB:    {ANALYSIS_DB}")
        if HANDY_RECORDINGS_DIR.exists():
            wavs = list(HANDY_RECORDINGS_DIR.glob("*.wav"))
            print(f"Recordings:     {len(wavs)}")
        else:
            print("Recordings:     (directory not found)")
        if ANALYSIS_DB.exists():
            import sqlite3
            conn = sqlite3.connect(str(ANALYSIS_DB))
            count = conn.execute("SELECT COUNT(*) FROM voice_analysis").fetchone()[0]
            conn.close()
            print(f"Analyzed:       {count}")
        else:
            print("Analyzed:       (no database yet)")

    else:
        print("Usage: claude-tts handy <watch|analyze|recent|status>")
        print()
        print("Commands:")
        print("  watch    Watch for new recordings and analyze in real-time")
        print("  analyze  Analyze all unprocessed recordings (or a specific file)")
        print("  recent   Show the most recent voice tone analysis")
        print("  status   Show Handy analyzer status")


def _stub(args: argparse.Namespace) -> None:
    """Placeholder for commands not yet implemented."""
    print(f"claude-tts {args.command}: not yet implemented")
    sys.exit(0)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    """Main entry point for the claude-tts CLI."""
    parser = argparse.ArgumentParser(
        prog="claude-tts",
        description="Text-to-speech for Claude Code",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    subparsers = parser.add_subparsers(dest="command")

    # --- status ---
    p = subparsers.add_parser("status", help="Show TTS status for this session")
    p.set_defaults(func=cmd_status)

    # --- mute ---
    p = subparsers.add_parser("mute", help="Mute TTS for this session")
    p.add_argument("--all", action="store_true", help="Mute all sessions globally")
    p.set_defaults(func=cmd_mute)

    # --- unmute ---
    p = subparsers.add_parser("unmute", help="Unmute TTS for this session")
    p.set_defaults(func=cmd_unmute)

    # --- speed ---
    p = subparsers.add_parser("speed", help="Show or set speech speed")
    p.add_argument("value", nargs="?", help="Speed value (0.5-4.0) or 'reset'")
    p.set_defaults(func=cmd_speed)

    # --- persona ---
    p = subparsers.add_parser("persona", help="Show or set voice persona")
    p.add_argument("name", nargs="?", help="Persona name or 'reset'")
    p.add_argument("--project", action="store_true", help="Set as project-level persona")
    p.add_argument("--session", action="store_true", help="Set as session-level persona (default)")
    p.set_defaults(func=cmd_persona)

    # --- mode ---
    p = subparsers.add_parser("mode", help="Show or set TTS mode (direct/queue)")
    p.add_argument("value", nargs="?", choices=["direct", "queue"], help="Mode to set")
    p.set_defaults(func=cmd_mode)

    # --- daemon ---
    p = subparsers.add_parser("daemon", help="Manage the TTS daemon")
    daemon_sub = p.add_subparsers(dest="daemon_command")
    ds = daemon_sub.add_parser("start", help="Start the daemon")
    ds.add_argument("--lockpick", action="store_true", help="Force start even if locked")
    ds.set_defaults(func=cmd_daemon)
    daemon_sub.add_parser("stop", help="Stop the daemon").set_defaults(func=cmd_daemon)
    ds = daemon_sub.add_parser("restart", help="Restart the daemon")
    ds.add_argument("--lockpick", action="store_true", help="Force start even if locked")
    ds.set_defaults(func=cmd_daemon)
    daemon_sub.add_parser("status", help="Show daemon status").set_defaults(func=cmd_daemon)
    ds = daemon_sub.add_parser("logs", help="Show daemon logs")
    ds.add_argument("--follow", "-f", action="store_true", help="Follow log output")
    ds.set_defaults(func=cmd_daemon)
    daemon_sub.add_parser("install", help="Install system service").set_defaults(func=cmd_daemon)
    ds = daemon_sub.add_parser("foreground", help="Run daemon in foreground")
    ds.add_argument("--lockpick", action="store_true", help="Force start even if locked")
    ds.set_defaults(func=cmd_daemon)
    ds = daemon_sub.add_parser("control", help="Send control message to daemon")
    ds.add_argument("--text", "-t", default="", help="Text to speak")
    ds.add_argument("--pre-action", default=None, help="Action before speech")
    ds.add_argument("--post-action", default=None, help="Action after speech")
    ds.set_defaults(func=cmd_daemon)
    p.set_defaults(func=cmd_daemon)

    # --- speak ---
    p = subparsers.add_parser("speak", help="Speak text (standalone or from hook)")
    p.add_argument("text", nargs="?", help="Text to speak")
    p.add_argument("--voice", help="Piper voice model name")
    p.add_argument("--speed", type=float, help="Speech speed")
    p.add_argument("--speaker", type=int, help="Speaker ID for multi-speaker models")
    p.add_argument("--random", action="store_true", help="Random speaker from model")
    p.add_argument("--voice-sherpa", dest="voice_sherpa",
                   help="Sherpa-onnx model id (a directory name under ~/.claude-tts/sherpa-models/)")
    p.add_argument("--speaker-sherpa", dest="speaker_sherpa", type=int, default=-1,
                   help="Speaker ID for multi-speaker sherpa models (-1 = model default)")
    p.add_argument("--from-file", metavar="PATH", help="Read and speak a file (zero context tokens)")
    p.add_argument("--preview", action="store_true", help="Show filtered text without speaking (use with --from-file)")
    p.add_argument("--reader", action="store_true", help="Side-by-side view: original | spoken (use with --from-file)")
    p.add_argument("--from-hook", action="store_true", help="Read hook JSON from stdin")
    p.add_argument("--hook-type", choices=["stop", "post_tool_use"], help="Hook type")
    p.set_defaults(func=cmd_speak)

    # --- audition ---
    p = subparsers.add_parser("audition", help="Audition voices interactively")
    p.add_argument("--voice", help="Specific voice to audition")
    p.add_argument("--speakers", type=int, help="Number of speakers to try")
    p.add_argument("--kokoro", action="store_true", help="Audition Kokoro voices")
    p.add_argument("--blend", help="Blend two Kokoro voices (e.g., am_adam,af_heart)")
    p.add_argument("--filter", help="Filter Kokoro voices by prefix (e.g., am_,bf_)")
    p.add_argument("--text", help="Custom audition text")
    p.add_argument("--range", help="Speaker ID range (e.g., 0-50)")
    p.add_argument("--queue", action="store_true", help="Play through daemon queue")
    p.add_argument("--speed", type=float, help="Audition speed")
    p.set_defaults(func=cmd_audition)

    # --- cleanup ---
    p = subparsers.add_parser("cleanup", help="Remove stale session entries")
    p.add_argument("--dry-run", action="store_true", help="Show what would be removed")
    p.set_defaults(func=cmd_cleanup)

    # --- sounds ---
    p = subparsers.add_parser("sounds", help="Toggle or configure notification sounds")
    p.add_argument("action", nargs="?", choices=["on", "off", "test"], help="Action")
    p.set_defaults(func=cmd_sounds)

    # --- intermediate ---
    p = subparsers.add_parser("intermediate", help="Toggle intermediate speech")
    p.add_argument("action", nargs="?", choices=["on", "off"], help="on or off")
    p.set_defaults(func=cmd_intermediate)

    # --- discover ---
    p = subparsers.add_parser("discover", help="Auto-suggest persona based on repo context")
    p.set_defaults(func=cmd_discover)

    # --- sherpa ---
    p = subparsers.add_parser("sherpa", help="Manage sherpa-onnx TTS backend models")
    sherpa_sub = p.add_subparsers(dest="sherpa_command")
    sherpa_sub.add_parser("list", help="List installed sherpa models").set_defaults(func=cmd_sherpa)
    p.set_defaults(func=cmd_sherpa)

    # --- random ---
    p = subparsers.add_parser("random", help="Generate a random TTS persona")
    p.add_argument("--preview", action="store_true", help="Preview without applying")
    p.set_defaults(func=cmd_random)

    # --- test ---
    p = subparsers.add_parser("test", help="Test TTS with sample text")
    p.add_argument("--quick", action="store_true", help="Short test")
    p.add_argument("--table", action="store_true", help="Table reading test")
    p.set_defaults(func=cmd_test)

    # --- pause ---
    p = subparsers.add_parser("pause", help="Toggle pause/resume")
    p.set_defaults(func=cmd_pause)

    # --- install ---
    p = subparsers.add_parser("install", help="Install/upgrade Claude Code TTS")
    p.add_argument("--upgrade", action="store_true", help="Upgrade existing installation")
    p.add_argument("--uninstall", action="store_true", help="Remove installation")
    p.add_argument("--check", action="store_true", help="Check for updates")
    p.add_argument("--dry-run", action="store_true", help="Show what would be done")
    p.set_defaults(func=_stub)

    # --- handy ---
    p = subparsers.add_parser("handy", help="Handy voice analyzer")
    handy_sub = p.add_subparsers(dest="handy_command")
    handy_sub.add_parser("watch", help="Watch for new recordings").set_defaults(func=cmd_handy)
    hs = handy_sub.add_parser("analyze", help="Analyze recordings")
    hs.add_argument("path", nargs="?", help="Specific WAV file to analyze")
    hs.set_defaults(func=cmd_handy)
    hs = handy_sub.add_parser("recent", help="Show most recent tone analysis")
    hs.add_argument("--age", type=float, default=60.0, help="Max age in seconds (default: 60)")
    hs.set_defaults(func=cmd_handy)
    hs = handy_sub.add_parser("tone-context", help="Output voice tone for hook injection")
    hs.add_argument("--age", type=float, default=30.0, help="Max age in seconds (default: 30)")
    hs.set_defaults(func=cmd_handy)
    handy_sub.add_parser("status", help="Show analyzer status").set_defaults(func=cmd_handy)
    p.set_defaults(func=cmd_handy)

    # --- release ---
    p = subparsers.add_parser("release", help="Create a release")
    p.add_argument("bump", nargs="?", choices=["patch", "minor", "major"], help="Version bump type")
    p.add_argument("--check", action="store_true", help="Verify without releasing")
    p.set_defaults(func=cmd_release)

    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        sys.exit(0)

    if hasattr(args, "func"):
        args.func(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
