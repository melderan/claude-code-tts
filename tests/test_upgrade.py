"""Tests for the v6.x → v7.x upgrade path.

Verifies that the installer correctly handles:
- MANIFEST structure (no scripts category)
- Legacy script cleanup
- Hook thin shims
- Command files calling claude-tts CLI
- Config and session preservation
- Backup creation during upgrade
- Idempotent re-upgrades
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

# Import the installer module so we can monkeypatch its constants
import claude_code_tts.install as inst


# The exact set of scripts that existed in v6.x MANIFEST["scripts"].
# If this changes, the test should break — it means LEGACY_SCRIPTS needs updating.
V6_SCRIPTS = {
    "tts-daemon.py", "tts-mode.sh", "tts-mute.sh", "tts-unmute.sh",
    "tts-status.sh", "tts-speed.sh", "tts-persona.sh", "tts-cleanup.sh",
    "tts-random.sh", "tts-test.sh", "tts-speak.sh", "tts-audition.sh",
    "tts-discover.sh", "tts-pause.sh", "tts-lib.sh", "tts-filter.py",
    "tts-sounds.sh", "tts-intermediate.sh",
}

# Scripts that should survive cleanup (standalone tools, not part of CLI)
SURVIVOR_SCRIPTS = {"tts-builder.py", "tts-builder.sh"}


def _repo_dir() -> Path:
    """Get the repo root."""
    return Path(__file__).parent.parent


# ---------------------------------------------------------------------------
# MANIFEST structure
# ---------------------------------------------------------------------------

class TestManifest:
    """Verify MANIFEST is correct for v7.x."""

    def test_no_scripts_category(self):
        assert "scripts" not in inst.MANIFEST, (
            "MANIFEST should not have a 'scripts' category — "
            "scripts are no longer deployed to ~/.claude-tts/"
        )

    def test_has_hooks_category(self):
        assert "hooks" in inst.MANIFEST

    def test_has_commands_category(self):
        assert "commands" in inst.MANIFEST

    def test_all_hook_sources_exist(self):
        repo = _repo_dir()
        for name in inst.MANIFEST["hooks"]:
            src = repo / "hooks" / name
            assert src.exists(), f"Hook source missing: {src}"

    def test_all_command_sources_exist(self):
        repo = _repo_dir()
        for name in inst.MANIFEST["commands"]:
            src = repo / "commands" / name
            assert src.exists(), f"Command source missing: {src}"

    def test_manifest_dirs_no_scripts(self):
        with pytest.raises(KeyError):
            inst._manifest_dirs("scripts")

    def test_manifest_entries_yields_only_hooks_and_commands(self):
        categories = set()
        for name, src, dst in inst._manifest_entries():
            # Determine category from source path
            if "hooks" in str(src):
                categories.add("hooks")
            elif "commands" in str(src):
                categories.add("commands")
            else:
                pytest.fail(f"Unexpected source path: {src}")
        assert categories == {"hooks", "commands"}


# ---------------------------------------------------------------------------
# LEGACY_SCRIPTS completeness
# ---------------------------------------------------------------------------

class TestLegacyScripts:
    """Verify the legacy scripts list is complete and correct."""

    def test_covers_all_v6_scripts(self):
        """LEGACY_SCRIPTS + COMPAT_SHIMS must account for every v6 script."""
        covered = set(inst.LEGACY_SCRIPTS) | set(inst.COMPAT_SHIMS)
        assert covered == V6_SCRIPTS, (
            f"LEGACY_SCRIPTS + COMPAT_SHIMS mismatch with v6 scripts.\n"
            f"  Missing: {V6_SCRIPTS - covered}\n"
            f"  Extra:   {covered - V6_SCRIPTS}"
        )

    def test_no_overlap_between_legacy_and_shims(self):
        """A script is either deleted (legacy) or shimmed, never both."""
        overlap = set(inst.LEGACY_SCRIPTS) & set(inst.COMPAT_SHIMS)
        assert not overlap, f"Scripts in both LEGACY_SCRIPTS and COMPAT_SHIMS: {overlap}"

    def test_builder_not_in_legacy(self):
        for name in SURVIVOR_SCRIPTS:
            assert name not in inst.LEGACY_SCRIPTS, (
                f"{name} should NOT be in LEGACY_SCRIPTS — it's a standalone tool"
            )

    def test_no_duplicates(self):
        assert len(inst.LEGACY_SCRIPTS) == len(set(inst.LEGACY_SCRIPTS))


# ---------------------------------------------------------------------------
# Hook content verification
# ---------------------------------------------------------------------------

class TestHookContent:
    """Verify hooks are thin shims that delegate to claude-tts CLI."""

    def test_speak_response_is_shim(self):
        hook = _repo_dir() / "hooks" / "speak-response.sh"
        content = hook.read_text()
        lines = content.strip().splitlines()
        assert len(lines) <= 3, f"Hook too long ({len(lines)} lines) — should be a thin shim"
        assert "exec claude-tts speak --from-hook --hook-type stop" in content

    def test_speak_intermediate_is_shim(self):
        hook = _repo_dir() / "hooks" / "speak-intermediate.sh"
        content = hook.read_text()
        lines = content.strip().splitlines()
        assert len(lines) <= 3, f"Hook too long ({len(lines)} lines) — should be a thin shim"
        assert "exec claude-tts speak --from-hook --hook-type post_tool_use" in content

    def test_hooks_are_executable_bash(self):
        for name in ["speak-response.sh", "speak-intermediate.sh"]:
            hook = _repo_dir() / "hooks" / name
            content = hook.read_text()
            assert content.startswith("#!/bin/bash"), f"{name} missing bash shebang"


# ---------------------------------------------------------------------------
# Command content verification
# ---------------------------------------------------------------------------

class TestCommandContent:
    """Verify all slash commands call claude-tts CLI, not old scripts."""

    def test_all_commands_call_claude_tts(self):
        repo = _repo_dir()
        for name in inst.MANIFEST["commands"]:
            content = (repo / "commands" / name).read_text()
            assert "claude-tts" in content, (
                f"{name} doesn't reference claude-tts CLI"
            )

    def test_no_commands_reference_old_scripts(self):
        repo = _repo_dir()
        for name in inst.MANIFEST["commands"]:
            content = (repo / "commands" / name).read_text()
            assert "~/.claude-tts/" not in content, (
                f"{name} still references old ~/.claude-tts/ scripts"
            )
            assert "$HOME/.claude-tts/" not in content, (
                f"{name} still references old $HOME/.claude-tts/ scripts"
            )


# ---------------------------------------------------------------------------
# Legacy cleanup simulation
# ---------------------------------------------------------------------------

class TestLegacyCleanup:
    """Simulate the legacy script cleanup that happens during --upgrade."""

    def _populate_v6_state(self, tts_dir: Path) -> None:
        """Create a realistic v6.x ~/.claude-tts/ directory."""
        tts_dir.mkdir(parents=True, exist_ok=True)

        # Legacy scripts
        for script in V6_SCRIPTS:
            (tts_dir / script).write_text(f"#!/bin/bash\n# v6 legacy: {script}\n")

        # Builder scripts (should survive)
        for script in SURVIVOR_SCRIPTS:
            (tts_dir / script).write_text(f"#!/bin/bash\n# standalone: {script}\n")

        # Config and runtime files (should survive)
        (tts_dir / "config.json").write_text(json.dumps({
            "version": 1,
            "mode": "queue",
            "active_persona": "claude-prime",
            "muted": False,
            "installed_version": "6.2.0",
            "personas": {"claude-prime": {"voice": "en_US-hfc_male-medium", "speed": 2.0}},
        }))

        sessions_dir = tts_dir / "sessions.d"
        sessions_dir.mkdir()
        (sessions_dir / "my-project.json").write_text(json.dumps({
            "muted": False, "persona": "claude-prime",
        }))

        # Queue dir (runtime)
        (tts_dir / "queue").mkdir()
        (tts_dir / "daemon.log").write_text("daemon log content\n")
        (tts_dir / "daemon.pid").write_text("12345\n")

    def test_removes_all_legacy_scripts(self, tmp_path):
        tts_dir = tmp_path / ".claude-tts"
        self._populate_v6_state(tts_dir)

        # Run cleanup logic (extracted from do_install)
        removed = 0
        for script_name in inst.LEGACY_SCRIPTS:
            old_script = tts_dir / script_name
            if old_script.exists():
                old_script.unlink()
                removed += 1

        # Legacy list no longer includes compat-shimmed scripts
        shimmed = set(inst.COMPAT_SHIMS)
        expected_removed = V6_SCRIPTS - shimmed
        assert removed == len(expected_removed)
        for script in expected_removed:
            assert not (tts_dir / script).exists(), f"{script} not cleaned up"
        # Shimmed scripts still exist (original v6 version, not yet replaced by shim)
        for script in shimmed:
            assert (tts_dir / script).exists(), f"{script} should not be deleted by legacy cleanup"

    def test_preserves_builder_scripts(self, tmp_path):
        tts_dir = tmp_path / ".claude-tts"
        self._populate_v6_state(tts_dir)

        # Run cleanup
        for script_name in inst.LEGACY_SCRIPTS:
            old_script = tts_dir / script_name
            if old_script.exists():
                old_script.unlink()

        for script in SURVIVOR_SCRIPTS:
            assert (tts_dir / script).exists(), f"{script} was incorrectly removed"

    def test_preserves_config(self, tmp_path):
        tts_dir = tmp_path / ".claude-tts"
        self._populate_v6_state(tts_dir)

        # Run cleanup
        for script_name in inst.LEGACY_SCRIPTS:
            old_script = tts_dir / script_name
            if old_script.exists():
                old_script.unlink()

        assert (tts_dir / "config.json").exists()
        config = json.loads((tts_dir / "config.json").read_text())
        assert config["installed_version"] == "6.2.0"
        assert config["active_persona"] == "claude-prime"

    def test_preserves_sessions_d(self, tmp_path):
        tts_dir = tmp_path / ".claude-tts"
        self._populate_v6_state(tts_dir)

        # Run cleanup
        for script_name in inst.LEGACY_SCRIPTS:
            old_script = tts_dir / script_name
            if old_script.exists():
                old_script.unlink()

        assert (tts_dir / "sessions.d" / "my-project.json").exists()

    def test_preserves_runtime_files(self, tmp_path):
        tts_dir = tmp_path / ".claude-tts"
        self._populate_v6_state(tts_dir)

        # Run cleanup
        for script_name in inst.LEGACY_SCRIPTS:
            old_script = tts_dir / script_name
            if old_script.exists():
                old_script.unlink()

        assert (tts_dir / "daemon.log").exists()
        assert (tts_dir / "daemon.pid").exists()
        assert (tts_dir / "queue").is_dir()

    def test_handles_partial_legacy(self, tmp_path):
        """Only some legacy scripts present (partial v6 install)."""
        tts_dir = tmp_path / ".claude-tts"
        tts_dir.mkdir()
        # Only 3 of the 18 scripts
        for script in ["tts-lib.sh", "tts-mute.sh", "tts-speak.sh"]:
            (tts_dir / script).write_text("#!/bin/bash\n")

        removed = 0
        for script_name in inst.LEGACY_SCRIPTS:
            old_script = tts_dir / script_name
            if old_script.exists():
                old_script.unlink()
                removed += 1

        assert removed == 3

    def test_handles_no_legacy(self, tmp_path):
        """No legacy scripts at all (fresh v7 install)."""
        tts_dir = tmp_path / ".claude-tts"
        tts_dir.mkdir()

        removed = 0
        for script_name in inst.LEGACY_SCRIPTS:
            old_script = tts_dir / script_name
            if old_script.exists():
                old_script.unlink()
                removed += 1

        assert removed == 0

    def test_idempotent_cleanup(self, tmp_path):
        """Running cleanup twice doesn't error."""
        tts_dir = tmp_path / ".claude-tts"
        self._populate_v6_state(tts_dir)

        for _ in range(2):
            for script_name in inst.LEGACY_SCRIPTS:
                old_script = tts_dir / script_name
                if old_script.exists():
                    old_script.unlink()

        # Should still have non-legacy files
        assert (tts_dir / "config.json").exists()


# ---------------------------------------------------------------------------
# Compatibility shims
# ---------------------------------------------------------------------------

class TestCompatShims:
    """Verify compatibility shims forward to claude-tts CLI."""

    def test_shim_content_is_valid_bash(self):
        for shim_name, cli_cmd in inst.COMPAT_SHIMS.items():
            content = inst._make_compat_shim(cli_cmd)
            assert content.startswith("#!/bin/bash"), f"{shim_name} missing bash shebang"

    def test_shim_forwards_to_cli(self):
        for shim_name, cli_cmd in inst.COMPAT_SHIMS.items():
            content = inst._make_compat_shim(cli_cmd)
            assert f"exec claude-tts {cli_cmd}" in content, (
                f"{shim_name} doesn't forward to claude-tts {cli_cmd}"
            )

    def test_shim_emits_deprecation_to_stderr(self):
        for shim_name, cli_cmd in inst.COMPAT_SHIMS.items():
            content = inst._make_compat_shim(cli_cmd)
            assert "DEPRECATED" in content, f"{shim_name} missing deprecation notice"
            assert ">&2" in content, f"{shim_name} deprecation not on stderr"

    def test_shim_passes_args_through(self):
        for shim_name, cli_cmd in inst.COMPAT_SHIMS.items():
            content = inst._make_compat_shim(cli_cmd)
            assert '"$@"' in content, f"{shim_name} doesn't forward arguments"

    def test_shim_deployed_during_upgrade(self, tmp_path):
        """Shims are written to ~/.claude-tts/ during upgrade."""
        tts_dir = tmp_path / ".claude-tts"
        tts_dir.mkdir()

        for shim_name, cli_cmd in inst.COMPAT_SHIMS.items():
            shim_path = tts_dir / shim_name
            shim_content = inst._make_compat_shim(cli_cmd)
            shim_path.write_text(shim_content)
            shim_path.chmod(0o755)

            assert shim_path.exists()
            assert "exec claude-tts" in shim_path.read_text()
            assert oct(shim_path.stat().st_mode)[-3:] == "755"

    def test_shim_replaces_old_v6_script(self, tmp_path):
        """If a v6 script exists, it gets replaced by the shim."""
        tts_dir = tmp_path / ".claude-tts"
        tts_dir.mkdir()

        for shim_name, cli_cmd in inst.COMPAT_SHIMS.items():
            shim_path = tts_dir / shim_name
            # Write old v6 content
            shim_path.write_text("#!/bin/bash\n# old v6 script\n")

            # Deploy shim (same logic as do_install)
            shim_content = inst._make_compat_shim(cli_cmd)
            shim_path.write_text(shim_content)
            shim_path.chmod(0o755)

            assert "exec claude-tts" in shim_path.read_text()
            assert "old v6 script" not in shim_path.read_text()

    def test_shim_idempotent(self, tmp_path):
        """Deploying shim twice produces identical results."""
        tts_dir = tmp_path / ".claude-tts"
        tts_dir.mkdir()

        for shim_name, cli_cmd in inst.COMPAT_SHIMS.items():
            shim_path = tts_dir / shim_name
            shim_content = inst._make_compat_shim(cli_cmd)

            shim_path.write_text(shim_content)
            first = shim_path.read_text()

            shim_path.write_text(shim_content)
            second = shim_path.read_text()

            assert first == second


# ---------------------------------------------------------------------------
# Backup manager
# ---------------------------------------------------------------------------

class TestBackupDuringUpgrade:
    """Verify backups are created before destructive operations."""

    def test_backup_manager_creates_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(inst, "BACKUP_DIR", tmp_path / "backups")
        monkeypatch.setattr(inst, "HOME", tmp_path)

        bm = inst.BackupManager(dry_run=False)
        backup_path = bm.create_backup_dir()

        assert backup_path.exists()
        assert backup_path.is_dir()
        assert "backup_" in backup_path.name

    def test_backup_preserves_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr(inst, "BACKUP_DIR", tmp_path / "backups")
        monkeypatch.setattr(inst, "HOME", tmp_path)

        # Create a file to backup
        src_file = tmp_path / ".claude-tts" / "config.json"
        src_file.parent.mkdir(parents=True)
        src_file.write_text('{"version": 1}')

        bm = inst.BackupManager(dry_run=False)
        backup_dest = bm.backup_file(src_file)

        assert backup_dest is not None
        assert backup_dest.exists()
        assert backup_dest.read_text() == '{"version": 1}'

    def test_backup_dry_run_no_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr(inst, "BACKUP_DIR", tmp_path / "backups")
        monkeypatch.setattr(inst, "HOME", tmp_path)

        src_file = tmp_path / ".claude-tts" / "config.json"
        src_file.parent.mkdir(parents=True)
        src_file.write_text('{"version": 1}')

        bm = inst.BackupManager(dry_run=True)
        backup_dest = bm.backup_file(src_file)

        # Dry run: should not create backup files
        assert backup_dest is not None  # returns path
        assert not backup_dest.exists()  # but doesn't create it


# ---------------------------------------------------------------------------
# Preflight checks (mocked)
# ---------------------------------------------------------------------------

class TestPreflight:
    """Test preflight check logic with mocked filesystem."""

    def test_manifest_sources_all_exist(self):
        """Every file in MANIFEST has a corresponding source in the repo."""
        repo = _repo_dir()
        for name, src, dst in inst._manifest_entries():
            assert src.exists(), f"Preflight would fail: {name} source missing at {src}"

    def test_no_legacy_sources_needed(self):
        """Preflight doesn't check for legacy script sources."""
        # The old MANIFEST["scripts"] entries no longer exist in the repo.
        # Preflight only iterates _manifest_entries() which only covers
        # hooks and commands now.
        repo = _repo_dir()
        for name, src, dst in inst._manifest_entries():
            # None of these should be legacy scripts
            assert name not in V6_SCRIPTS, (
                f"Preflight still checks for legacy script: {name}"
            )


# ---------------------------------------------------------------------------
# Version consistency
# ---------------------------------------------------------------------------

class TestVersionConsistency:
    """Verify version is consistent across all files."""

    def test_versions_match(self):
        from claude_code_tts import __version__ as pkg_version
        assert inst.__version__ == pkg_version, (
            f"install.py ({inst.__version__}) != __init__.py ({pkg_version})"
        )

    def test_version_is_8x(self):
        assert inst.__version__.startswith("8."), (
            f"Expected 8.x version, got {inst.__version__}"
        )


# ---------------------------------------------------------------------------
# Full upgrade simulation (filesystem only, no subprocess)
# ---------------------------------------------------------------------------

class TestUpgradeSimulation:
    """Simulate the file operations of a v6 → v7 upgrade."""

    def _setup_v6_install(self, base: Path) -> dict[str, Path]:
        """Create a complete v6.x install state and return paths."""
        home = base / "home"
        claude_dir = home / ".claude"
        hooks_dir = claude_dir / "hooks"
        commands_dir = claude_dir / "commands"
        tts_dir = home / ".claude-tts"
        sessions_dir = tts_dir / "sessions.d"

        for d in [hooks_dir, commands_dir, tts_dir, sessions_dir]:
            d.mkdir(parents=True)

        # Old hooks (full bash, not shims)
        (hooks_dir / "speak-response.sh").write_text(
            "#!/bin/bash\n"
            "# Full v6 hook with jq config loading\n"
            "CONFIG=$(jq -r '...' ~/.claude-tts/config.json)\n"
            "source ~/.claude-tts/tts-lib.sh\n"
        )
        (hooks_dir / "speak-intermediate.sh").write_text(
            "#!/bin/bash\n"
            "exec ~/.claude-tts/tts-intermediate.sh\n"
        )

        # Old commands (calling scripts)
        for cmd in inst.MANIFEST["commands"]:
            stem = cmd.replace("tts-", "").replace(".md", "")
            (commands_dir / cmd).write_text(
                f"Run this command:\n```bash\n"
                f"$HOME/.claude-tts/tts-{stem}.sh $ARGUMENTS\n```\n"
            )

        # Old scripts
        for script in V6_SCRIPTS:
            (tts_dir / script).write_text(f"#!/bin/bash\n# v6: {script}")

        # Builder scripts
        for script in SURVIVOR_SCRIPTS:
            (tts_dir / script).write_text(f"#!/bin/bash\n# builder: {script}")

        # Config
        (tts_dir / "config.json").write_text(json.dumps({
            "version": 1,
            "mode": "queue",
            "active_persona": "my-persona",
            "installed_version": "6.2.0",
            "personas": {
                "claude-prime": {"voice": "en_US-hfc_male-medium", "speed": 2.0},
                "my-persona": {"voice": "en_US-joe-medium", "speed": 1.5},
            },
        }))

        # Sessions
        (sessions_dir / "project-a.json").write_text(json.dumps({
            "muted": False, "persona": "my-persona",
        }))
        (sessions_dir / "project-b.json").write_text(json.dumps({
            "muted": True,
        }))

        # Settings.json
        (claude_dir / "settings.json").write_text(json.dumps({
            "hooks": {
                "Stop": [{
                    "matcher": "*",
                    "hooks": [{"type": "command", "command": str(hooks_dir / "speak-response.sh")}],
                }],
            },
        }))

        return {
            "home": home,
            "claude_dir": claude_dir,
            "hooks_dir": hooks_dir,
            "commands_dir": commands_dir,
            "tts_dir": tts_dir,
            "sessions_dir": sessions_dir,
        }

    def _run_upgrade_file_ops(self, paths: dict[str, Path]) -> None:
        """Execute the file operations that do_install --upgrade performs.

        This mirrors the actual code path without calling do_install directly
        (which would try to install packages, download voices, etc.).
        """
        repo = _repo_dir()
        tts_dir = paths["tts_dir"]
        hooks_dir = paths["hooks_dir"]
        commands_dir = paths["commands_dir"]

        # Step 1: Deploy hooks and commands from repo
        for category, files in inst.MANIFEST.items():
            repo_subdir = "hooks" if category == "hooks" else "commands"
            install_dir = hooks_dir if category == "hooks" else commands_dir
            for name in files:
                src = repo / repo_subdir / name
                dst = install_dir / name
                if src.exists():
                    shutil.copy(src, dst)
                    dst.chmod(0o755)

        # Step 2: Clean up legacy scripts
        for script_name in inst.LEGACY_SCRIPTS:
            old_script = tts_dir / script_name
            if old_script.exists():
                old_script.unlink()

        # Step 3: Deploy compatibility shims
        for shim_name, cli_cmd in inst.COMPAT_SHIMS.items():
            shim_path = tts_dir / shim_name
            shim_path.write_text(inst._make_compat_shim(cli_cmd))
            shim_path.chmod(0o755)

    def test_hooks_become_shims(self, tmp_path):
        paths = self._setup_v6_install(tmp_path)
        self._run_upgrade_file_ops(paths)

        hook = (paths["hooks_dir"] / "speak-response.sh").read_text()
        assert "exec claude-tts speak --from-hook" in hook
        assert "jq" not in hook
        assert "tts-lib.sh" not in hook

    def test_commands_call_cli(self, tmp_path):
        paths = self._setup_v6_install(tmp_path)
        self._run_upgrade_file_ops(paths)

        for name in inst.MANIFEST["commands"]:
            content = (paths["commands_dir"] / name).read_text()
            assert "claude-tts" in content, f"{name} doesn't call claude-tts"
            assert "$HOME/.claude-tts/" not in content, f"{name} still references old path"

    def test_legacy_scripts_removed(self, tmp_path):
        paths = self._setup_v6_install(tmp_path)
        self._run_upgrade_file_ops(paths)

        shimmed = set(inst.COMPAT_SHIMS)
        for script in V6_SCRIPTS - shimmed:
            assert not (paths["tts_dir"] / script).exists(), (
                f"Legacy script not removed: {script}"
            )
        # Shimmed scripts should exist but contain the forwarding shim
        for script in shimmed:
            shim_path = paths["tts_dir"] / script
            assert shim_path.exists(), f"Compat shim not deployed: {script}"
            content = shim_path.read_text()
            assert "exec claude-tts" in content, f"Shim {script} doesn't forward to CLI"
            assert "DEPRECATED" in content, f"Shim {script} missing deprecation notice"

    def test_builder_scripts_preserved(self, tmp_path):
        paths = self._setup_v6_install(tmp_path)
        self._run_upgrade_file_ops(paths)

        for script in SURVIVOR_SCRIPTS:
            assert (paths["tts_dir"] / script).exists(), (
                f"Builder script incorrectly removed: {script}"
            )

    def test_config_preserved(self, tmp_path):
        paths = self._setup_v6_install(tmp_path)
        self._run_upgrade_file_ops(paths)

        config = json.loads((paths["tts_dir"] / "config.json").read_text())
        assert config["active_persona"] == "my-persona"
        assert config["installed_version"] == "6.2.0"
        assert "my-persona" in config["personas"]

    def test_sessions_preserved(self, tmp_path):
        paths = self._setup_v6_install(tmp_path)
        self._run_upgrade_file_ops(paths)

        session_a = json.loads(
            (paths["sessions_dir"] / "project-a.json").read_text()
        )
        assert session_a["persona"] == "my-persona"
        assert session_a["muted"] is False

        session_b = json.loads(
            (paths["sessions_dir"] / "project-b.json").read_text()
        )
        assert session_b["muted"] is True

    def test_settings_json_preserved(self, tmp_path):
        paths = self._setup_v6_install(tmp_path)
        self._run_upgrade_file_ops(paths)

        settings = json.loads(
            (paths["claude_dir"] / "settings.json").read_text()
        )
        # Settings.json should not be touched by file ops
        # (the hook registration logic is separate)
        assert "hooks" in settings
        assert "Stop" in settings["hooks"]

    def test_idempotent_reupgrade(self, tmp_path):
        """Running upgrade twice should produce identical results."""
        paths = self._setup_v6_install(tmp_path)
        self._run_upgrade_file_ops(paths)

        # Snapshot state after first upgrade
        hook_content_1 = (paths["hooks_dir"] / "speak-response.sh").read_text()
        cmd_content_1 = (paths["commands_dir"] / "tts-mute.md").read_text()
        config_1 = (paths["tts_dir"] / "config.json").read_text()

        # Run upgrade again
        self._run_upgrade_file_ops(paths)

        hook_content_2 = (paths["hooks_dir"] / "speak-response.sh").read_text()
        cmd_content_2 = (paths["commands_dir"] / "tts-mute.md").read_text()
        config_2 = (paths["tts_dir"] / "config.json").read_text()

        assert hook_content_1 == hook_content_2
        assert cmd_content_1 == cmd_content_2
        assert config_1 == config_2
