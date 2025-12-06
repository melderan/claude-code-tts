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
import json
import os
import platform
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional


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

# Get repo directory (parent of scripts/)
SCRIPT_DIR = Path(__file__).parent.resolve()
REPO_DIR = SCRIPT_DIR.parent


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
    src_hook = REPO_DIR / "hooks" / "speak-response.sh"
    if not src_hook.exists():
        issues.append(f"Source hook not found: {src_hook}")
        preflight(f"{Colors.RED}FAIL{Colors.NC} Source hook missing")
    else:
        preflight(f"{Colors.GREEN}PASS{Colors.NC} Source hook found")

    for cmd_name in ["mute.md", "unmute.md"]:
        src_cmd = REPO_DIR / "commands" / cmd_name
        if not src_cmd.exists():
            issues.append(f"Source command not found: {src_cmd}")
            preflight(f"{Colors.RED}FAIL{Colors.NC} Source {cmd_name} missing")
        else:
            preflight(f"{Colors.GREEN}PASS{Colors.NC} Source {cmd_name} found")

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

    # Remove hook script
    hook_file = HOOKS_DIR / "speak-response.sh"
    if hook_file.exists():
        if dry_run:
            dry(f"rm {hook_file}")
        else:
            hook_file.unlink()
        success(f"Removed hook: {hook_file}")
    else:
        info("Hook not found (already removed)")

    # Remove slash commands
    for cmd_name in ["mute.md", "unmute.md"]:
        cmd_file = COMMANDS_DIR / cmd_name
        if cmd_file.exists():
            if dry_run:
                dry(f"rm {cmd_file}")
            else:
                cmd_file.unlink()
            success(f"Removed command: /{cmd_name.replace('.md', '')}")

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

    files_to_backup = [
        SETTINGS_FILE,
        HOOKS_DIR / "speak-response.sh",
        COMMANDS_DIR / "mute.md",
        COMMANDS_DIR / "unmute.md",
    ]

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

    # --- Install hook script ---

    print()
    src_hook = REPO_DIR / "hooks" / "speak-response.sh"
    dst_hook = HOOKS_DIR / "speak-response.sh"
    if dry_run:
        dry(f"cp {src_hook} -> {dst_hook}")
    else:
        shutil.copy(src_hook, dst_hook)
        dst_hook.chmod(0o755)
    success(f"Hook: {dst_hook}")

    # --- Install slash commands ---

    for cmd_name in ["mute.md", "unmute.md"]:
        src_cmd = REPO_DIR / "commands" / cmd_name
        dst_cmd = COMMANDS_DIR / cmd_name
        if dry_run:
            dry(f"cp {src_cmd} -> {dst_cmd}")
        else:
            shutil.copy(src_cmd, dst_cmd)
    success("Commands: /mute, /unmute")

    # --- Configure settings.json ---

    print()
    new_hook = {
        "hooks": [{"type": "command", "command": str(dst_hook), "timeout": 180}]
    }

    if upgrade:
        # In upgrade mode, skip settings.json modification - just update files
        info("Upgrade mode: keeping existing settings.json configuration")
        success("Settings preserved")
    elif SETTINGS_FILE.exists():
        info("Configuring Claude Code settings...")
        with open(SETTINGS_FILE) as f:
            content = f.read()

        if "speak-response.sh" in content:
            success("TTS hook already configured in settings.json")
        else:
            if dry_run:
                dry("Add TTS hook to settings.json (preserving existing hooks)")
            else:
                # Load, modify, save
                with open(SETTINGS_FILE) as f:
                    settings = json.load(f)

                if "hooks" not in settings:
                    settings["hooks"] = {}
                if "Stop" not in settings["hooks"]:
                    settings["hooks"]["Stop"] = []

                settings["hooks"]["Stop"].append(new_hook)

                with open(SETTINGS_FILE, "w") as f:
                    json.dump(settings, f, indent=2)
                success("Hook added to settings.json (preserving existing hooks)")
    else:
        info("Configuring Claude Code settings...")
        if dry_run:
            dry(f"Create {SETTINGS_FILE} with hook configuration")
        else:
            settings = {"hooks": {"Stop": [new_hook]}}
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
        print(f"  Hook:     {dst_hook}")
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


# --- Main ---

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Install or uninstall Claude Code TTS",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python install.py              # Install TTS
  python install.py --dry-run    # Preview installation
  python install.py --upgrade    # Update to latest version
  python install.py --uninstall  # Remove TTS
        """,
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
        "--uninstall",
        action="store_true",
        help="Remove Claude Code TTS completely",
    )

    args = parser.parse_args()

    if args.uninstall:
        do_uninstall(dry_run=args.dry_run)
    else:
        do_install(dry_run=args.dry_run, upgrade=args.upgrade)


if __name__ == "__main__":
    main()
