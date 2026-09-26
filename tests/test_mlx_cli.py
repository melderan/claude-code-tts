"""Operator surfaces of the mlx backend: `claude-tts mlx ...`, `speak --voice-mlx`, `persona add --mlx`."""

from __future__ import annotations

import json
import re
from unittest.mock import patch

import pytest

from claude_code_tts.cli import main
from claude_code_tts.mlx_catalog import CATALOG, entry_for_repo, list_ids, resolve_model
from tests.test_cli import patched_env, tts_home  # noqa: F401  (fixtures)


class TestCatalog:
    def test_every_entry_is_permissive_and_checked(self):
        for entry in CATALOG.values():
            assert entry["license_weights"] in {"apache-2.0", "mit"}, entry["id"]
            assert entry["checked"].startswith("2026-"), entry["id"]
            assert entry["hf_repo"].count("/") == 1
            assert entry["size_mb"] > 0

    def test_resolve_and_lookup(self):
        assert resolve_model("kokoro") == "mlx-community/Kokoro-82M-bf16"
        assert resolve_model("someone/Their-Model") == "someone/Their-Model"
        assert entry_for_repo("mlx-community/Kokoro-82M-bf16")["id"] == "kokoro"
        assert entry_for_repo("nobody/nothing") is None
        assert list_ids()[0] == "kokoro"


class TestMlxCommand:
    def test_list_available_shows_licenses(self, tts_home, patched_env, capsys):  # noqa: F811
        main(["mlx", "list-available"])
        out = capsys.readouterr().out
        for entry in CATALOG.values():
            assert entry["id"] in out and entry["hf_repo"] in out and entry["license_weights"] in out
        assert "--speaker-mlx af_heart --lang-mlx a" in out

    def test_status_without_venv_points_at_the_installer(self, tts_home, patched_env, monkeypatch, capsys):  # noqa: F811
        monkeypatch.setattr("claude_code_tts.config.MLX_VENV_DIR", tts_home / "venvs" / "mlx")
        monkeypatch.setenv("HF_HUB_CACHE", str(tts_home / "hf"))
        main(["mlx", "status"])
        out = capsys.readouterr().out
        assert "NOT enabled (run: claude-tts-install --enable-mlx)" in out
        assert "kokoro" in out and "not fetched" in out
        assert "No persona uses mlx yet" in out

    def test_status_lists_cached_models_and_personas(self, tts_home, patched_env, monkeypatch, capsys):  # noqa: F811
        monkeypatch.setattr("claude_code_tts.config.MLX_VENV_DIR", tts_home / "venvs" / "mlx")
        cache = tts_home / "hf"
        (cache / "models--mlx-community--Kokoro-82M-bf16" / "snapshots" / "abc").mkdir(parents=True)
        monkeypatch.setenv("HF_HUB_CACHE", str(cache))
        main(["persona", "add", "k", "--mlx", "kokoro"])
        main(["mlx", "status"])
        out = capsys.readouterr().out
        assert re.search(r"kokoro\s+mlx-community/Kokoro-82M-bf16\s+cached\n", out)
        assert re.search(r"kitten-nano\s+mlx-community/kitten-tts-nano-0.8\s+not fetched\n", out)
        assert "k: mlx-community/Kokoro-82M-bf16 voice af_heart" in out

    def test_pull_without_venv_exits_2(self, tts_home, patched_env, monkeypatch, capsys):  # noqa: F811
        monkeypatch.setattr("claude_code_tts.config.MLX_VENV_DIR", tts_home / "venvs" / "mlx")
        with pytest.raises(SystemExit) as exc:
            main(["mlx", "pull", "kokoro"])
        assert exc.value.code == 2
        assert "--enable-mlx" in capsys.readouterr().out

    def test_pull_runs_snapshot_download_in_the_venv(self, tts_home, patched_env, monkeypatch, capsys):  # noqa: F811
        venv = tts_home / "venvs" / "mlx"
        (venv / "bin").mkdir(parents=True)
        (venv / "bin" / "python").write_text("")
        monkeypatch.setattr("claude_code_tts.config.MLX_VENV_DIR", venv)
        monkeypatch.setenv("HF_HUB_CACHE", str(tts_home / "hf"))
        with patch("claude_code_tts.cli.subprocess.run") as run:
            run.return_value.returncode = 0
            main(["mlx", "pull", "kokoro"])
        cmd = run.call_args.args[0]
        assert cmd[0] == str(venv / "bin" / "python")
        assert "snapshot_download('mlx-community/Kokoro-82M-bf16')" in cmd[2]
        assert "389 MB, apache-2.0" in capsys.readouterr().out

    def test_pull_skips_cached_model(self, tts_home, patched_env, monkeypatch, capsys):  # noqa: F811
        venv = tts_home / "venvs" / "mlx"
        (venv / "bin").mkdir(parents=True)
        (venv / "bin" / "python").write_text("")
        monkeypatch.setattr("claude_code_tts.config.MLX_VENV_DIR", venv)
        cache = tts_home / "hf"
        (cache / "models--mlx-community--Kokoro-82M-bf16" / "snapshots" / "abc").mkdir(parents=True)
        monkeypatch.setenv("HF_HUB_CACHE", str(cache))
        with patch("claude_code_tts.cli.subprocess.run") as run:
            main(["mlx", "pull", "kokoro"])
        run.assert_not_called()
        assert "already in" in capsys.readouterr().out


class TestPersonaAddMlx:
    def test_catalog_id_fills_repo_voice_and_lang(self, tts_home, patched_env, capsys):  # noqa: F811
        main(["persona", "add", "k", "--mlx", "kokoro"])
        entry = json.loads((tts_home / ".claude-tts" / "config.json").read_text())["personas"]["k"]
        assert entry["voice_mlx"] == "mlx-community/Kokoro-82M-bf16"
        assert entry["speaker_mlx"] == "af_heart" and entry["lang_mlx"] == "a"
        assert "not enabled on this machine" in capsys.readouterr().out

    def test_explicit_voice_and_repo(self, tts_home, patched_env):  # noqa: F811
        main(["persona", "add", "q", "--mlx", "mlx-community/Qwen3-TTS-12Hz-0.6B-CustomVoice-8bit", "--mlx-voice", "Ryan"])
        entry = json.loads((tts_home / ".claude-tts" / "config.json").read_text())["personas"]["q"]
        assert entry["voice_mlx"] == "mlx-community/Qwen3-TTS-12Hz-0.6B-CustomVoice-8bit"
        assert entry["speaker_mlx"] == "Ryan" and "lang_mlx" not in entry

    def test_mlx_voice_without_model_is_rejected(self, tts_home, patched_env):  # noqa: F811
        with pytest.raises(SystemExit) as exc:
            main(["persona", "add", "k", "--mlx-voice", "af_heart"])
        assert exc.value.code == 2


class TestSpeakVoiceMlx:
    def test_override_routes_to_mlx_with_catalog_defaults(self, tts_home, patched_env, capsys):  # noqa: F811
        with patch("claude_code_tts.audio.generate_speech") as gen, patch("claude_code_tts.audio.play_audio") as play:
            gen.return_value = tts_home / "o.wav"
            main(["speak", "--voice-mlx", "kokoro", "hello there"])
        kwargs = gen.call_args.kwargs
        assert kwargs["voice_mlx"] == "mlx-community/Kokoro-82M-bf16"
        assert kwargs["speaker_mlx"] == "af_heart" and kwargs["lang_mlx"] == "a"
        assert kwargs["voice_path"] is None and kwargs["voice_sherpa"] == "" and kwargs["voice_kokoro"] == ""
        play.assert_called_once()
        assert "Voice: mlx/mlx-community/Kokoro-82M-bf16 af_heart lang a" in capsys.readouterr().out

    def test_explicit_speaker_wins(self, tts_home, patched_env):  # noqa: F811
        with patch("claude_code_tts.audio.generate_speech") as gen, patch("claude_code_tts.audio.play_audio"):
            gen.return_value = tts_home / "o.wav"
            main(["speak", "--voice-mlx", "kokoro", "--speaker-mlx", "bm_george", "--lang-mlx", "b", "hi"])
        assert gen.call_args.kwargs["speaker_mlx"] == "bm_george"
        assert gen.call_args.kwargs["lang_mlx"] == "b"

    def test_piper_voice_flag_clears_mlx_from_persona(self, tts_home, patched_env):  # noqa: F811
        main(["persona", "add", "k", "--mlx", "kokoro", "--session"])
        with patch("claude_code_tts.audio.generate_speech") as gen, patch("claude_code_tts.audio.play_audio"):
            gen.return_value = tts_home / "o.wav"
            main(["speak", "--voice", "en_US-joe-medium", "hi"])
        assert gen.call_args.kwargs["voice_mlx"] == ""


def test_describe_voice_names_mlx(tmp_path):
    import claude_code_tts.daemon as daemon_mod
    with patch.object(daemon_mod, "VOICES_DIR", tmp_path):
        assert daemon_mod.describe_voice("p", {"voice_mlx": "mlx-community/Kokoro-82M-bf16", "speaker_mlx": "af_heart"}) == "mlx:mlx-community/Kokoro-82M-bf16#af_heart"
        assert daemon_mod.describe_voice("p", {"voice_mlx": "m"}) == "mlx:m"


def test_generate_speech_accepts_the_mlx_keywords():
    """generate_speech takes the three mlx keywords cmd_speak and the daemon pass."""
    import inspect

    from claude_code_tts.audio import generate_speech
    params = inspect.signature(generate_speech).parameters
    assert {"voice_mlx", "speaker_mlx", "lang_mlx"} <= set(params)
