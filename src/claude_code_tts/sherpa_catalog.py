"""Curated, supply-chain-verified picklist of sherpa-onnx TTS models.

Each entry in CATALOG is a permanent trust statement we carry in the
codebase. Adding an entry requires:
  1. Verifying the LICENSE file in the artifact matches the claimed license
  2. Downloading the artifact and computing its SHA256 locally
  3. Pinning to an immutable URL (GitHub release asset path, or HF commit hash)
  4. Disclosing any caveats — bundled GPL deps, attribution requirements, etc.

We are intentionally conservative: only models with unambiguously
permissive (MIT / Apache 2.0 / BSD / CC0) **weights licenses** are listed.

Note on bundled espeak-ng-data: every sherpa-onnx archive that uses
espeak phonemization (i.e. all VITS / Kokoro entries) bundles
espeak-ng-data, which is GPLv3. We are not redistributing that data;
the user downloads it directly from sherpa-onnx upstream at install
time. Each entry surfaces this fact in `license_notes` so the operator
makes an informed choice.

To add a new entry:
  - Run `curl -sL <url> | shasum -a 256` to verify the SHA256
  - Inspect the archive: `tar -tjf <file> | head` to confirm layout
  - Read every LICENSE / MODEL_CARD / NOTICE file inside the archive
  - Cite the sherpa-onnx docs page that lists the model
"""

from __future__ import annotations

from typing import TypedDict


class CatalogEntry(TypedDict):
    id: str
    url: str
    sha256: str
    compressed_bytes: int
    extracted_mb: int
    layout: str  # "vits" | "kokoro" | "matcha"
    license_weights: str
    license_notes: str
    voices: int
    voices_list: list[str]
    sample_rate: int
    source_page: str
    language: str
    verified_date: str


# Verified 2026-05-05 against k2-fsa/sherpa-onnx GitHub releases.
# Each SHA256 was computed locally by downloading the artifact and running
# `shasum -a 256`. URLs use the GitHub release asset path (immutable as long
# as the release is not retagged or assets re-uploaded).
CATALOG: dict[str, CatalogEntry] = {
    "kokoro-en-v0_19": {
        "id": "kokoro-en-v0_19",
        "url": "https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/kokoro-en-v0_19.tar.bz2",
        "sha256": "912804855a04745fa77a30be545b3f9a5d15c4d66db00b88cbcd4921df605ac7",
        "compressed_bytes": 319625534,
        "extracted_mb": 330,
        "layout": "kokoro",
        "license_weights": "Apache 2.0",
        "license_notes": (
            "Kokoro-82M model weights are Apache 2.0 (hexgrad/Kokoro-82M). "
            "The sherpa-onnx archive bundles espeak-ng-data (GPLv3) for "
            "phonemization. We do not redistribute these files; they are "
            "downloaded directly from the sherpa-onnx upstream release."
        ),
        "voices": 11,
        "voices_list": [
            "af", "af_bella", "af_nicole", "af_sarah", "af_sky",
            "am_adam", "am_michael",
            "bf_emma", "bf_isabella",
            "bm_george", "bm_lewis",
        ],
        "sample_rate": 24000,
        "source_page": "https://k2-fsa.github.io/sherpa/onnx/tts/pretrained_models/kokoro.html",
        "language": "en",
        "verified_date": "2026-05-05",
    },

    "kokoro-int8-multi-lang-v1_1": {
        "id": "kokoro-int8-multi-lang-v1_1",
        "url": "https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/kokoro-int8-multi-lang-v1_1.tar.bz2",
        "sha256": "a1e94694776049035c4f2c6529f003aaece993c76aae9a78995831c3c4dcafc6",
        "compressed_bytes": 147031220,
        "extracted_mb": 155,
        "layout": "kokoro",
        "license_weights": "Apache 2.0",
        "license_notes": (
            "Kokoro-82M-v1.1 model weights are Apache 2.0 (hexgrad/"
            "Kokoro-82M-v1.1-zh). int8-quantized variant (~half size of "
            "fp32 with negligible quality drop). Bundles jieba (MIT) for "
            "Chinese segmentation and espeak-ng-data (GPLv3) for English "
            "phonemization, downloaded directly from sherpa-onnx upstream."
        ),
        "voices": 103,
        "voices_list": [],  # 103 voices, EN+ZH multi-lingual; see source_page for full list
        "sample_rate": 24000,
        "source_page": "https://k2-fsa.github.io/sherpa/onnx/tts/pretrained_models/kokoro.html",
        "language": "en+zh",
        "verified_date": "2026-05-05",
    },
}


def get_entry(model_id: str) -> CatalogEntry | None:
    """Look up a catalog entry by id. Returns None if not found."""
    return CATALOG.get(model_id)


def list_ids() -> list[str]:
    """All catalog ids, in stable order."""
    return list(CATALOG.keys())
