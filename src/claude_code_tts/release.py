"""Release workflow for Claude Code TTS.

Replaces scripts/release.sh with a Python implementation.
Runs pre-release checks and guides version bumping.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


def _repo_dir() -> Path:
    """Find the repo root (directory containing pyproject.toml)."""
    d = Path(__file__).resolve().parent.parent.parent
    if (d / "pyproject.toml").exists():
        return d
    # Fallback: walk up from cwd
    d = Path.cwd()
    while d != d.parent:
        if (d / "pyproject.toml").exists():
            return d
        d = d.parent
    print("Could not find repo root (no pyproject.toml)")
    sys.exit(1)


def _current_version(repo: Path) -> str:
    """Read current version from pyproject.toml."""
    for line in (repo / "pyproject.toml").read_text().splitlines():
        if line.strip().startswith("current_version"):
            return line.split('"')[1]
    return "unknown"


def run_checks(repo: Path) -> bool:
    """Run pre-release checks. Returns True if all pass."""
    print()
    print("========================================")
    print("  Pre-release Checks")
    print("========================================")
    print()

    errors = 0
    warnings = 0

    # Check 1: Uncommitted changes
    print("1. Checking for uncommitted changes... ", end="")
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True, text=True, cwd=repo,
    )
    if result.stdout.strip():
        print("UNCOMMITTED CHANGES")
        print(result.stdout)
        warnings += 1
    else:
        print("OK")

    # Check 2: Version consistency
    print("2. Checking version consistency... ", end="")
    check_script = repo / "scripts" / "check-version.sh"
    if check_script.exists():
        r = subprocess.run(
            ["bash", str(check_script)],
            capture_output=True, cwd=repo,
        )
        if r.returncode == 0:
            print(f"OK (v{_current_version(repo)})")
        else:
            print("FAIL")
            subprocess.run(["bash", str(check_script)], cwd=repo)
            errors += 1
    else:
        print(f"OK (v{_current_version(repo)}) [no check script]")

    # Check 3: Commands in installer
    print("3. Checking all commands wired in installer... ", end="")
    commands_dir = repo / "commands"
    if commands_dir.is_dir():
        cmd_files = {f.stem for f in commands_dir.glob("*.md")}
        install_py = (repo / "src" / "claude_code_tts" / "install.py").read_text()
        missing = [c for c in sorted(cmd_files) if f"{c}.md" not in install_py]
        if missing:
            print("FAIL")
            print(f"   Missing from installer: {' '.join(missing)}")
            errors += 1
        else:
            print("OK")
    else:
        print("OK (no commands dir)")

    # Check 4: Python syntax
    print("4. Checking Python syntax... ", end="")
    py_files = list((repo / "src" / "claude_code_tts").glob("*.py"))
    syntax_ok = True
    for pf in py_files:
        r = subprocess.run(
            ["python3", "-m", "py_compile", str(pf)],
            capture_output=True, cwd=repo,
        )
        if r.returncode != 0:
            syntax_ok = False
            print(f"FAIL ({pf.name})")
            errors += 1
            break
    if syntax_ok:
        print("OK")

    # Check 5: bump-my-version installed
    print("5. Checking bump-my-version installed... ", end="")
    if shutil.which("bump-my-version"):
        print("OK")
    else:
        print("FAIL - Install with: uv tool install bump-my-version")
        errors += 1

    # Check 6: Tests pass
    print("6. Running tests... ", end="")
    r = subprocess.run(
        ["uv", "run", "pytest", "tests/", "-q"],
        capture_output=True, text=True, cwd=repo,
    )
    if r.returncode == 0:
        # Extract summary line
        lines = r.stdout.strip().splitlines()
        summary = lines[-1] if lines else "OK"
        print(f"OK ({summary})")
    else:
        print("FAIL")
        print(r.stdout[-500:] if len(r.stdout) > 500 else r.stdout)
        errors += 1

    print()

    if errors:
        print("Checks failed. Fix errors before releasing.")
        return False

    if warnings:
        print("Checks passed with warnings.")
    else:
        print("All checks passed!")

    return True


def do_release(repo: Path, bump_type: str) -> None:
    """Perform a version bump and push."""
    print()
    print(f"Bumping version ({bump_type})...")
    r = subprocess.run(
        ["bump-my-version", "bump", bump_type],
        cwd=repo,
    )
    if r.returncode != 0:
        print("Version bump failed")
        sys.exit(1)

    print()
    print("Pushing to origin with tags...")
    subprocess.run(["git", "push", "origin", "main", "--tags"], cwd=repo)

    new_version = _current_version(repo)
    print()
    print("========================================")
    print(f"  Released v{new_version}")
    print("========================================")
    print()

    # Sync local install — rebuild CLI binary and upgrade deployed files
    print("Syncing local installation...")
    subprocess.run(
        ["uv", "tool", "install", str(repo), "--force"],
        capture_output=True, cwd=repo,
    )
    subprocess.run(
        ["claude-tts-install", "--upgrade"],
        capture_output=True, cwd=repo,
    )
    print("Done!")


def main(args: list[str] | None = None) -> None:
    """Release CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="Claude Code TTS Release Manager")
    parser.add_argument(
        "bump", nargs="?", choices=["patch", "minor", "major"],
        help="Version bump type",
    )
    parser.add_argument("--check", action="store_true", help="Run checks only")
    parser.add_argument("--install-hooks", action="store_true", help="Install git hooks")

    parsed = parser.parse_args(args)
    repo = _repo_dir()

    if parsed.install_hooks:
        hook_src = repo / "scripts" / "pre-push"
        hook_dst = repo / ".git" / "hooks" / "pre-push"
        if hook_src.exists():
            import shutil
            shutil.copy2(hook_src, hook_dst)
            hook_dst.chmod(0o755)
            print("Pre-push hook installed")
        else:
            print("No pre-push hook found in scripts/")
        return

    if parsed.check:
        ok = run_checks(repo)
        sys.exit(0 if ok else 1)

    # Run checks first
    if not run_checks(repo):
        sys.exit(1)

    if parsed.bump:
        do_release(repo, parsed.bump)
        return

    # Interactive mode
    print()
    print("Select release type:")
    print("  [1] patch - Bug fixes")
    print("  [2] minor - New features")
    print("  [3] major - Breaking changes")
    print("  [4] Cancel")
    print()

    try:
        choice = input("Choice [1-4]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled")
        return

    bump_map = {"1": "patch", "2": "minor", "3": "major"}
    if choice in bump_map:
        do_release(repo, bump_map[choice])
    else:
        print("Cancelled")
