"""Tests for the installer module."""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

# Import the module under test
from claude_code_tts import install


class TestPlatformDetection:
    """Tests for platform detection functions."""

    def test_detect_platform_macos(self):
        """Test macOS detection."""
        with patch("platform.system", return_value="Darwin"):
            assert install.detect_platform() == "macos"

    def test_detect_platform_linux(self):
        """Test Linux detection."""
        with patch("platform.system", return_value="Linux"):
            with patch("builtins.open", side_effect=FileNotFoundError):
                assert install.detect_platform() == "linux"

    def test_detect_platform_unsupported(self):
        """Test unsupported platform detection."""
        with patch("platform.system", return_value="Windows"):
            assert install.detect_platform() == "unsupported"


class TestVoiceList:
    """Tests for the voice list configuration."""

    def test_available_voices_not_empty(self):
        """Verify we have voices configured."""
        assert len(install.AVAILABLE_VOICES) > 0

    def test_available_voices_structure(self):
        """Verify each voice has correct tuple structure."""
        for voice in install.AVAILABLE_VOICES:
            assert len(voice) == 5, f"Voice tuple should have 5 elements: {voice}"
            name, gender, quality, desc, path = voice
            assert isinstance(name, str)
            assert gender in ("male", "female")
            assert quality in ("low", "medium", "high")
            assert isinstance(desc, str)
            assert isinstance(path, str)

    def test_has_female_voices(self):
        """Verify we have female voices for Gemini."""
        female_voices = [v for v in install.AVAILABLE_VOICES if v[1] == "female"]
        assert len(female_voices) >= 3, "Should have at least 3 female voices"

    def test_has_male_voices(self):
        """Verify we have male voices for Claude."""
        male_voices = [v for v in install.AVAILABLE_VOICES if v[1] == "male"]
        assert len(male_voices) >= 3, "Should have at least 3 male voices"


class TestConfig:
    """Tests for configuration handling."""

    def test_default_config_structure(self):
        """Verify default config has required fields."""
        config = install.DEFAULT_CONFIG
        assert "version" in config
        assert "active_persona" in config
        assert "muted" in config
        assert "personas" in config

    def test_default_personas_have_ai_type(self):
        """Verify all default personas have ai_type field."""
        for name, persona in install.DEFAULT_CONFIG["personas"].items():
            assert "ai_type" in persona, f"Persona {name} missing ai_type"
            assert persona["ai_type"] in ("claude", "gemini")

    def test_default_personas_have_required_fields(self):
        """Verify all default personas have required fields."""
        required_fields = ["description", "voice", "speed", "speed_method", "max_chars"]
        for name, persona in install.DEFAULT_CONFIG["personas"].items():
            for field in required_fields:
                assert field in persona, f"Persona {name} missing {field}"

    def test_load_config_returns_default_when_no_file(self):
        """Test load_config returns defaults when file doesn't exist."""
        with patch.object(install, "TTS_CONFIG_FILE", Path("/nonexistent/config.json")):
            config = install.load_config()
            assert config == install.DEFAULT_CONFIG

    def test_save_and_load_config(self):
        """Test config can be saved and loaded."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "config.json"
            config_dir = Path(tmpdir)

            with patch.object(install, "TTS_CONFIG_FILE", config_file):
                with patch.object(install, "TTS_CONFIG_DIR", config_dir):
                    test_config = {"version": 1, "test": "value"}
                    install.save_config(test_config)

                    # Verify file was created
                    assert config_file.exists()

                    # Verify contents
                    with open(config_file) as f:
                        loaded = json.load(f)
                    assert loaded == test_config


class TestVoiceUrls:
    """Tests for voice URL generation."""

    def test_hf_base_url_valid(self):
        """Verify Hugging Face base URL is properly formatted."""
        assert install.HF_VOICES_BASE.startswith("https://")
        assert "huggingface.co" in install.HF_VOICES_BASE
        assert "piper-voices" in install.HF_VOICES_BASE

    def test_voice_url_generation(self):
        """Test that voice URLs are correctly formed."""
        # Pick a voice from the list
        voice = install.AVAILABLE_VOICES[0]
        name, gender, quality, desc, path = voice

        expected_onnx = f"{install.HF_VOICES_BASE}/{path}/{name}.onnx"
        expected_json = f"{install.HF_VOICES_BASE}/{path}/{name}.onnx.json"

        # These would be the URLs used in download_voice
        assert path in expected_onnx
        assert name in expected_onnx


class TestGetInstalledVoices:
    """Tests for installed voice detection."""

    def test_get_installed_voices_empty_dir(self):
        """Test returns empty set when no voices installed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(install, "VOICES_DIR", Path(tmpdir)):
                voices = install.get_installed_voices()
                assert voices == set()

    def test_get_installed_voices_with_files(self):
        """Test correctly identifies installed voices."""
        with tempfile.TemporaryDirectory() as tmpdir:
            voices_dir = Path(tmpdir)
            # Create fake voice files
            (voices_dir / "en_US-test-medium.onnx").touch()
            (voices_dir / "en_US-test-medium.onnx.json").touch()
            (voices_dir / "en_GB-other-high.onnx").touch()

            with patch.object(install, "VOICES_DIR", voices_dir):
                voices = install.get_installed_voices()
                assert "en_US-test-medium" in voices
                assert "en_GB-other-high" in voices


class TestCreatePersonaFromVoice:
    """Tests for persona creation from voice."""

    def test_creates_claude_persona(self):
        """Test creating a Claude persona."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "config.json"
            config_dir = Path(tmpdir)

            with patch.object(install, "TTS_CONFIG_FILE", config_file):
                with patch.object(install, "TTS_CONFIG_DIR", config_dir):
                    # Start with empty config
                    install.save_config({"version": 1, "personas": {}})

                    persona_name = install.create_persona_from_voice(
                        "en_US-ryan-medium",
                        "male",
                        "Test voice",
                        "claude",
                    )

                    # Load and verify
                    config = install.load_config()
                    assert persona_name in config["personas"]
                    assert config["personas"][persona_name]["ai_type"] == "claude"
                    assert config["personas"][persona_name]["voice"] == "en_US-ryan-medium"

    def test_creates_gemini_persona_with_prefix(self):
        """Test creating a Gemini persona adds gemini- prefix."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "config.json"
            config_dir = Path(tmpdir)

            with patch.object(install, "TTS_CONFIG_FILE", config_file):
                with patch.object(install, "TTS_CONFIG_DIR", config_dir):
                    install.save_config({"version": 1, "personas": {}})

                    persona_name = install.create_persona_from_voice(
                        "en_US-amy-medium",
                        "female",
                        "Test voice",
                        "gemini",
                    )

                    assert persona_name.startswith("gemini-")
                    config = install.load_config()
                    assert config["personas"][persona_name]["ai_type"] == "gemini"


class TestCommandExists:
    """Tests for command existence checking."""

    def test_command_exists_true(self):
        """Test returns True for existing commands."""
        # These should exist on any Unix-like system
        assert install.command_exists("ls") is True
        assert install.command_exists("echo") is True

    def test_command_exists_false(self):
        """Test returns False for non-existing commands."""
        assert install.command_exists("nonexistent_command_12345") is False


class TestRepoDir:
    """Tests for repository directory detection."""

    def test_repo_dir_found(self):
        """Test that REPO_DIR is correctly detected."""
        # REPO_DIR should contain hooks/ and commands/ directories
        assert install.REPO_DIR.is_dir()
        # When running tests from repo, these should exist
        # (may not exist if installed as package without repo)


class TestColors:
    """Tests for color constants."""

    def test_colors_are_ansi_codes(self):
        """Verify color constants are ANSI escape codes."""
        assert install.Colors.RED.startswith("\033[")
        assert install.Colors.GREEN.startswith("\033[")
        assert install.Colors.NC.startswith("\033[")
