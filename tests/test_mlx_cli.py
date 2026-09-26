"""Operator surfaces of the mlx backend: `claude-tts mlx ...`, `speak --voice-mlx`, `persona add --mlx`."""

from __future__ import annotations

import json
import re
from unittest.mock import patch

import pytest

from claude_code_tts.cli import main
from claude_code_tts.mlx_catalog import (
    CATALOG,
    KITTEN_VOICES,
    KOKORO_VOICES,
    QWEN3_SPEAKERS,
    default_lang_for,
    entry_for_repo,
    kokoro_lang_for,
    list_ids,
    resolve_model,
    voices_for,
)
from tests.test_cli import patched_env, tts_home  # noqa: F401  (fixtures)


class TestCatalog:
    def test_every_entry_is_permissive_and_checked(self):
        for entry in CATALOG.values():
            assert entry["license_weights"] in {"apache-2.0", "mit"}, entry["id"]
            assert entry["checked"].startswith("2026-"), entry["id"]
            assert entry["hf_repo"].count("/") == 1
            assert entry["size_mb"] > 0

    def test_voice_lists_and_language_from_prefix(self):
        assert len(KOKORO_VOICES) == 54 and len(set(KOKORO_VOICES)) == 54
        assert all(kokoro_lang_for(v) for v in KOKORO_VOICES)
        assert kokoro_lang_for("bm_george") == "b" and kokoro_lang_for("jf_alpha") == "j"
        assert kokoro_lang_for("Vivian") == "" and kokoro_lang_for("xz_nobody") == ""
        assert voices_for("kokoro") == KOKORO_VOICES and voices_for("kokoro-4bit") == KOKORO_VOICES
        assert voices_for("mlx-community/kitten-tts-nano-0.8") == KITTEN_VOICES
        assert voices_for("qwen3-tts-1.7b") == QWEN3_SPEAKERS
        assert voices_for("dia-1.6b") == [] and voices_for("nobody/nothing") == []
        assert default_lang_for("kokoro", "bm_george") == "b"
        assert default_lang_for("kokoro", "") == "a"
        assert default_lang_for("kokoro", "Ryan") == "a"
        assert default_lang_for("kitten-nano", "Bella") == ""
        assert default_lang_for("nobody/nothing", "x") == ""

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

    def test_british_voice_gets_british_language(self, tts_home, patched_env):  # noqa: F811
        main(["persona", "add", "g", "--mlx", "kokoro-8bit", "--mlx-voice", "bm_george"])
        entry = json.loads((tts_home / ".claude-tts" / "config.json").read_text())["personas"]["g"]
        assert entry["voice_mlx"] == "mlx-community/Kokoro-82M-8bit"
        assert entry["speaker_mlx"] == "bm_george" and entry["lang_mlx"] == "b"

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

    def test_explicit_speaker_wins_and_sets_its_language(self, tts_home, patched_env):  # noqa: F811
        with patch("claude_code_tts.audio.generate_speech") as gen, patch("claude_code_tts.audio.play_audio"):
            gen.return_value = tts_home / "o.wav"
            main(["speak", "--voice-mlx", "kokoro", "--speaker-mlx", "bm_george", "hi"])
        assert gen.call_args.kwargs["speaker_mlx"] == "bm_george"
        assert gen.call_args.kwargs["lang_mlx"] == "b"
        with patch("claude_code_tts.audio.generate_speech") as gen, patch("claude_code_tts.audio.play_audio"):
            gen.return_value = tts_home / "o.wav"
            main(["speak", "--voice-mlx", "kokoro", "--speaker-mlx", "bm_george", "--lang-mlx", "a", "hi"])
        assert gen.call_args.kwargs["lang_mlx"] == "a"

    def test_speaker_flag_applies_to_an_mlx_persona(self, tts_home, patched_env):  # noqa: F811
        main(["persona", "add", "k", "--mlx", "kokoro"])
        cfg_file = tts_home / ".claude-tts" / "config.json"
        config = json.loads(cfg_file.read_text())
        config["active_persona"] = "k"  # global, since the speak path resolves the session on its own
        cfg_file.write_text(json.dumps(config))
        with patch("claude_code_tts.audio.generate_speech") as gen, patch("claude_code_tts.audio.play_audio"):
            gen.return_value = tts_home / "o.wav"
            main(["speak", "--speaker-mlx", "bf_emma", "hi"])
        assert gen.call_args.kwargs["voice_mlx"] == "mlx-community/Kokoro-82M-bf16"
        assert gen.call_args.kwargs["speaker_mlx"] == "bf_emma" and gen.call_args.kwargs["lang_mlx"] == "b"

    def test_piper_voice_flag_clears_mlx_from_persona(self, tts_home, patched_env):  # noqa: F811
        main(["persona", "add", "k", "--mlx", "kokoro", "--session"])
        with patch("claude_code_tts.audio.generate_speech") as gen, patch("claude_code_tts.audio.play_audio"):
            gen.return_value = tts_home / "o.wav"
            main(["speak", "--voice", "en_US-joe-medium", "hi"])
        assert gen.call_args.kwargs["voice_mlx"] == ""


def test_hf_cache_dir_order(monkeypatch, tmp_path):
    from claude_code_tts.cli import _hf_cache_dir
    for var in ("HF_HUB_CACHE", "HF_HOME", "XDG_CACHE_HOME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    assert _hf_cache_dir() == tmp_path / ".cache" / "huggingface" / "hub"
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert _hf_cache_dir() == tmp_path / "xdg" / "huggingface" / "hub"
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hfhome"))
    assert _hf_cache_dir() == tmp_path / "hfhome" / "hub"
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "hub"))
    assert _hf_cache_dir() == tmp_path / "hub"


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


class TestAuditionMlx:
    """`claude-tts audition --mlx <model>` cycles a catalog model's named voices."""

    def _run(self, home, argv, keys, generate):
        keys = iter(keys)
        with patch("claude_code_tts.config.MLX_VENV_DIR", home / "venvs" / "mlx"), \
             patch("claude_code_tts.audio.generate_speech", side_effect=generate), \
             patch("claude_code_tts.audio.detect_player", return_value=None), \
             patch("builtins.input", return_value=""), \
             patch("claude_code_tts.cli.sys.stdin") as stdin:
            stdin.fileno.return_value = 0
            stdin.read.side_effect = lambda n=1: next(keys)
            with patch("termios.tcgetattr", return_value=None), patch("termios.tcsetattr"), patch("tty.setraw"):
                main(argv)

    def test_refuses_without_venv(self, tts_home, patched_env, capsys):  # noqa: F811
        with patch("claude_code_tts.config.MLX_VENV_DIR", tts_home / "venvs" / "mlx"), pytest.raises(SystemExit) as exc:
            main(["audition", "--mlx", "kokoro"])
        assert exc.value.code == 1
        assert "--enable-mlx" in capsys.readouterr().out

    def test_model_without_named_voices_is_refused(self, tts_home, patched_env, capsys):  # noqa: F811
        venv = tts_home / "venvs" / "mlx"
        (venv / "bin").mkdir(parents=True)
        (venv / "bin" / "python").write_text("")
        with patch("claude_code_tts.config.MLX_VENV_DIR", venv), pytest.raises(SystemExit) as exc:
            main(["audition", "--mlx", "dia-1.6b"])
        assert exc.value.code == 1
        assert "No named voices" in capsys.readouterr().out

    def test_plays_filtered_voices_in_their_language_and_quits(self, tts_home, patched_env, capsys):  # noqa: F811
        venv = tts_home / "venvs" / "mlx"
        (venv / "bin").mkdir(parents=True)
        (venv / "bin" / "python").write_text("")
        calls: list[dict] = []

        def generate(text, **kwargs):
            calls.append(kwargs)
            return tts_home / "o.wav"

        # first voice: Enter (play) then Enter (next); second voice: Enter (play) then q (quit)
        self._run(tts_home, ["audition", "--mlx", "kokoro", "--filter", "bm_"], ["\r", "\r", "\r", "q"], generate)
        assert [c["speaker_mlx"] for c in calls] == ["bm_daniel", "bm_fable"]
        assert all(c["lang_mlx"] == "b" and c["voice_mlx"] == "mlx-community/Kokoro-82M-bf16" for c in calls)
        out = capsys.readouterr().out
        assert "Found 4 voices (filter: bm_)" in out and "Daniel (bm_daniel) [4 remaining]" in out
