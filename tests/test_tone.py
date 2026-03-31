"""Tests for the content tone classifier."""

import pytest

from claude_code_tts.tone import (
    DEFAULT_TONE,
    TONES,
    ToneParams,
    classify_tone,
    get_tone,
)


class TestClassifyTone:
    """Test content tone classification."""

    def test_neutral_for_generic_text(self):
        assert classify_tone("Here is the file listing.").name == "neutral"

    def test_neutral_for_empty_text(self):
        assert classify_tone("").name == "neutral"

    # --- Excited ---
    def test_excited_all_tests_passing(self):
        assert classify_tone("All 269 tests passing!").name == "excited"

    def test_excited_tests_passed(self):
        assert classify_tone("All checks passed!").name == "excited"

    def test_excited_deployed_successfully(self):
        assert classify_tone("Deployed to production successfully").name == "excited"

    def test_excited_shipped(self):
        assert classify_tone("We shipped the feature!").name == "excited"

    def test_excited_hell_yes(self):
        assert classify_tone("Hell yes, that worked!").name == "excited"

    def test_excited_beautiful(self):
        assert classify_tone("This is beautiful work.").name == "excited"

    def test_excited_nailed_it(self):
        assert classify_tone("Nailed it on the first try.").name == "excited"

    def test_excited_lets_go(self):
        assert classify_tone("Let's go!").name == "excited"

    # --- Serious ---
    def test_serious_security_vulnerability(self):
        assert classify_tone("Found a security vulnerability in auth.").name == "serious"

    def test_serious_critical(self):
        assert classify_tone("CRITICAL: database connection pool exhausted").name == "serious"

    def test_serious_build_failed(self):
        assert classify_tone("The build failed on CI.").name == "serious"

    def test_serious_test_failed(self):
        assert classify_tone("3 tests failed in the integration suite.").name == "serious"

    def test_serious_data_loss(self):
        assert classify_tone("Risk of data loss if we proceed.").name == "serious"

    def test_serious_rollback(self):
        assert classify_tone("We need to rollback the deployment.").name == "serious"

    def test_serious_crash(self):
        assert classify_tone("The daemon crashed on startup.").name == "serious"

    # --- Warm ---
    def test_warm_thanks(self):
        assert classify_tone("Thanks for the help, brother.").name == "warm"

    def test_warm_appreciate(self):
        assert classify_tone("I really appreciate you doing this.").name == "warm"

    def test_warm_friend(self):
        assert classify_tone("You're a good friend.").name == "warm"

    def test_warm_brother(self):
        assert classify_tone("Hey brother, how's it going?").name == "warm"

    def test_warm_proud(self):
        assert classify_tone("I'm proud of what we built.").name == "warm"

    def test_warm_welcome(self):
        assert classify_tone("Welcome to the team!").name == "warm"

    # --- Focused ---
    def test_focused_debugging(self):
        assert classify_tone("Debugging the authentication flow.").name == "focused"

    def test_focused_investigating(self):
        assert classify_tone("Investigating the root cause now.").name == "focused"

    def test_focused_stack_trace(self):
        assert classify_tone("Looking at this stack trace...").name == "focused"

    def test_focused_let_me_check(self):
        assert classify_tone("Let me check the logs for clues.").name == "focused"

    def test_focused_looking_into(self):
        assert classify_tone("Looking into the memory leak.").name == "focused"

    # --- Edge cases ---
    def test_case_insensitive(self):
        assert classify_tone("ALL TESTS PASSED").name == "excited"

    def test_first_match_wins(self):
        # "failed" is serious but "beautiful" is excited -- first match wins
        result = classify_tone("Beautiful failure recovery system")
        assert result.name == "excited"

    def test_no_false_positive_on_partial(self):
        # "crash" should match but "crashing" embedded in another word shouldn't
        assert classify_tone("The app is crashing.").name == "serious"


class TestToneParams:
    """Test tone parameter presets."""

    def test_all_tones_have_valid_ranges(self):
        for name, tone in TONES.items():
            assert 0.0 <= tone.noise_scale <= 1.0, f"{name} noise_scale out of range"
            assert 0.0 <= tone.noise_w_scale <= 1.0, f"{name} noise_w_scale out of range"
            assert 0.0 <= tone.sentence_silence <= 1.0, f"{name} sentence_silence out of range"
            assert 0.5 <= tone.speed_factor <= 1.5, f"{name} speed_factor out of range"

    def test_excited_is_more_expressive_than_neutral(self):
        excited = TONES["excited"]
        neutral = TONES["neutral"]
        assert excited.noise_scale > neutral.noise_scale
        assert excited.noise_w_scale > neutral.noise_w_scale

    def test_serious_is_less_expressive_than_neutral(self):
        serious = TONES["serious"]
        neutral = TONES["neutral"]
        assert serious.noise_scale < neutral.noise_scale

    def test_default_tone_is_neutral(self):
        assert DEFAULT_TONE.name == "neutral"

    def test_get_tone_known(self):
        assert get_tone("excited").name == "excited"

    def test_get_tone_unknown(self):
        assert get_tone("nonexistent").name == "neutral"

    def test_toneParams_frozen(self):
        tone = TONES["neutral"]
        with pytest.raises(AttributeError):
            tone.noise_scale = 0.5  # type: ignore[misc]
