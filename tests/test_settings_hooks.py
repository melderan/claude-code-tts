"""settings.json hook registry: find ours anywhere, add once, remove all, write safely.

Before v9.10.3 the installer looked only at the first hook of each entry (a
TTS hook grouped second was invisible and got duplicated), uninstall removed
only the Stop entry (PostToolUse and UserPromptSubmit kept pointing at deleted
scripts), and writes escaped the user's non-ASCII.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from claude_code_tts.install import (
    TTS_HOOK_EVENTS,
    ensure_tts_hooks,
    remove_tts_hooks,
    write_settings,
)

HOOKS_DIR = Path("/home/u/.claude/hooks")


def _user_hook(cmd: str = "~/bin/notify.sh") -> dict:
    return {"type": "command", "command": cmd}


class TestEnsureTtsHooks:
    def test_fresh_settings_get_all_three_events(self):
        s: dict = {}
        assert ensure_tts_hooks(s, HOOKS_DIR) is True
        assert set(s["hooks"]) == set(TTS_HOOK_EVENTS)
        stop = s["hooks"]["Stop"][0]["hooks"][0]
        assert stop["command"].endswith("speak-response.sh")
        assert stop["async"] is True
        assert "async" not in s["hooks"]["UserPromptSubmit"][0]["hooks"][0]

    def test_one_event_carries_two_of_our_hooks_each_added_once(self):
        """UserPromptSubmit runs the tone context and, separately, the supersede."""
        s = {"hooks": {"UserPromptSubmit": [{"matcher": "*", "hooks": [
            {"type": "command", "command": str(HOOKS_DIR / "voice-context.sh"), "timeout": 5},
        ]}]}}
        assert ensure_tts_hooks(s, HOOKS_DIR) is True
        commands = [h["command"] for e in s["hooks"]["UserPromptSubmit"] for h in e["hooks"]]
        assert commands == [str(HOOKS_DIR / "voice-context.sh"), str(HOOKS_DIR / "prompt-submitted.sh")]
        added = s["hooks"]["UserPromptSubmit"][1]["hooks"][0]
        assert added["timeout"] == 5 and "async" not in added  # written before the turn queues anything
        assert ensure_tts_hooks(s, HOOKS_DIR) is False

    def test_idempotent(self):
        s: dict = {}
        ensure_tts_hooks(s, HOOKS_DIR)
        before = copy.deepcopy(s)
        assert ensure_tts_hooks(s, HOOKS_DIR) is False
        assert s == before

    def test_finds_tts_hook_grouped_second(self):
        s = {"hooks": {"Stop": [{"matcher": "*", "hooks": [
            _user_hook(), {"type": "command", "command": str(HOOKS_DIR / "speak-response.sh"), "async": True},
        ]}]}}
        ensure_tts_hooks(s, HOOKS_DIR)
        assert len(s["hooks"]["Stop"]) == 1, "grouped TTS hook was duplicated"

    def test_marks_existing_speech_hooks_async_in_place(self):
        s = {"hooks": {"PostToolUse": [{"matcher": "*", "hooks": [
            {"type": "command", "command": str(HOOKS_DIR / "speak-intermediate.sh"), "timeout": 30},
        ]}]}}
        assert ensure_tts_hooks(s, HOOKS_DIR) is True
        assert s["hooks"]["PostToolUse"][0]["hooks"][0]["async"] is True
        assert len(s["hooks"]["PostToolUse"]) == 1

    def test_user_hooks_and_other_keys_untouched(self):
        s = {"model": "opus", "hooks": {"Stop": [{"matcher": "*", "hooks": [_user_hook()]}],
                                          "PreToolUse": [{"matcher": "Bash", "hooks": [_user_hook("~/bin/guard.sh")]}]}}
        ensure_tts_hooks(s, HOOKS_DIR)
        assert s["model"] == "opus"
        assert s["hooks"]["PreToolUse"] == [{"matcher": "Bash", "hooks": [_user_hook("~/bin/guard.sh")]}]
        assert s["hooks"]["Stop"][0] == {"matcher": "*", "hooks": [_user_hook()]}


class TestRemoveTtsHooks:
    def test_removes_all_four_and_prunes(self):
        s: dict = {"env": {"X": "1"}}
        ensure_tts_hooks(s, HOOKS_DIR)
        assert remove_tts_hooks(s) == 4
        assert "hooks" not in s
        assert s["env"] == {"X": "1"}

    def test_keeps_user_hooks_in_shared_group(self):
        s = {"hooks": {"Stop": [{"matcher": "*", "hooks": [
            _user_hook(), {"type": "command", "command": str(HOOKS_DIR / "speak-response.sh")},
        ]}]}}
        assert remove_tts_hooks(s) == 1
        assert s["hooks"]["Stop"] == [{"matcher": "*", "hooks": [_user_hook()]}]

    def test_recognises_direct_cli_command(self):
        s = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "claude-tts speak --from-hook --hook-type stop"}]}]}}
        assert remove_tts_hooks(s) == 1
        assert "hooks" not in s

    def test_nothing_to_remove(self):
        s = {"hooks": {"Stop": [{"hooks": [_user_hook()]}]}}
        before = copy.deepcopy(s)
        assert remove_tts_hooks(s) == 0
        assert s == before


class TestWriteSettings:
    def test_keeps_non_ascii_and_mode_and_newline(self, tmp_path):
        path = tmp_path / "settings.json"
        path.write_text("{}")
        path.chmod(0o600)
        write_settings({"statusLine": {"command": "echo 'ready 🐳 grüß'"}}, path)
        text = path.read_text()
        assert "🐳 grüß" in text and "\\u" not in text
        assert text.endswith("}\n")
        assert path.stat().st_mode & 0o777 == 0o600
        assert json.loads(text)["statusLine"]["command"] == "echo 'ready 🐳 grüß'"
        assert not list(tmp_path.glob("*.tmp"))

    def test_creates_parent_dirs(self, tmp_path):
        path = tmp_path / "deep" / "settings.json"
        write_settings({"a": 1}, path)
        assert json.loads(path.read_text()) == {"a": 1}


def test_rewriting_settings_under_a_running_session_is_said_out_loud():
    """2026-10-01: the installer rewrote settings.json while a session was open; that session
    registered the hooks twice and spoke every reply twice until restarted. The notice names
    the consequence and the remedy; the install flow prints it whenever it writes an existing file."""
    import inspect

    from claude_code_tts import install

    assert "spoken twice" in install.SETTINGS_REWRITE_NOTICE
    assert "fresh session" in install.SETTINGS_REWRITE_NOTICE
    src = inspect.getsource(install)
    # Both branches that rewrite an existing settings file say it; creating a new file does not.
    assert src.count("warn(SETTINGS_REWRITE_NOTICE)") == 2


class TestHooksRegisteredElsewhere:
    """2026-10-01: a kit room starts `claude --settings house.json`, which registers our hooks; the
    installer saw only settings.json, added a second registration, and every hook fired twice."""

    def test_the_environment_names_the_owner(self, monkeypatch):
        from claude_code_tts import install

        monkeypatch.setenv(install.HOOKS_MANAGED_ENV, "/home/x/.config/claude/house.json")
        assert install.tts_hooks_registered_elsewhere() == Path("/home/x/.config/claude/house.json")

    def test_a_settings_file_passed_to_a_running_claude_is_scanned(self, tmp_path, monkeypatch):
        from claude_code_tts import install

        monkeypatch.delenv(install.HOOKS_MANAGED_ENV, raising=False)
        house = tmp_path / "house.json"
        house.write_text(json.dumps({"hooks": {"Stop": [{"matcher": "*", "hooks": [
            {"type": "command", "command": "~/.claude/hooks/speak-response.sh"}]}]}}))
        monkeypatch.setattr(install, "CLAUDE_DIR", tmp_path / "nowhere")
        monkeypatch.setattr(install, "SETTINGS_FILE", tmp_path / "nowhere" / "settings.json")
        monkeypatch.setattr(install.Path, "home", classmethod(lambda cls: tmp_path / "home"))
        fake_ps = f"claude --dangerously-skip-permissions --settings {house}\nbash\n"
        monkeypatch.setattr(install.subprocess, "run",
                            lambda *a, **k: type("R", (), {"stdout": fake_ps})())
        assert install.tts_hooks_registered_elsewhere(cwd=tmp_path) == house

    def test_other_files_without_our_hooks_do_not_block(self, tmp_path, monkeypatch):
        from claude_code_tts import install

        monkeypatch.delenv(install.HOOKS_MANAGED_ENV, raising=False)
        other = tmp_path / ".claude" / "settings.local.json"
        other.parent.mkdir()
        other.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "mine.sh"}]}]}}))
        monkeypatch.setattr(install, "CLAUDE_DIR", tmp_path / "nowhere")
        monkeypatch.setattr(install, "SETTINGS_FILE", tmp_path / "nowhere" / "settings.json")
        monkeypatch.setattr(install.Path, "home", classmethod(lambda cls: tmp_path / "home"))
        monkeypatch.setattr(install.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": "bash\n"})())
        assert install.tts_hooks_registered_elsewhere(cwd=tmp_path) is None


class TestManagedFileReport:
    """A kit's file owns registration: the installer names each of our hooks it lacks."""

    def test_names_each_missing_script_one_line_each(self, tmp_path, capsys):
        from claude_code_tts.install import missing_tts_hooks, report_missing_tts_hooks
        kit = tmp_path / "house.json"
        kit.write_text(json.dumps({"hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": "~/.claude/hooks/speak-response.sh"}]}],
            "PostToolUse": [{"hooks": [{"type": "command", "command": "~/.claude/hooks/speak-intermediate.sh"}]}],
            "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "~/.local/bin/post unread"}]}],
        }}))
        assert missing_tts_hooks(kit) == [
            ("UserPromptSubmit", "voice-context.sh"), ("UserPromptSubmit", "prompt-submitted.sh"),
        ]
        assert report_missing_tts_hooks(kit) == 2
        out = capsys.readouterr().out
        assert f"UserPromptSubmit: prompt-submitted.sh not registered in {kit}" in out
        assert len([ln for ln in out.splitlines() if "not registered in" in ln]) == 2

    def test_unreadable_file_says_so_and_names_nothing(self, tmp_path, capsys):
        from claude_code_tts.install import missing_tts_hooks, report_missing_tts_hooks
        assert missing_tts_hooks(tmp_path / "absent.json") is None
        assert report_missing_tts_hooks(tmp_path / "absent.json") == 0
        assert "Cannot read" in capsys.readouterr().out

    def test_the_installer_reports_when_another_file_owns_registration(self):
        import inspect

        from claude_code_tts import install
        src = inspect.getsource(install)
        owner = src.index("elsewhere = tts_hooks_registered_elsewhere()")
        branch = src[owner:src.index("elif upgrade:", owner)]
        assert "report_missing_tts_hooks(elsewhere)" in branch
