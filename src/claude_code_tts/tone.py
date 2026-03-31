"""Content tone classifier for expressive TTS generation.

Analyzes text content and returns Piper generation parameters that
modulate prosody (noise_scale, noise_w_scale, sentence_silence) to
make speech sound contextually appropriate -- excited for celebrations,
grave for errors, warm for personal moments.

Zero runtime dependencies. Keyword/pattern-based classification.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ToneParams:
    """Piper generation parameters for a given tone."""
    name: str
    noise_scale: float      # Prosody variation (higher = more expressive)
    noise_w_scale: float    # Timing variation (higher = more natural rhythm)
    sentence_silence: float  # Seconds of silence between sentences
    speed_factor: float     # Multiplier on base speed (1.0 = no change)


# --- Tone presets ---
# Each maps to Piper's hidden expressiveness knobs.
# Default Piper values: noise_scale=0.667, noise_w_scale=0.8

TONES: dict[str, ToneParams] = {
    "excited": ToneParams(
        name="excited",
        noise_scale=0.9,
        noise_w_scale=0.9,
        sentence_silence=0.1,
        speed_factor=1.05,
    ),
    "serious": ToneParams(
        name="serious",
        noise_scale=0.4,
        noise_w_scale=0.5,
        sentence_silence=0.4,
        speed_factor=0.95,
    ),
    "warm": ToneParams(
        name="warm",
        noise_scale=0.75,
        noise_w_scale=0.85,
        sentence_silence=0.3,
        speed_factor=0.98,
    ),
    "focused": ToneParams(
        name="focused",
        noise_scale=0.5,
        noise_w_scale=0.6,
        sentence_silence=0.15,
        speed_factor=1.0,
    ),
    "neutral": ToneParams(
        name="neutral",
        noise_scale=0.667,
        noise_w_scale=0.8,
        sentence_silence=0.2,
        speed_factor=1.0,
    ),
}

DEFAULT_TONE = TONES["neutral"]

# --- Classification patterns ---
# Each pattern list maps to a tone. First match wins, so order matters.
# Patterns are checked against the full text (case-insensitive).

_EXCITED_PATTERNS = [
    r"\b(?:all\s+)?\d+\s+(?:tests?\s+)?pass(?:ed|ing)\b",
    r"\ball\s+(?:checks?\s+|tests?\s+)?(?:pass(?:ed|ing)|green)\b",
    r"\bsuccessfully\b",
    r"\bshipped?\b",
    r"\bdeployed?\b.*(?:success|complete|done)",
    r"\bcelebrat",
    r"\bhell\s+yes\b",
    r"\bbeautiful\b",
    r"\bperfect\b",
    r"\bnailed\s+it\b",
    r"\blet'?s\s+go\b",
    r"\bawesome\b",
    r"\bincredible\b",
    r"\bamazing\b",
]

_SERIOUS_PATTERNS = [
    r"\b(?:security\s+)?vulnerabilit",
    r"\bCRITICAL\b",
    r"\bERROR\b",
    r"\bfailed?\b.*(?:build|test|deploy|push|commit)",
    r"\b(?:build|tests?|deploy|push)\s+failed\b",
    r"\bbreaking\s+change\b",
    r"\bdata\s+loss\b",
    r"\bincident\b",
    r"\bcompromise[ds]?\b",
    r"\brollback\b",
    r"\bdowntime\b",
    r"\bpanic\b",
    r"\bcrash(?:ed|ing)?\b",
]

_WARM_PATTERNS = [
    r"\bthank(?:s| you)\b",
    r"\bappreciate\b",
    r"\bfriend(?:s|ship)?\b",
    r"\bbrother\b",
    r"\bgood\s+(?:morning|evening|night)\b",
    r"\btake\s+care\b",
    r"\bproud\b",
    r"\bhappy\s+(?:to|for)\b",
    r"\bwelcome\b",
    r"\bcheers\b",
]

_FOCUSED_PATTERNS = [
    r"\bdebug(?:ging)?\b",
    r"\binvestigat",
    r"\banalyzing\b",
    r"\bstack\s+trace\b",
    r"\btraceback\b",
    r"\broot\s+cause\b",
    r"\bbisect",
    r"\bdiagnos",
    r"\blet\s+me\s+(?:check|look|read|trace|dig)",
    r"\blooking\s+(?:at|into)\b",
]

# Compiled pattern groups for performance
_TONE_RULES: list[tuple[str, list[re.Pattern[str]]]] = [
    ("excited", [re.compile(p, re.IGNORECASE) for p in _EXCITED_PATTERNS]),
    ("serious", [re.compile(p, re.IGNORECASE) for p in _SERIOUS_PATTERNS]),
    ("warm", [re.compile(p, re.IGNORECASE) for p in _WARM_PATTERNS]),
    ("focused", [re.compile(p, re.IGNORECASE) for p in _FOCUSED_PATTERNS]),
]


def classify_tone(text: str) -> ToneParams:
    """Classify the tone of text content for TTS generation.

    Returns the ToneParams for the first matching tone, or neutral
    if no patterns match.
    """
    for tone_name, patterns in _TONE_RULES:
        for pattern in patterns:
            if pattern.search(text):
                return TONES[tone_name]
    return DEFAULT_TONE


def get_tone(name: str) -> ToneParams:
    """Get a tone by name, falling back to neutral."""
    return TONES.get(name, DEFAULT_TONE)
