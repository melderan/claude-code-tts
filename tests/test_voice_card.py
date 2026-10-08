"""The voice card: one file per session saying what it sounds like, for readers that must not shell out.

Contract in docs/voice-card.md. A status line reads `~/.claude-tts/voice.d/<session>.json` and
nothing else, so these tests pin the fields, when the card is rewritten, and that writing it
never breaks the caller.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_code_tts import __version__
from claude_code_tts import config as config_mod
from claude_code_tts.config import load_config, session_del, session_set, voice_card_path


@pytest.fixture
def tts_home(tmp_path):
    home = tmp_path / ".claude-tts"
    home.mkdir()
    with patch.object(config_mod, "TTS_CONFIG_DIR", home), \
         patch.object(config_mod, "TTS_CONFIG_FILE", home / "config.json"), \
         patch.object(config_mod, "TTS_SESSIONS_DIR", home / "sessions.d"), \
         patch.object(config_mod, "VOICE_CARDS_DIR", home / "voice.d"):
        yield home


def _config(home: Path, personas: dict, project_personas: dict | None = None) -> None:
    (home / "config.json").write_text(json.dumps({
        "mode": "queue",
        "active_persona": "claude-prime",
        "personas": personas,
        "project_personas": project_personas or {},
    }))


def _card(sid: str) -> dict:
    return json.loads(voice_card_path(sid).read_text())


class TestFields:
    def test_a_resolved_session_writes_its_card(self, tts_home):
        _config(tts_home,
                {"room-heart": {"voice_mlx": "mlx-community/Kokoro-82M-bf16", "speaker_mlx": "af_heart", "speed": 1.6}},
                {"-Users-dev-room": "room-heart"})
        cfg = load_config("-Users-dev-room")
        card = _card("-Users-dev-room")
        assert card == {
            "schema": 1,
            "session": "-Users-dev-room",
            "name": "",
            "persona": "room-heart",
            "backend": "mlx",
            "voice": "mlx-community/Kokoro-82M-bf16:af_heart",
            "speed": 1.6,
            "muted": False,
            "intermediate": True,
            "mode": "queue",
            "written_at": card["written_at"],
            "claude_tts": __version__,
        }
        assert card["written_at"].endswith("Z") and len(card["written_at"]) == 20
        assert cfg.active_persona == "room-heart"

    def test_piper_is_the_backend_when_no_other_voice_is_set(self, tts_home):
        _config(tts_home, {"claude-prime": {"voice": "en_US-amy-medium", "speed": 2.0}})
        load_config("-Users-dev-a")
        card = _card("-Users-dev-a")
        assert (card["backend"], card["voice"], card["speed"]) == ("piper", "en_US-amy-medium", 2.0)

    def test_kokoro_wins_over_mlx_and_sherpa_as_the_player_does(self, tts_home):
        _config(tts_home, {"claude-prime": {
            "voice_kokoro": "bm_george", "voice_mlx": "mlx-community/x", "voice_sherpa": "vctk-vits", "speaker_sherpa": 7,
        }})
        load_config("-Users-dev-b")
        assert (_card("-Users-dev-b")["backend"], _card("-Users-dev-b")["voice"]) == ("kokoro", "bm_george")

    def test_sherpa_carries_its_speaker(self, tts_home):
        _config(tts_home, {"claude-prime": {"voice_sherpa": "vctk-vits", "speaker_sherpa": 42}})
        load_config("-Users-dev-c")
        assert _card("-Users-dev-c")["voice"] == "vctk-vits:42"

    def test_the_session_file_overrides_show_in_the_card(self, tts_home):
        _config(tts_home, {"claude-prime": {"voice": "en_US-amy-medium", "speed": 2.0}})
        (tts_home / "sessions.d").mkdir()
        (tts_home / "sessions.d" / "-Users-dev-d.json").write_text(json.dumps({"speed": 3.0, "muted": True}))
        load_config("-Users-dev-d")
        card = _card("-Users-dev-d")
        assert (card["speed"], card["muted"]) == (3.0, True)


class TestWhenItIsWritten:
    def test_a_session_change_rewrites_the_card_at_once(self, tts_home):
        _config(tts_home, {"claude-prime": {"voice": "en_US-amy-medium", "speed": 2.0}})
        load_config("-Users-dev-e")
        assert _card("-Users-dev-e")["speed"] == 2.0
        session_set("-Users-dev-e", "speed", 2.5)
        assert _card("-Users-dev-e")["speed"] == 2.5
        session_del("-Users-dev-e", "speed")
        assert _card("-Users-dev-e")["speed"] == 2.0

    def test_no_config_file_still_writes_a_card_with_the_defaults(self, tts_home):
        load_config("-Users-dev-f")
        card = _card("-Users-dev-f")
        assert (card["persona"], card["backend"], card["mode"]) == ("claude-prime", "piper", "direct")

    def test_an_empty_session_id_writes_nothing(self, tts_home):
        _config(tts_home, {"claude-prime": {"voice": "en_US-amy-medium"}})
        load_config("")
        assert not (tts_home / "voice.d").exists()

    def test_the_write_is_one_step_and_leaves_no_temp_file(self, tts_home):
        _config(tts_home, {"claude-prime": {"voice": "en_US-amy-medium"}})
        load_config("-Users-dev-g")
        assert [p.name for p in (tts_home / "voice.d").iterdir()] == ["-Users-dev-g.json"]

    def test_an_unwritable_card_directory_does_not_break_the_caller(self, tts_home):
        _config(tts_home, {"claude-prime": {"voice": "en_US-amy-medium"}})
        (tts_home / "voice.d").write_text("a file where the directory should be")
        cfg = load_config("-Users-dev-h")
        assert cfg.active_persona == "claude-prime"
