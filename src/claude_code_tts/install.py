#!/usr/bin/env python3
"""
install.py - Install/Uninstall Claude Code TTS

Supports:
    - macOS (via Homebrew)
    - Linux (via apt/dnf)
    - WSL 2 on Windows (via apt, audio through WSLg)

Usage:
    python install.py              Install TTS
    python install.py --dry-run    Show what would be done
    python install.py --uninstall  Remove TTS
    python install.py --help       Show help
"""

import argparse
import hashlib
import json
import os
import platform
import random
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

# Version of this installer/package
__version__ = "6.0.1"


# --- Platform Detection ---

def detect_platform() -> str:
    """Detect the current platform: macos, linux, or wsl."""
    system = platform.system()
    if system == "Darwin":
        return "macos"
    elif system == "Linux":
        # Check for WSL
        try:
            with open("/proc/version", "r") as f:
                if "microsoft" in f.read().lower():
                    return "wsl"
        except (FileNotFoundError, PermissionError):
            pass
        return "linux"
    else:
        return "unsupported"


def detect_package_manager() -> Optional[str]:
    """Detect available package manager."""
    if shutil.which("brew"):
        return "brew"
    elif shutil.which("apt"):
        return "apt"
    elif shutil.which("dnf"):
        return "dnf"
    elif shutil.which("pacman"):
        return "pacman"
    return None


PLATFORM = detect_platform()
PKG_MANAGER = detect_package_manager()

# --- Configuration ---

HOME = Path.home()
CLAUDE_DIR = HOME / ".claude"
HOOKS_DIR = CLAUDE_DIR / "hooks"
COMMANDS_DIR = CLAUDE_DIR / "commands"
VOICES_DIR = HOME / ".local" / "share" / "piper-voices"
SETTINGS_FILE = CLAUDE_DIR / "settings.json"

VOICE_MODEL = "en_US-hfc_male-medium"
VOICE_URL = f"https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/hfc_male/medium/{VOICE_MODEL}.onnx"
VOICE_JSON_URL = f"https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/hfc_male/medium/{VOICE_MODEL}.onnx.json"
VOICE_FILE = VOICES_DIR / f"{VOICE_MODEL}.onnx"
VOICE_JSON = VOICES_DIR / f"{VOICE_MODEL}.onnx.json"

# Backup directory
BACKUP_DIR = HOME / ".claude-tts-backups"

# Get repo directory - handles both package install and direct script execution
SCRIPT_DIR = Path(__file__).parent.resolve()

def _find_repo_dir() -> Path:
    """Find the repository root directory containing hooks/ and commands/."""
    # Check common locations
    candidates = [
        SCRIPT_DIR.parent,          # scripts/install.py -> repo
        SCRIPT_DIR.parent.parent,   # src/claude_code_tts/install.py -> repo
        SCRIPT_DIR.parent.parent.parent,  # deeper package structures
        Path.cwd(),                  # Current working directory
    ]

    for candidate in candidates:
        if (candidate / "hooks").is_dir() and (candidate / "commands").is_dir():
            return candidate

    # Fallback: assume parent of script dir
    return SCRIPT_DIR.parent

REPO_DIR = _find_repo_dir()

TTS_CONFIG_DIR = HOME / ".claude-tts"
TTS_CONFIG_FILE = TTS_CONFIG_DIR / "config.json"

# Single source of truth for all installable files.
# Adding a new file = one edit to this dict.
MANIFEST: dict[str, list[str]] = {
    "hooks": ["speak-response.sh", "speak-intermediate.sh", "play-sound.sh"],
    "commands": ["tts-mute.md", "tts-unmute.md", "tts-speed.md", "tts-sounds.md", "tts-mode.md", "tts-persona.md", "tts-status.md", "tts-cleanup.md", "tts-random.md", "tts-test.md", "tts-discover.md", "tts-intermediate.md"],
    "scripts": ["tts-daemon.py", "tts-mode.sh", "tts-mute.sh", "tts-unmute.sh", "tts-status.sh", "tts-speed.sh", "tts-persona.sh", "tts-cleanup.sh", "tts-random.sh", "tts-test.sh", "tts-speak.sh", "tts-audition.sh", "tts-builder.sh", "tts-builder.py", "tts-discover.sh", "tts-pause.sh", "tts-lib.sh", "tts-filter.py", "tts-sounds.sh", "tts-intermediate.sh"],
}


def _manifest_dirs(category: str) -> tuple[str, Path]:
    """Return (repo_subdir, install_dir) for a manifest category."""
    return {
        "hooks": ("hooks", HOOKS_DIR),
        "commands": ("commands", COMMANDS_DIR),
        "scripts": ("scripts", TTS_CONFIG_DIR),
    }[category]


def _manifest_entries():
    """Yield (name, src_path, dst_path) for all manifest files."""
    for category, files in MANIFEST.items():
        repo_subdir, install_dir = _manifest_dirs(category)
        for name in files:
            yield name, REPO_DIR / repo_subdir / name, install_dir / name


# Hugging Face Piper voices base URL
HF_VOICES_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0"

# Available voice models (curated list of quality English voices)
# Format: (name, gender, quality, description, path_segment)
AVAILABLE_VOICES = [
    # Female voices
    ("en_US-amy-medium", "female", "medium", "Amy - Clear American female", "en/en_US/amy/medium"),
    ("en_US-amy-low", "female", "low", "Amy - Faster, lower quality", "en/en_US/amy/low"),
    ("en_US-hfc_female-medium", "female", "medium", "HFC Female - Natural female voice", "en/en_US/hfc_female/medium"),
    ("en_US-kristin-medium", "female", "medium", "Kristin - Warm American female", "en/en_US/kristin/medium"),
    ("en_US-ljspeech-high", "female", "high", "LJSpeech - High quality female", "en/en_US/ljspeech/high"),
    ("en_US-ljspeech-medium", "female", "medium", "LJSpeech - Medium quality female", "en/en_US/ljspeech/medium"),
    # Male voices - US
    ("en_US-hfc_male-medium", "male", "medium", "HFC Male - Natural male voice (default)", "en/en_US/hfc_male/medium"),
    ("en_US-joe-medium", "male", "medium", "Joe - Deep American male", "en/en_US/joe/medium"),
    ("en_US-lessac-medium", "male", "medium", "Lessac - Professional male narrator", "en/en_US/lessac/medium"),
    ("en_US-lessac-high", "male", "high", "Lessac - High quality male narrator", "en/en_US/lessac/high"),
    ("en_US-ryan-medium", "male", "medium", "Ryan - Friendly American male", "en/en_US/ryan/medium"),
    ("en_US-ryan-high", "male", "high", "Ryan - High quality American male", "en/en_US/ryan/high"),
    # British voices
    ("en_GB-alan-medium", "male", "medium", "Alan - British male", "en/en_GB/alan/medium"),
    ("en_GB-northern_english_male-medium", "male", "medium", "Northern English Male - Yorkshire accent", "en/en_GB/northern_english_male/medium"),
    ("en_GB-alba-medium", "female", "medium", "Alba - Scottish female", "en/en_GB/alba/medium"),
    ("en_GB-jenny_dioco-medium", "female", "medium", "Jenny - British female", "en/en_GB/jenny_dioco/medium"),
]


# --- Colors ---

class Colors:
    RED = "\033[0;31m"
    GREEN = "\033[0;32m"
    YELLOW = "\033[1;33m"
    BLUE = "\033[0;34m"
    CYAN = "\033[0;36m"
    MAGENTA = "\033[0;35m"
    NC = "\033[0m"


def info(msg: str) -> None:
    print(f"{Colors.BLUE}[INFO]{Colors.NC} {msg}")


def success(msg: str) -> None:
    print(f"{Colors.GREEN}[OK]{Colors.NC} {msg}")


def warn(msg: str) -> None:
    print(f"{Colors.YELLOW}[WARN]{Colors.NC} {msg}")


def error(msg: str) -> None:
    print(f"{Colors.RED}[ERROR]{Colors.NC} {msg}")


def dry(msg: str) -> None:
    print(f"{Colors.CYAN}[DRY-RUN]{Colors.NC} Would: {msg}")


def preflight(msg: str) -> None:
    print(f"{Colors.MAGENTA}[PREFLIGHT]{Colors.NC} {msg}")


def die(msg: str) -> None:
    error(msg)
    sys.exit(1)


# --- Helpers ---

def command_exists(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def run_cmd(
    cmd: list[str], check: bool = True, capture: bool = False
) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, capture_output=capture, text=True)


def download_file(url: str, dest: Path) -> None:
    info(f"Downloading {dest.name}...")
    try:
        run_cmd(["curl", "-L", "--progress-bar", "-o", str(dest), url])
    except subprocess.CalledProcessError:
        die(f"Failed to download {url}")


# --- Backup Manager ---

class BackupManager:
    """Manages backups of files before modification."""

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self.timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.backup_path: Optional[Path] = None
        self.backed_up_files: list[tuple[Path, Path]] = []

    def create_backup_dir(self) -> Path:
        """Create timestamped backup directory."""
        self.backup_path = BACKUP_DIR / f"backup_{self.timestamp}"
        if not self.dry_run:
            self.backup_path.mkdir(parents=True, exist_ok=True)
        return self.backup_path

    def backup_file(self, file_path: Path) -> Optional[Path]:
        """Backup a single file if it exists."""
        if not file_path.exists():
            return None

        if self.backup_path is None:
            self.create_backup_dir()

        # Preserve directory structure in backup
        relative = file_path.relative_to(HOME)
        backup_dest = self.backup_path / relative

        if self.dry_run:
            dry(f"Backup {file_path} -> {backup_dest}")
        else:
            backup_dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file_path, backup_dest)
            self.backed_up_files.append((file_path, backup_dest))

        return backup_dest

    def print_restore_instructions(self) -> None:
        """Print instructions for restoring from backup."""
        if not self.backed_up_files and self.backup_path is None:
            return

        print()
        print(f"{Colors.YELLOW}--- BACKUP INFORMATION ---{Colors.NC}")
        if self.backup_path:
            print(f"Backup location: {self.backup_path}")
            print()
            print("To restore from backup, run:")
            for original, backup in self.backed_up_files:
                print(f"  cp {backup} {original}")
            print()
            print(f"Or restore everything:")
            print(f"  cp -r {self.backup_path}/.claude/* ~/.claude/")
        print(f"{Colors.YELLOW}--------------------------{Colors.NC}")


# --- Pre-flight Checks ---

def run_preflight_checks(dry_run: bool = False) -> tuple[bool, list[str]]:
    """
    Run all pre-flight checks BEFORE making any changes.
    Returns (success, list_of_issues).
    """
    issues: list[str] = []

    print()
    print(f"{Colors.MAGENTA}========================================{Colors.NC}")
    print(f"{Colors.MAGENTA}  Pre-flight Checks{Colors.NC}")
    print(f"{Colors.MAGENTA}========================================{Colors.NC}")
    print()

    # Check platform
    if PLATFORM == "unsupported":
        issues.append(f"Unsupported platform: {platform.system()}")
        preflight(f"{Colors.RED}FAIL{Colors.NC} Unsupported platform")
    elif PLATFORM == "macos":
        preflight(f"{Colors.GREEN}PASS{Colors.NC} macOS detected")
    elif PLATFORM == "wsl":
        preflight(f"{Colors.GREEN}PASS{Colors.NC} WSL 2 detected (Windows Subsystem for Linux)")
    elif PLATFORM == "linux":
        preflight(f"{Colors.GREEN}PASS{Colors.NC} Linux detected")

    # Check package manager
    if PKG_MANAGER is None:
        issues.append("No supported package manager found (need brew, apt, dnf, or pacman)")
        preflight(f"{Colors.RED}FAIL{Colors.NC} No package manager found")
    else:
        preflight(f"{Colors.GREEN}PASS{Colors.NC} Package manager: {PKG_MANAGER}")

    # Check curl (needed for downloads)
    if not command_exists("curl"):
        issues.append("curl is not installed")
        preflight(f"{Colors.RED}FAIL{Colors.NC} curl not found")
    else:
        preflight(f"{Colors.GREEN}PASS{Colors.NC} curl found")

    # Check source files exist
    for name, src_path, dst_path in _manifest_entries():
        if not src_path.exists():
            issues.append(f"Source file not found: {src_path}")
            preflight(f"{Colors.RED}FAIL{Colors.NC} Source {name} missing")
        else:
            preflight(f"{Colors.GREEN}PASS{Colors.NC} Source {name} found")

    # Check write permissions for target directories
    test_dirs = [HOME / ".claude", HOME / ".local" / "share"]
    for test_dir in test_dirs:
        parent = test_dir
        while not parent.exists():
            parent = parent.parent
        if not os.access(parent, os.W_OK):
            issues.append(f"No write permission for {parent}")
            preflight(f"{Colors.RED}FAIL{Colors.NC} Cannot write to {parent}")
        else:
            preflight(f"{Colors.GREEN}PASS{Colors.NC} Can write to {parent}")

    # Check if settings.json is valid JSON (if it exists)
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE) as f:
                json.load(f)
            preflight(f"{Colors.GREEN}PASS{Colors.NC} settings.json is valid JSON")
        except json.JSONDecodeError as e:
            issues.append(f"settings.json is invalid JSON: {e}")
            preflight(f"{Colors.RED}FAIL{Colors.NC} settings.json is corrupt")

    # WSL-specific checks
    if PLATFORM == "wsl":
        # Check for WSLg PulseAudio socket
        wslg_pulse = Path("/mnt/wslg/PulseServer")
        if wslg_pulse.exists():
            preflight(f"{Colors.GREEN}PASS{Colors.NC} WSLg PulseAudio socket found")
        else:
            preflight(f"{Colors.YELLOW}WARN{Colors.NC} WSLg PulseAudio not found - audio may not work")
            preflight(f"{Colors.YELLOW}     {Colors.NC} Make sure you're on Windows 11 with WSLg enabled")

    # Check required: uv (for consistent Python version management)
    if not command_exists("uv"):
        preflight(f"{Colors.RED}FAIL{Colors.NC} uv not found (required for Python version management)")
        preflight(f"{Colors.RED}     {Colors.NC} Install with: curl -LsSf https://astral.sh/uv/install.sh | sh")
        issues.append("uv not found")
    else:
        preflight(f"{Colors.GREEN}PASS{Colors.NC} uv found")

    # Check optional dependencies (warnings only)
    if not command_exists("jq"):
        preflight(f"{Colors.YELLOW}INFO{Colors.NC} jq not found (will be installed)")
    if not command_exists("pipx"):
        preflight(f"{Colors.YELLOW}INFO{Colors.NC} pipx not found (will be installed)")
    if not command_exists("piper"):
        preflight(f"{Colors.YELLOW}INFO{Colors.NC} piper not found (will be installed)")

    # Check for audio player
    if PLATFORM == "macos":
        if command_exists("afplay"):
            preflight(f"{Colors.GREEN}PASS{Colors.NC} afplay found (audio player)")
    else:
        if command_exists("paplay"):
            preflight(f"{Colors.GREEN}PASS{Colors.NC} paplay found (PulseAudio player)")
        elif command_exists("aplay"):
            preflight(f"{Colors.YELLOW}INFO{Colors.NC} aplay found (ALSA player, paplay preferred)")
        else:
            preflight(f"{Colors.YELLOW}INFO{Colors.NC} No audio player found (will install pulseaudio-utils)")

    print()

    if issues:
        print(f"{Colors.RED}Pre-flight checks FAILED:{Colors.NC}")
        for issue in issues:
            print(f"  - {issue}")
        return False, issues
    else:
        print(f"{Colors.GREEN}All pre-flight checks passed!{Colors.NC}")
        return True, []


# --- Uninstall ---

def do_uninstall(dry_run: bool = False) -> None:
    print()
    print("========================================")
    print("  Claude Code TTS Uninstaller")
    print("========================================")
    print()

    if dry_run:
        info("DRY RUN MODE - No changes will be made")
        print()

    backup = BackupManager(dry_run=dry_run)

    # Backup settings.json before modifying
    if SETTINGS_FILE.exists():
        backup.backup_file(SETTINGS_FILE)

    # Remove hook scripts and slash commands
    for category in ("hooks", "commands"):
        _subdir, install_dir = _manifest_dirs(category)
        for name in MANIFEST[category]:
            f = install_dir / name
            if f.exists():
                if dry_run:
                    dry(f"rm {f}")
                else:
                    f.unlink()
                success(f"Removed {category[:-1]}: {f}")

    # Clean up old command names (v1.x -> v2.x, v3.x -> v4.x migration)
    for old_cmd in ["mute.md", "unmute.md", "speed.md", "sounds.md", "persona.md"]:
        cmd_file = COMMANDS_DIR / old_cmd
        if cmd_file.exists():
            if dry_run:
                dry(f"rm {cmd_file}")
            else:
                cmd_file.unlink()
            success(f"Removed command: /{old_cmd.replace('.md', '')}")

    # Remove mute file
    mute_file = Path("/tmp/claude_tts_muted")
    if mute_file.exists():
        if dry_run:
            dry(f"rm {mute_file}")
        else:
            mute_file.unlink()
        success("Removed mute file")

    # Remove hook from settings.json
    if SETTINGS_FILE.exists():
        with open(SETTINGS_FILE) as f:
            settings = json.load(f)

        stop_hooks = settings.get("hooks", {}).get("Stop", [])
        original_count = len(stop_hooks)

        # Filter out TTS hook
        stop_hooks = [
            h
            for h in stop_hooks
            if not any(
                "speak-response.sh" in hook.get("command", "")
                for hook in h.get("hooks", [])
            )
        ]

        if len(stop_hooks) < original_count:
            if dry_run:
                dry("Remove speak-response.sh entry from settings.json")
            else:
                settings["hooks"]["Stop"] = stop_hooks
                with open(SETTINGS_FILE, "w") as f:
                    json.dump(settings, f, indent=2)
            success("Removed hook from settings.json")
        else:
            info("No TTS hook found in settings.json")

    # Inform about voice model and Piper
    print()
    if VOICE_FILE.exists():
        warn(f"Voice model kept at: {VOICE_FILE}")
        warn("To remove voice model (~60MB), run:")
        print(f"  rm -rf {VOICES_DIR}")

    if command_exists("piper"):
        print()
        warn("Piper TTS is still installed.")
        warn("To remove it, run: pipx uninstall piper-tts")

    if not dry_run:
        backup.print_restore_instructions()

    print()
    print("========================================")
    if dry_run:
        print(f"{Colors.CYAN}  Dry Run Complete - No changes made{Colors.NC}")
    else:
        print(f"{Colors.GREEN}  Uninstall Complete{Colors.NC}")
    print("========================================")
    print()


# --- Package Installation Helpers ---

def install_package(package: str, dry_run: bool = False) -> None:
    """Install a package using the detected package manager."""
    if dry_run:
        dry(f"{PKG_MANAGER} install {package}")
        return

    info(f"Installing {package}...")

    if PKG_MANAGER == "brew":
        run_cmd(["brew", "install", package])
    elif PKG_MANAGER == "apt":
        # Check if we need sudo
        if os.geteuid() != 0:
            run_cmd(["sudo", "apt", "install", "-y", package])
        else:
            run_cmd(["apt", "install", "-y", package])
    elif PKG_MANAGER == "dnf":
        if os.geteuid() != 0:
            run_cmd(["sudo", "dnf", "install", "-y", package])
        else:
            run_cmd(["dnf", "install", "-y", package])
    elif PKG_MANAGER == "pacman":
        if os.geteuid() != 0:
            run_cmd(["sudo", "pacman", "-S", "--noconfirm", package])
        else:
            run_cmd(["pacman", "-S", "--noconfirm", package])


def install_pipx(dry_run: bool = False) -> None:
    """Install pipx using the appropriate method for the platform."""
    if command_exists("pipx"):
        return

    if dry_run:
        if PKG_MANAGER == "brew":
            dry("brew install pipx && pipx ensurepath")
        elif PKG_MANAGER == "apt":
            dry("apt install pipx && pipx ensurepath")
        else:
            dry(f"{PKG_MANAGER} install pipx && pipx ensurepath")
        return

    info("Installing pipx (for Piper installation)...")

    if PKG_MANAGER == "brew":
        run_cmd(["brew", "install", "pipx"])
    elif PKG_MANAGER == "apt":
        if os.geteuid() != 0:
            run_cmd(["sudo", "apt", "install", "-y", "pipx"])
        else:
            run_cmd(["apt", "install", "-y", "pipx"])
    elif PKG_MANAGER == "dnf":
        if os.geteuid() != 0:
            run_cmd(["sudo", "dnf", "install", "-y", "pipx"])
        else:
            run_cmd(["dnf", "install", "-y", "pipx"])
    elif PKG_MANAGER == "pacman":
        if os.geteuid() != 0:
            run_cmd(["sudo", "pacman", "-S", "--noconfirm", "python-pipx"])
        else:
            run_cmd(["pacman", "-S", "--noconfirm", "python-pipx"])

    # Ensure pipx is in PATH
    run_cmd(["pipx", "ensurepath"], check=False)


# --- Install ---

def do_install(dry_run: bool = False, upgrade: bool = False) -> None:
    platform_name = {"macos": "macOS", "linux": "Linux", "wsl": "WSL 2"}.get(PLATFORM, PLATFORM)

    print()
    print("========================================")
    if upgrade:
        print(f"  Claude Code TTS Upgrader ({platform_name})")
    else:
        print(f"  Claude Code TTS Installer ({platform_name})")
    print("========================================")
    print()

    if dry_run:
        info("DRY RUN MODE - No changes will be made")
        print()

    # --- Pre-flight checks (BEFORE touching anything) ---

    passed, issues = run_preflight_checks(dry_run=dry_run)
    if not passed:
        print()
        die("Pre-flight checks failed. Please fix the issues above and try again.")

    # --- Create backup manager ---

    backup = BackupManager(dry_run=dry_run)

    # Backup existing files BEFORE any modifications
    print()
    info("Creating backups of existing files...")

    files_to_backup = [SETTINGS_FILE]
    for _name, _src, dst_path in _manifest_entries():
        files_to_backup.append(dst_path)

    backed_up_count = 0
    for f in files_to_backup:
        if f.exists():
            backup.backup_file(f)
            backed_up_count += 1

    if backed_up_count > 0:
        success(f"Backed up {backed_up_count} existing file(s)")
    else:
        info("No existing files to backup (fresh install)")

    # --- Install dependencies ---

    print()
    info("Checking dependencies...")

    # Install jq
    if not command_exists("jq"):
        install_package("jq", dry_run=dry_run)
    success(
        f"jq {'will be installed' if dry_run and not command_exists('jq') else 'ready'}"
    )

    # Install audio player on Linux/WSL
    if PLATFORM in ("linux", "wsl") and not command_exists("paplay"):
        pkg_name = "pulseaudio-utils" if PKG_MANAGER in ("apt", "dnf") else "pulseaudio"
        install_package(pkg_name, dry_run=dry_run)
    if PLATFORM in ("linux", "wsl"):
        success(
            f"paplay {'will be installed' if dry_run and not command_exists('paplay') else 'ready'}"
        )

    # Install pipx
    install_pipx(dry_run=dry_run)
    success(
        f"pipx {'will be installed' if dry_run and not command_exists('pipx') else 'ready'}"
    )

    # --- Install Piper TTS ---

    print()
    if not command_exists("piper"):
        if dry_run:
            dry("pipx install piper-tts")
        else:
            info("Installing Piper TTS via pipx...")
            run_cmd(["pipx", "install", "piper-tts"])
            # Update PATH for this session
            local_bin = HOME / ".local" / "bin"
            os.environ["PATH"] = f"{local_bin}:{os.environ['PATH']}"
            if not command_exists("piper"):
                die("Piper installation failed. Try: pipx install piper-tts")
    success(
        f"Piper TTS {'will be installed' if dry_run and not command_exists('piper') else 'ready'}"
    )

    # --- Download voice model ---

    print()
    if not VOICE_FILE.exists():
        if dry_run:
            dry(f"Download voice model: {VOICE_MODEL} (~60MB)")
            dry(f"  -> {VOICE_FILE}")
        else:
            VOICES_DIR.mkdir(parents=True, exist_ok=True)
            download_file(VOICE_URL, VOICE_FILE)
            download_file(VOICE_JSON_URL, VOICE_JSON)
        success(f"Voice model {'will be downloaded' if dry_run else 'downloaded'}")
    else:
        success("Voice model already installed")

    # --- Set up directories ---

    print()
    if dry_run:
        dry(f"mkdir -p {HOOKS_DIR}")
        dry(f"mkdir -p {COMMANDS_DIR}")
    else:
        HOOKS_DIR.mkdir(parents=True, exist_ok=True)
        COMMANDS_DIR.mkdir(parents=True, exist_ok=True)
    success("Directories ready")

    # --- Install hook scripts ---

    print()
    for category, files in MANIFEST.items():
        repo_subdir, install_dir = _manifest_dirs(category)
        install_dir.mkdir(parents=True, exist_ok=True)
        for name in files:
            src = REPO_DIR / repo_subdir / name
            dst = install_dir / name
            if src.exists():
                if dry_run:
                    dry(f"cp {src} -> {dst}")
                else:
                    shutil.copy(src, dst)
                    dst.chmod(0o755)
        success(f"Installed {category}: {len(files)} files")

    # Clean up old command names (v1.x -> v2.x, v3.x -> v4.x migration)
    for old_cmd in ["mute.md", "unmute.md", "speed.md", "sounds.md", "persona.md"]:
        old_file = COMMANDS_DIR / old_cmd
        if old_file.exists():
            if not dry_run:
                old_file.unlink()
            info(f"Removed old command: /{old_cmd.replace('.md', '')}")

    # --- Restart daemon if running (picks up new code) ---
    daemon_pid_file = TTS_CONFIG_DIR / "daemon.pid"
    if daemon_pid_file.exists():
        try:
            pid = int(daemon_pid_file.read_text().strip())
            os.kill(pid, 0)  # Check if running
            if dry_run:
                dry("Restart TTS daemon to pick up new code")
            else:
                # Check if daemon supports control messages
                version_file = TTS_CONFIG_DIR / "daemon.version"
                supports_control = (version_file.exists() and
                                    "control" in version_file.read_text())

                if supports_control:
                    # Queue-based restart: drop a control message, daemon
                    # processes it in order (no overlap with active speech)
                    import secrets as _secrets
                    queue_dir = TTS_CONFIG_DIR / "queue"
                    queue_dir.mkdir(parents=True, exist_ok=True)
                    msg = {
                        "id": _secrets.token_hex(8),
                        "timestamp": time.time(),
                        "type": "control",
                        "session_id": "installer",
                        "text": "Installing updates. Back in a moment.",
                        "pre_action": "drain",
                        "post_action": "restart",
                        "persona": "claude-prime",
                    }
                    queue_file = queue_dir / f"{msg['timestamp']}_{msg['id']}.json"
                    tmp_file = queue_file.with_suffix(".tmp")
                    tmp_file.write_text(json.dumps(msg))
                    tmp_file.rename(queue_file)
                    success("Daemon restart queued (will restart after current speech)")
                else:
                    # Legacy fallback: SIGTERM + wait + start
                    info("Restarting TTS daemon to pick up new code...")
                    os.kill(pid, signal.SIGTERM)
                    for _ in range(100):
                        try:
                            os.kill(pid, 0)
                            time.sleep(0.1)
                        except ProcessLookupError:
                            break
                    daemon_script = TTS_CONFIG_DIR / "tts-daemon.py"
                    if daemon_script.exists():
                        subprocess.Popen(
                            ["uv", "run", "--python", "3.12", str(daemon_script), "start"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL
                        )
                        time.sleep(0.5)
                    success("Daemon restarted")
        except (ValueError, ProcessLookupError, PermissionError):
            pass  # Daemon not running, nothing to restart

    # --- Install service files ---

    services_dir = TTS_CONFIG_DIR / "services"
    if dry_run:
        dry(f"mkdir -p {services_dir}")
    else:
        services_dir.mkdir(parents=True, exist_ok=True)

    # macOS launchd plist
    src_plist = REPO_DIR / "services" / "com.claude-tts.daemon.plist"
    dst_plist = services_dir / "com.claude-tts.daemon.plist"
    if src_plist.exists():
        if dry_run:
            dry(f"cp {src_plist} -> {dst_plist}")
        else:
            shutil.copy(src_plist, dst_plist)

    # Linux systemd service
    src_systemd = REPO_DIR / "services" / "claude-tts.service"
    dst_systemd = services_dir / "claude-tts.service"
    if src_systemd.exists():
        if dry_run:
            dry(f"cp {src_systemd} -> {dst_systemd}")
        else:
            shutil.copy(src_systemd, dst_systemd)

    if src_plist.exists() or src_systemd.exists():
        success("Services: launchd plist, systemd unit")

    # --- Configure settings.json ---

    print()
    stop_hook_entry = {
        "matcher": "*",
        "hooks": [
            {
                "type": "command",
                "command": str(HOOKS_DIR / "speak-response.sh"),
                "timeout": 180
            }
        ]
    }
    post_tool_hook_entry = {
        "matcher": "*",
        "hooks": [
            {
                "type": "command",
                "command": str(HOOKS_DIR / "speak-intermediate.sh"),
                "timeout": 30
            }
        ]
    }

    def _ensure_tts_hooks(settings: dict) -> bool:
        """Ensure both Stop and PostToolUse TTS hooks are registered. Returns True if changes were made."""
        changed = False
        if "hooks" not in settings:
            settings["hooks"] = {}

        # Ensure Stop hook
        if "Stop" not in settings["hooks"]:
            settings["hooks"]["Stop"] = []
        has_stop = any("speak-response.sh" in h.get("hooks", [{}])[0].get("command", "") for h in settings["hooks"]["Stop"] if h.get("hooks"))
        if not has_stop:
            settings["hooks"]["Stop"].append(stop_hook_entry)
            changed = True

        # Ensure PostToolUse hook
        if "PostToolUse" not in settings["hooks"]:
            settings["hooks"]["PostToolUse"] = []
        has_post = any("speak-intermediate.sh" in h.get("hooks", [{}])[0].get("command", "") for h in settings["hooks"]["PostToolUse"] if h.get("hooks"))
        if not has_post:
            settings["hooks"]["PostToolUse"].append(post_tool_hook_entry)
            changed = True

        return changed

    if upgrade:
        # In upgrade mode, check if PostToolUse hook needs to be added
        if SETTINGS_FILE.exists():
            with open(SETTINGS_FILE) as f:
                settings = json.load(f)
            if _ensure_tts_hooks(settings):
                with open(SETTINGS_FILE, "w") as f:
                    json.dump(settings, f, indent=2)
                success("Settings updated (added PostToolUse intermediate speech hook)")
            else:
                info("Upgrade mode: keeping existing settings.json configuration")
                success("Settings preserved")
        else:
            info("Upgrade mode: keeping existing settings.json configuration")
            success("Settings preserved")
    elif SETTINGS_FILE.exists():
        info("Configuring Claude Code settings...")
        if dry_run:
            dry("Add TTS hooks to settings.json (preserving existing hooks)")
        else:
            with open(SETTINGS_FILE) as f:
                settings = json.load(f)
            if _ensure_tts_hooks(settings):
                with open(SETTINGS_FILE, "w") as f:
                    json.dump(settings, f, indent=2)
                success("TTS hooks added to settings.json (preserving existing hooks)")
            else:
                success("TTS hooks already configured in settings.json")
    else:
        info("Configuring Claude Code settings...")
        if dry_run:
            dry(f"Create {SETTINGS_FILE} with hook configuration")
        else:
            settings = {}
            _ensure_tts_hooks(settings)
            SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(SETTINGS_FILE, "w") as f:
                json.dump(settings, f, indent=2)
            success("Created settings.json with hook configuration")

    # --- Verify installation ---

    print()
    if not dry_run:
        info("Verifying installation...")

        if command_exists("piper") and VOICE_FILE.exists():
            test_file = Path("/tmp/claude_tts_test.wav")
            try:
                # Generate test audio with 2x speed (length_scale=0.5)
                subprocess.run(
                    ["piper", "--model", str(VOICE_FILE), "--length_scale", "0.5",
                     "--output_file", str(test_file)],
                    input="Hello, Claude Code TTS is now installed.",
                    text=True,
                    capture_output=True,
                )
                if test_file.exists():
                    success("Piper TTS is working")
                    info("Playing test audio...")

                    # Use platform-appropriate player
                    if PLATFORM == "macos" and command_exists("afplay"):
                        subprocess.run(["afplay", str(test_file)], check=False)
                    elif command_exists("paplay"):
                        subprocess.run(["paplay", str(test_file)], check=False)
                    elif command_exists("aplay"):
                        subprocess.run(["aplay", "-q", str(test_file)], check=False)
                    else:
                        warn("No audio player available for test")

                    test_file.unlink()
                else:
                    warn("Could not generate test audio")
            except Exception as e:
                warn(f"Could not test TTS: {e}")
    else:
        dry("Test TTS with sample audio")

    # --- Print backup info ---

    if not dry_run:
        backup.print_restore_instructions()

    # --- Done! ---

    action = "Upgrade" if upgrade else "Installation"

    print()
    print("========================================")
    if dry_run:
        print(f"{Colors.CYAN}  Dry Run Complete - No changes made{Colors.NC}")
        print("========================================")
        print()
        if upgrade:
            print("Run without --dry-run to upgrade.")
        else:
            print("Run without --dry-run to install.")
    else:
        print(f"{Colors.GREEN}  {action} Complete!{Colors.NC}")
        print("========================================")
        print()
        if upgrade:
            print("Hook and commands have been updated to the latest version.")
            print("Your settings.json configuration was preserved.")
        else:
            print("Next steps:")
            print("  1. Start a new Claude Code session")
            print("  2. Claude's responses will now be spoken aloud")
            print("  3. Use /mute to temporarily silence TTS")
            print("  4. Use /unmute to re-enable TTS")
        print()
        print("Configuration:")
        print(f"  Hook:     {HOOKS_DIR / 'speak-response.sh'}")
        print(f"  Settings: {SETTINGS_FILE}")
        print(f"  Voice:    {VOICE_FILE}")
        print()
        print("Environment variables (optional):")
        print("  CLAUDE_TTS_SPEED=2.0        Playback speed (default: 2.0)")
        print("  CLAUDE_TTS_MAX_CHARS=10000  Max characters to speak")
        print("  CLAUDE_TTS_ENABLED=0        Disable TTS entirely")
        print()
        print("Debug log: /tmp/claude_tts_debug.log")
        print()

        # Record installed version
        set_installed_version(__version__)
        info(f"Version {__version__} recorded in config")
    print()


# --- Config Management ---

DEFAULT_CONFIG = {
    "version": 1,
    "mode": "direct",  # "direct" = immediate playback, "queue" = daemon handles playback
    "active_persona": "claude-prime",
    "muted": False,
    "default_muted": True,  # New sessions are muted by default; use /tts-unmute to enable
    "queue": {
        "max_depth": 20,
        "max_age_seconds": 300,
        "speaker_transition": "chime",  # "chime", "announce", "none"
        "coalesce_rapid_ms": 500,
        "idle_poll_ms": 100,
    },
    "sounds": {
        "enabled": False,
        "volume": 0.5,
        "events": {
            "thinking": None,
            "ready": None,
            "error": "alert",
            "muted": "beep",
            "unmuted": "beep",
        },
    },
    "personas": {
        "claude-prime": {
            "description": "The original Claude voice - fast and chipmunky",
            "voice": "en_US-hfc_male-medium",
            "speed": 2.0,
            "speed_method": "playback",
            "max_chars": 10000,
            "ai_type": "claude",
        },
        "claude-chill": {
            "description": "Relaxed Claude - natural pitch, slower pace",
            "voice": "en_US-hfc_male-medium",
            "speed": 1.5,
            "speed_method": "length_scale",
            "max_chars": 10000,
            "ai_type": "claude",
        },
        "code-reviewer": {
            "description": "For code review agents - authoritative pace",
            "voice": "en_US-hfc_male-medium",
            "speed": 1.8,
            "speed_method": "length_scale",
            "max_chars": 5000,
            "ai_type": "claude",
        },
    },
}


def load_config() -> dict:
    """Load config from file or return defaults."""
    if TTS_CONFIG_FILE.exists():
        with open(TTS_CONFIG_FILE) as f:
            return json.load(f)
    return DEFAULT_CONFIG.copy()


def save_config(config: dict) -> None:
    """Save config to file."""
    TTS_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(TTS_CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def get_installed_version() -> Optional[str]:
    """Get the currently installed version from config."""
    config = load_config()
    return config.get("installed_version")


def set_installed_version(version: str) -> None:
    """Set the installed version in config."""
    config = load_config()
    config["installed_version"] = version
    config["installed_at"] = datetime.now().isoformat()
    save_config(config)


def get_file_hash(filepath: Path) -> str:
    """Get MD5 hash of a file for change detection."""
    if not filepath.exists():
        return ""
    with open(filepath, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()[:8]


def check_for_updates() -> dict:
    """Check if installed files differ from repo files."""
    results = {
        "installed_version": get_installed_version(),
        "repo_version": __version__,
        "files": {},
    }

    # Files to check
    for name, repo, installed in _manifest_entries():
        installed_hash = get_file_hash(installed)
        repo_hash = get_file_hash(repo)

        if not installed.exists():
            status = "not_installed"
        elif installed_hash == repo_hash:
            status = "current"
        else:
            status = "outdated"

        results["files"][name] = {
            "status": status,
            "installed_hash": installed_hash,
            "repo_hash": repo_hash,
        }

    # Overall status
    statuses = [f["status"] for f in results["files"].values()]
    if "not_installed" in statuses:
        results["overall"] = "not_installed"
    elif "outdated" in statuses:
        results["overall"] = "outdated"
    else:
        results["overall"] = "current"

    return results


def do_check() -> None:
    """Check installation status and version."""
    print()
    print("========================================")
    print("  Claude Code TTS - Version Check")
    print("========================================")
    print()

    results = check_for_updates()

    print(f"Repo version:      {results['repo_version']}")
    print(f"Installed version: {results['installed_version'] or 'unknown'}")
    print()

    print("File status:")
    for name, info in results["files"].items():
        if info["status"] == "current":
            status_str = f"{Colors.GREEN}current{Colors.NC}"
        elif info["status"] == "outdated":
            status_str = f"{Colors.YELLOW}outdated{Colors.NC}"
        else:
            status_str = f"{Colors.RED}not installed{Colors.NC}"
        print(f"  {name}: {status_str}")

    print()
    if results["overall"] == "current":
        print(f"{Colors.GREEN}Everything is up to date!{Colors.NC}")
    elif results["overall"] == "outdated":
        print(f"{Colors.YELLOW}Updates available. Run: python install.py --upgrade{Colors.NC}")
    else:
        print(f"{Colors.RED}TTS not installed. Run: python install.py --install{Colors.NC}")
    print()


def prompt_choice(prompt: str, options: list[str], default: int = 0) -> int:
    """Prompt user to choose from options. Returns index."""
    print(f"\n{prompt}")
    for i, opt in enumerate(options):
        marker = ">" if i == default else " "
        print(f"  {marker} [{i + 1}] {opt}")
    print()

    while True:
        try:
            choice = input(f"Enter choice [1-{len(options)}] (default: {default + 1}): ").strip()
            if not choice:
                return default
            idx = int(choice) - 1
            if 0 <= idx < len(options):
                return idx
            print(f"Please enter a number between 1 and {len(options)}")
        except ValueError:
            print("Please enter a valid number")
        except KeyboardInterrupt:
            print("\nCancelled.")
            sys.exit(0)


def prompt_string(prompt: str, default: str = "") -> str:
    """Prompt user for a string value."""
    default_str = f" [{default}]" if default else ""
    try:
        value = input(f"{prompt}{default_str}: ").strip()
        return value if value else default
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(0)


def prompt_float(prompt: str, default: float) -> float:
    """Prompt user for a float value."""
    while True:
        try:
            value = input(f"{prompt} [{default}]: ").strip()
            if not value:
                return default
            return float(value)
        except ValueError:
            print("Please enter a valid number")
        except KeyboardInterrupt:
            print("\nCancelled.")
            sys.exit(0)


def do_manage_personas() -> None:
    """Interactive persona management."""
    config = load_config()

    while True:
        print()
        print("========================================")
        print("  Persona Management")
        print("========================================")
        print()
        print(f"Active persona: {Colors.GREEN}{config['active_persona']}{Colors.NC}")
        print()
        print("Available personas:")
        for name, persona in config["personas"].items():
            active = " (active)" if name == config["active_persona"] else ""
            desc = persona.get("description", "No description")
            ai_type = persona.get("ai_type", "claude")
            ai_badge = f"[{ai_type}]" if ai_type else ""
            print(f"  - {name}{active} {ai_badge}: {desc}")

        choice = prompt_choice(
            "What would you like to do?",
            [
                "Switch active persona",
                "Create new persona",
                "Edit persona",
                "Delete persona",
                "Back to main menu",
            ],
            default=4,
        )

        if choice == 0:  # Switch
            personas = list(config["personas"].keys())
            idx = prompt_choice("Select persona:", personas)
            config["active_persona"] = personas[idx]
            save_config(config)
            success(f"Switched to {personas[idx]}")

        elif choice == 1:  # Create
            print()
            name = prompt_string("Persona name (e.g., 'my-voice')")
            if not name:
                warn("Name cannot be empty")
                continue
            if name in config["personas"]:
                warn(f"Persona '{name}' already exists")
                continue

            desc = prompt_string("Description", "Custom persona")
            ai_idx = prompt_choice("AI type:", ["Claude", "Gemini"], default=0)
            ai_type = "claude" if ai_idx == 0 else "gemini"
            speed = prompt_float("Speed multiplier", 2.0)
            method_idx = prompt_choice(
                "Speed method:",
                ["playback (chipmunk, macOS only)", "length_scale (natural pitch)"],
            )
            method = "playback" if method_idx == 0 else "length_scale"
            max_chars = int(prompt_float("Max characters", 10000))

            config["personas"][name] = {
                "description": desc,
                "voice": "en_US-hfc_male-medium",
                "speed": speed,
                "speed_method": method,
                "max_chars": max_chars,
                "ai_type": ai_type,
            }
            save_config(config)
            success(f"Created persona '{name}'")

        elif choice == 2:  # Edit
            personas = list(config["personas"].keys())
            idx = prompt_choice("Select persona to edit:", personas)
            name = personas[idx]
            persona = config["personas"][name]

            print(f"\nEditing '{name}' (press Enter to keep current value)")
            persona["description"] = prompt_string("Description", persona.get("description", ""))
            persona["speed"] = prompt_float("Speed", persona["speed"])
            method_idx = prompt_choice(
                "Speed method:",
                ["playback (chipmunk)", "length_scale (natural)"],
                default=0 if persona["speed_method"] == "playback" else 1,
            )
            persona["speed_method"] = "playback" if method_idx == 0 else "length_scale"
            persona["max_chars"] = int(prompt_float("Max chars", persona["max_chars"]))

            save_config(config)
            success(f"Updated persona '{name}'")

        elif choice == 3:  # Delete
            personas = list(config["personas"].keys())
            if len(personas) <= 1:
                warn("Cannot delete the last persona")
                continue
            idx = prompt_choice("Select persona to delete:", personas)
            name = personas[idx]
            if name == config["active_persona"]:
                warn("Cannot delete the active persona. Switch to another first.")
                continue

            confirm = prompt_string(f"Type '{name}' to confirm deletion")
            if confirm == name:
                del config["personas"][name]
                save_config(config)
                success(f"Deleted persona '{name}'")
            else:
                info("Deletion cancelled")

        elif choice == 4:  # Back
            break


# --- Voice Download ---

def get_installed_voices() -> set[str]:
    """Get set of already installed voice model names."""
    installed = set()
    if VOICES_DIR.exists():
        for f in VOICES_DIR.glob("*.onnx"):
            # Remove .onnx extension to get voice name
            installed.add(f.stem)
    return installed


def download_voice(voice_name: str, path_segment: str, dry_run: bool = False) -> bool:
    """Download a voice model from Hugging Face."""
    onnx_url = f"{HF_VOICES_BASE}/{path_segment}/{voice_name}.onnx"
    json_url = f"{HF_VOICES_BASE}/{path_segment}/{voice_name}.onnx.json"

    onnx_file = VOICES_DIR / f"{voice_name}.onnx"
    json_file = VOICES_DIR / f"{voice_name}.onnx.json"

    if dry_run:
        dry(f"Download {voice_name}.onnx (~60MB)")
        dry(f"Download {voice_name}.onnx.json")
        return True

    VOICES_DIR.mkdir(parents=True, exist_ok=True)

    try:
        download_file(onnx_url, onnx_file)
        download_file(json_url, json_file)
        return True
    except Exception as e:
        error(f"Failed to download {voice_name}: {e}")
        # Clean up partial downloads
        if onnx_file.exists():
            onnx_file.unlink()
        if json_file.exists():
            json_file.unlink()
        return False


PREVIEW_SAMPLES = [
    "Hello! This is what I sound like. Pretty cool, right?",
    "I can read your code comments and explain what's happening.",
    "Let me help you debug that tricky issue you've been working on.",
]


def preview_voice(voice_name: str, speed: float = 1.5) -> bool:
    """Play a preview of a voice. Returns True if successful."""
    voice_file = VOICES_DIR / f"{voice_name}.onnx"

    if not voice_file.exists():
        warn(f"Voice {voice_name} is not installed")
        return False

    if not command_exists("piper"):
        warn("Piper is not installed - cannot preview")
        return False

    # Pick a random sample
    sample_text = random.choice(PREVIEW_SAMPLES)

    info(f"Previewing {voice_name}...")
    print(f"  \"{sample_text}\"")

    # Generate and play audio
    test_file = Path("/tmp/claude_tts_preview.wav")
    try:
        # Use length_scale for natural pitch preview
        length_scale = str(1.0 / speed)
        subprocess.run(
            ["piper", "--model", str(voice_file), "--length_scale", length_scale,
             "--output_file", str(test_file)],
            input=sample_text,
            text=True,
            capture_output=True,
            check=True,
        )

        if test_file.exists():
            # Play with appropriate player
            if PLATFORM == "macos" and command_exists("afplay"):
                subprocess.run(["afplay", str(test_file)], check=False)
            elif command_exists("paplay"):
                subprocess.run(["paplay", str(test_file)], check=False)
            elif command_exists("aplay"):
                subprocess.run(["aplay", "-q", str(test_file)], check=False)
            else:
                warn("No audio player available")
                return False

            test_file.unlink()
            return True
        else:
            warn("Failed to generate preview audio")
            return False
    except subprocess.CalledProcessError as e:
        warn(f"Piper failed: {e}")
        return False
    except Exception as e:
        warn(f"Preview failed: {e}")
        return False


def create_persona_from_voice(
    voice_name: str,
    gender: str,
    description: str,
    ai_type: str = "claude",
    config: Optional[dict] = None,
) -> str:
    """Create a persona for a downloaded voice. Returns persona name."""
    if config is None:
        config = load_config()

    # Generate persona name from voice
    # e.g., "en_US-amy-medium" -> "amy" or "gemini-amy" if for gemini
    parts = voice_name.split("-")
    base_name = parts[1] if len(parts) > 1 else voice_name

    if ai_type == "gemini":
        persona_name = f"gemini-{base_name}"
    else:
        persona_name = base_name

    # Avoid duplicates
    if persona_name in config["personas"]:
        persona_name = f"{persona_name}-{parts[2]}" if len(parts) > 2 else f"{persona_name}-new"

    # Set speed based on gender preference (male faster for claude style)
    default_speed = 2.0 if gender == "male" else 1.8

    config["personas"][persona_name] = {
        "description": description,
        "voice": voice_name,
        "speed": default_speed,
        "speed_method": "playback" if PLATFORM == "macos" else "length_scale",
        "max_chars": 10000,
        "ai_type": ai_type,
    }

    save_config(config)
    return persona_name


def do_preview_voices() -> None:
    """Preview installed voices."""
    print()
    print("========================================")
    print("  Voice Preview")
    print("========================================")

    installed = get_installed_voices()

    if not installed:
        warn("No voices installed yet. Download some first!")
        return

    print(f"\nInstalled voices: {len(installed)}")
    print()

    # Build list of installed voices with info from AVAILABLE_VOICES
    voice_options = []
    for voice in AVAILABLE_VOICES:
        name, gender, quality, desc, path = voice
        if name in installed:
            gender_icon = "F" if gender == "female" else "M"
            voice_options.append((name, f"[{gender_icon}] {desc} ({quality})"))

    # Add any installed voices not in our curated list
    for name in installed:
        if not any(v[0] == name for v in AVAILABLE_VOICES):
            voice_options.append((name, f"[?] {name} (custom)"))

    display_options = [opt[1] for opt in voice_options]
    display_options.append("Back")

    choice = prompt_choice("Select voice to preview:", display_options)

    if choice == len(display_options) - 1:  # Back
        return

    voice_name = voice_options[choice][0]
    preview_voice(voice_name)

    # Offer to preview again
    again = prompt_choice("Preview another voice?", ["Yes", "No"], default=1)
    if again == 0:
        do_preview_voices()


def do_download_voices() -> None:
    """Interactive voice download menu."""
    print()
    print("========================================")
    print("  Voice Model Downloader")
    print("========================================")

    installed = get_installed_voices()

    print(f"\nInstalled voices: {len(installed)}")
    print(f"Voice directory: {VOICES_DIR}")
    print()

    # Filter options
    filter_choice = prompt_choice(
        "Filter voices by:",
        [
            "All voices",
            "Female voices only",
            "Male voices only",
            "Not installed only",
            "Preview installed voices",
        ],
        default=3,
    )

    # Handle preview option
    if filter_choice == 4:
        do_preview_voices()
        return

    # Apply filter
    voices_to_show = []
    for voice in AVAILABLE_VOICES:
        name, gender, quality, desc, path = voice
        is_installed = name in installed

        if filter_choice == 1 and gender != "female":
            continue
        if filter_choice == 2 and gender != "male":
            continue
        if filter_choice == 3 and is_installed:
            continue

        voices_to_show.append(voice)

    if not voices_to_show:
        info("No voices match the filter (all may already be installed)")
        return

    # Build display list
    display_options = []
    for name, gender, quality, desc, path in voices_to_show:
        is_installed = name in installed
        status = f"{Colors.GREEN}[installed]{Colors.NC}" if is_installed else ""
        gender_icon = "F" if gender == "female" else "M"
        display_options.append(f"[{gender_icon}] {desc} ({quality}) {status}")

    display_options.append("Download ALL listed voices")
    display_options.append("Back to main menu")

    choice = prompt_choice("Select voice to download:", display_options)

    if choice == len(display_options) - 1:  # Back
        return
    elif choice == len(display_options) - 2:  # Download all
        ai_type = prompt_choice(
            "What AI are these voices for?",
            ["Claude (male voices)", "Gemini (female voices)", "Both / Mixed"],
            default=2,
        )

        for voice in voices_to_show:
            name, gender, quality, desc, path = voice
            if name in installed:
                info(f"Skipping {name} (already installed)")
                continue

            info(f"Downloading {name}...")
            if download_voice(name, path):
                # Determine AI type for persona
                if ai_type == 0:
                    voice_ai = "claude"
                elif ai_type == 1:
                    voice_ai = "gemini"
                else:
                    # Auto-assign: male=claude, female=gemini
                    voice_ai = "claude" if gender == "male" else "gemini"

                persona = create_persona_from_voice(name, gender, desc, voice_ai)
                success(f"Downloaded and created persona: {persona}")

    else:  # Single voice
        voice = voices_to_show[choice]
        name, gender, quality, desc, path = voice

        if name in installed:
            info(f"{name} is already installed")
            # Offer preview or persona creation
            action = prompt_choice(
                "What would you like to do?",
                ["Preview this voice", "Create a persona", "Back"],
                default=0,
            )
            if action == 0:  # Preview
                preview_voice(name)
            elif action == 1:  # Create persona
                ai_type = prompt_choice(
                    "AI type for this persona?",
                    ["Claude", "Gemini"],
                    default=0 if gender == "male" else 1,
                )
                persona = create_persona_from_voice(
                    name, gender, desc, "claude" if ai_type == 0 else "gemini"
                )
                success(f"Created persona: {persona}")
            return

        ai_type = prompt_choice(
            "AI type for this persona?",
            ["Claude", "Gemini"],
            default=0 if gender == "male" else 1,
        )

        if download_voice(name, path):
            persona = create_persona_from_voice(
                name, gender, desc, "claude" if ai_type == 0 else "gemini"
            )
            success(f"Downloaded and created persona: {persona}")


def do_bootstrap_from_config(config_path: Path) -> None:
    """Bootstrap installation from a config file - downloads all required voices."""
    print()
    print("========================================")
    print("  Bootstrap from Config")
    print("========================================")
    print()

    if not config_path.exists():
        die(f"Config file not found: {config_path}")

    with open(config_path) as f:
        config = json.load(f)

    info(f"Loading config from {config_path}")

    # Get all unique voices from personas
    voices_needed = set()
    for name, persona in config.get("personas", {}).items():
        voice = persona.get("voice")
        if voice:
            voices_needed.add(voice)

    info(f"Found {len(voices_needed)} unique voice(s) in config")

    # Check which need downloading
    installed = get_installed_voices()
    to_download = voices_needed - installed

    if not to_download:
        success("All required voices are already installed")
    else:
        info(f"Need to download {len(to_download)} voice(s)")
        for voice_name in to_download:
            # Find voice in available list
            voice_info = None
            for v in AVAILABLE_VOICES:
                if v[0] == voice_name:
                    voice_info = v
                    break

            if voice_info:
                name, gender, quality, desc, path = voice_info
                info(f"Downloading {name}...")
                if download_voice(name, path):
                    success(f"Downloaded {name}")
                else:
                    error(f"Failed to download {name}")
            else:
                warn(f"Voice {voice_name} not in curated list, skipping")

    # Copy config to TTS config location
    info("Installing config...")
    TTS_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy(config_path, TTS_CONFIG_FILE)
    success(f"Config installed to {TTS_CONFIG_FILE}")

    print()
    success("Bootstrap complete!")


def do_interactive() -> None:
    """Interactive main menu."""
    print()
    print("========================================")
    print(f"  Claude Code TTS - Interactive Setup")
    print("========================================")

    # Check current state
    is_installed = (HOOKS_DIR / "speak-response.sh").exists()
    has_config = TTS_CONFIG_FILE.exists()

    if is_installed:
        print(f"\n{Colors.GREEN}TTS is installed{Colors.NC}")
        if has_config:
            config = load_config()
            print(f"Active persona: {config['active_persona']}")
            print(f"Muted: {config['muted']}")
    else:
        print(f"\n{Colors.YELLOW}TTS is not installed{Colors.NC}")

    options = []
    actions = []

    if not is_installed:
        options.append("Install Claude Code TTS")
        actions.append("install")
    else:
        options.append("Upgrade to latest version")
        actions.append("upgrade")

    options.append("Manage personas")
    actions.append("personas")

    options.append("Download new voices")
    actions.append("voices")

    if is_installed:
        options.append("Toggle mute/unmute")
        actions.append("mute")

    options.append("Uninstall")
    actions.append("uninstall")

    options.append("Exit")
    actions.append("exit")

    choice = prompt_choice("What would you like to do?", options)
    action = actions[choice]

    if action == "install":
        do_install(dry_run=False, upgrade=False)
    elif action == "upgrade":
        do_install(dry_run=False, upgrade=True)
    elif action == "personas":
        do_manage_personas()
        do_interactive()  # Return to main menu
    elif action == "voices":
        do_download_voices()
        do_interactive()  # Return to main menu
    elif action == "mute":
        config = load_config()
        config["muted"] = not config["muted"]
        save_config(config)
        state = "muted" if config["muted"] else "unmuted"
        success(f"TTS is now {state}")
        do_interactive()  # Return to main menu
    elif action == "uninstall":
        confirm = prompt_string("Type 'uninstall' to confirm")
        if confirm == "uninstall":
            do_uninstall(dry_run=False)
        else:
            info("Uninstall cancelled")
            do_interactive()
    elif action == "exit":
        print("\nGoodbye!")
        sys.exit(0)


# --- Main ---

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Install or uninstall Claude Code TTS",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python install.py                      # Interactive menu
  python install.py --install            # Direct install
  python install.py --upgrade            # Update to latest version
  python install.py --check              # Check for updates
  python install.py --version            # Show version info
  python install.py --personas           # Manage personas
  python install.py --voices             # Download new voice models
  python install.py --preview            # Preview installed voices
  python install.py --bootstrap config.json  # Install from config file
  python install.py --uninstall          # Remove TTS
        """,
    )
    parser.add_argument(
        "--install",
        action="store_true",
        help="Install TTS (non-interactive)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without making changes",
    )
    parser.add_argument(
        "--upgrade",
        action="store_true",
        help="Upgrade existing installation (updates hook and commands, preserves settings)",
    )
    parser.add_argument(
        "--personas",
        action="store_true",
        help="Manage personas interactively",
    )
    parser.add_argument(
        "--voices",
        action="store_true",
        help="Download new voice models from Hugging Face",
    )
    parser.add_argument(
        "--preview",
        action="store_true",
        help="Preview installed voice models",
    )
    parser.add_argument(
        "--bootstrap",
        type=str,
        metavar="CONFIG",
        help="Bootstrap from config file (downloads required voices and installs config)",
    )
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help="Remove Claude Code TTS completely",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="Show version information",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check if updates are available",
    )

    args = parser.parse_args()

    # If any specific action is requested, do it directly
    if args.version:
        print(f"claude-code-tts version {__version__}")
        installed = get_installed_version()
        if installed:
            print(f"Installed version: {installed}")
        sys.exit(0)
    elif args.check:
        do_check()
    elif args.uninstall:
        do_uninstall(dry_run=args.dry_run)
    elif args.bootstrap:
        do_bootstrap_from_config(Path(args.bootstrap))
    elif args.upgrade:
        do_install(dry_run=args.dry_run, upgrade=True)
    elif args.install:
        do_install(dry_run=args.dry_run, upgrade=False)
    elif args.personas:
        do_manage_personas()
    elif args.voices:
        do_download_voices()
    elif args.preview:
        do_preview_voices()
    elif args.dry_run:
        do_install(dry_run=True, upgrade=False)
    else:
        # No args = interactive mode
        do_interactive()


if __name__ == "__main__":
    main()
