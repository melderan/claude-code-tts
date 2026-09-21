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
    def test_removes_all_three_and_prunes(self):
        s: dict = {"env": {"X": "1"}}
        ensure_tts_hooks(s, HOOKS_DIR)
        assert remove_tts_hooks(s) == 3
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
