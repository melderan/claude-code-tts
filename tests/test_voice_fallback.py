"""A persona whose Piper model is missing falls back to the default voice.

Before v9.10.2 that happened silently, so a new persona sounded like the old
one with nothing in daemon.log to explain it. Now the daemon says so once per
persona, and the installer can fetch a named voice without prompting.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import claude_code_tts.daemon as daemon_mod
import claude_code_tts.install as install_mod


class TestDaemonVoiceFallback:
    def _run(self, tmp_path: Path, persona_cfg: dict, calls: int = 1):
        (tmp_path / f"{daemon_mod.DEFAULT_VOICE}.onnx").write_bytes(b"model")
        logs: list[tuple[str, str]] = []
        gen_kwargs: list[dict] = []

        def fake_generate(text, **kwargs):
            gen_kwargs.append(kwargs)
            return kwargs["output_path"]

        with patch.object(daemon_mod, "VOICES_DIR", tmp_path), \
             patch.object(daemon_mod, "get_persona_config", return_value=persona_cfg), \
             patch.object(daemon_mod, "_generate_speech", side_effect=fake_generate), \
             patch.object(daemon_mod, "log", side_effect=lambda m, level="INFO": logs.append((level, m))), \
             patch.object(daemon_mod, "_missing_voice_warned", set()):
            for _ in range(calls):
                daemon_mod.daemon_generate_speech("hello there", "claude-connery", tmp_path / "out.wav")
        return logs, gen_kwargs

    def test_missing_voice_uses_default_and_warns(self, tmp_path):
        logs, gen = self._run(tmp_path, {"voice": "en_GB-northern_english_male-medium", "speed": 1.8})
        assert gen[0]["voice_path"] == tmp_path / f"{daemon_mod.DEFAULT_VOICE}.onnx"
        warns = [m for level, m in logs if level == "WARN"]
        assert len(warns) == 1
        assert "en_GB-northern_english_male-medium" in warns[0]
        assert "claude-connery" in warns[0]
        assert "claude-tts-install --voice en_GB-northern_english_male-medium" in warns[0]

    def test_warns_once_per_persona(self, tmp_path):
        logs, _ = self._run(tmp_path, {"voice": "en_GB-northern_english_male-medium"}, calls=3)
        assert sum(1 for level, _ in logs if level == "WARN") == 1

    def test_installed_voice_is_used_without_warning(self, tmp_path):
        (tmp_path / "en_GB-northern_english_male-medium.onnx").write_bytes(b"model")
        logs, gen = self._run(tmp_path, {"voice": "en_GB-northern_english_male-medium"})
        assert gen[0]["voice_path"] == tmp_path / "en_GB-northern_english_male-medium.onnx"
        assert not [m for level, m in logs if level == "WARN"]

    def test_sherpa_persona_does_not_warn_about_piper(self, tmp_path):
        logs, _ = self._run(tmp_path, {"voice": "not-a-piper-model", "voice_sherpa": "vctk-vits"})
        assert not [m for level, m in logs if level == "WARN"]

    def test_says_once_when_the_missing_voice_turns_up(self, tmp_path):
        """The log can tell fallback from persona without an ear (house room, 2026-09-23)."""
        voice = "en_GB-northern_english_male-medium"
        (tmp_path / f"{daemon_mod.DEFAULT_VOICE}.onnx").write_bytes(b"model")
        logs: list[tuple[str, str]] = []
        with patch.object(daemon_mod, "VOICES_DIR", tmp_path), \
             patch.object(daemon_mod, "log", side_effect=lambda m, level="INFO": logs.append((level, m))), \
             patch.object(daemon_mod, "_missing_voice_warned", set()):
            assert daemon_mod.resolve_piper_voice("p", {"voice": voice}) == (daemon_mod.DEFAULT_VOICE, True)
            assert daemon_mod.describe_voice("p", {"voice": voice}) == f"{daemon_mod.DEFAULT_VOICE} (fallback)"
            (tmp_path / f"{voice}.onnx").write_bytes(b"model")
            assert daemon_mod.resolve_piper_voice("p", {"voice": voice}) == (voice, False)
            assert daemon_mod.resolve_piper_voice("p", {"voice": voice}) == (voice, False)
            assert daemon_mod.describe_voice("p", {"voice": voice}) == voice
        assert [level for level, _ in logs] == ["WARN", "INFO"]
        assert "installed now" in logs[1][1]

    def test_describe_voice_names_the_engine(self, tmp_path):
        with patch.object(daemon_mod, "VOICES_DIR", tmp_path):
            assert daemon_mod.describe_voice("p", {"voice_kokoro": "af_heart"}) == "kokoro:af_heart"
            assert daemon_mod.describe_voice("p", {}, voice_kokoro_blend="af_heart:0.5,am_adam:0.5") == "kokoro:af_heart:0.5,am_adam:0.5"
            assert daemon_mod.describe_voice("p", {"voice_sherpa": "vctk-vits", "speaker_sherpa": 42}) == "sherpa:vctk-vits#42"
            assert daemon_mod.describe_voice("p", {"voice_sherpa": "kokoro-en"}) == "sherpa:kokoro-en"


class TestNamedVoiceDownload:
    def test_downloads_known_voice_and_reports_missing(self, tmp_path):
        downloaded: list[tuple[str, str]] = []
        with patch.object(install_mod, "VOICES_DIR", tmp_path), \
             patch.object(install_mod, "download_voice", side_effect=lambda n, p, dry_run=False: downloaded.append((n, p)) or True):
            missing = install_mod.do_download_named_voices(
                ["en_GB-northern_english_male-medium", "no-such-voice"]
            )
        assert downloaded == [("en_GB-northern_english_male-medium", "en/en_GB/northern_english_male/medium")]
        assert missing == 1

    def test_already_installed_is_skipped(self, tmp_path):
        (tmp_path / "en_GB-alan-medium.onnx").write_bytes(b"model")
        with patch.object(install_mod, "VOICES_DIR", tmp_path), \
             patch.object(install_mod, "download_voice") as dl:
            assert install_mod.do_download_named_voices(["en_GB-alan-medium"]) == 0
        dl.assert_not_called()


class TestInstallerDefaultSpeed:
    def test_apply_default_speed_touches_every_persona(self, tmp_path):
        cfg_dir = tmp_path / ".claude-tts"
        cfg_dir.mkdir()
        cfg = {"personas": {"a": {"speed": 2.0, "voice": "x"}, "b": {"speed": 1.5, "voice": "y"}}, "mode": "queue"}
        (cfg_dir / "config.json").write_text(json.dumps(cfg))
        with patch.object(install_mod, "TTS_CONFIG_DIR", cfg_dir), \
             patch.object(install_mod, "TTS_CONFIG_FILE", cfg_dir / "config.json"), \
             patch.object(install_mod, "TTS_SESSIONS_DIR", cfg_dir / "sessions.d"):
            names = install_mod.apply_default_speed(1.0)
        after = json.loads((cfg_dir / "config.json").read_text())
        assert sorted(names) == ["a", "b"]
        assert {p["speed"] for p in after["personas"].values()} == {1.0}
        assert after["mode"] == "queue"
