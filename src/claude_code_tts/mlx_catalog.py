"""Curated picklist of mlx-audio TTS models, with their licenses checked.

mlx-audio (MIT, Prince Canuma) runs text-to-speech models on Apple silicon
through MLX. It loads models from Hugging Face by repository id, so unlike
the sherpa catalog there is no archive to pin and hash: the trust statement
here is the license and size of each repository as its model card reported
them on the day noted, read through the Hugging Face API
(`https://huggingface.co/api/models/<id>`, field `cardData.license`).

Only models whose weights license is permissive are listed. Left out on
purpose, same check, same day: Spark-TTS (cc-by-nc-sa-4.0), Voxtral TTS
(cc-by-nc-4.0) and Soprano (no license stated).

Kokoro's English text processing comes from misaki[en], which depends on
espeak-ng (GPLv3) through espeakng-loader. That is the same fact the
sherpa catalog notes for espeak-ng-data: the operator installs it from PyPI
on their own machine; this project does not redistribute it.

To add an entry: check the license and size with the API call above, write
the date into `checked`, and name the voices only when the model card or
the mlx-audio README names them.
"""

from __future__ import annotations

from typing import TypedDict


class MlxCatalogEntry(TypedDict):
    id: str
    hf_repo: str
    engine: str
    license_weights: str
    size_mb: int
    checked: str
    default_voice: str
    voices_hint: str
    default_lang: str
    languages: str
    notes: str


CATALOG: dict[str, MlxCatalogEntry] = {
    "kokoro": {
        "id": "kokoro",
        "hf_repo": "mlx-community/Kokoro-82M-bf16",
        "engine": "Kokoro",
        "license_weights": "apache-2.0",
        "size_mb": 389,
        "checked": "2026-09-26",
        "default_voice": "af_heart",
        "voices_hint": "af_heart af_bella af_nova af_sky am_adam am_echo (American); "
                       "bf_alice bf_emma bm_daniel bm_george (British); jf_alpha jm_kumo; zf_xiaobei zm_yunxi",
        "default_lang": "a",
        "languages": "en (a American, b British), ja (j), zh (z), es (e), fr, hi, it, pt",
        "notes": "82M parameters, fast, 54 voice presets; the natural first pick. Speed is honoured.",
    },
    "kitten-nano": {
        "id": "kitten-nano",
        "hf_repo": "mlx-community/kitten-tts-nano-0.8",
        "engine": "KittenTTS",
        "license_weights": "apache-2.0",
        "size_mb": 64,
        "checked": "2026-09-26",
        "default_voice": "",
        "voices_hint": "see the model card",
        "default_lang": "",
        "languages": "en",
        "notes": "Tiny edge model; the smallest download here.",
    },
    "qwen3-tts-0.6b": {
        "id": "qwen3-tts-0.6b",
        "hf_repo": "mlx-community/Qwen3-TTS-12Hz-0.6B-CustomVoice-8bit",
        "engine": "Qwen3-TTS",
        "license_weights": "apache-2.0",
        "size_mb": 2931,
        "checked": "2026-09-26",
        "default_voice": "Vivian",
        "voices_hint": "Vivian Ryan (from the mlx-audio README); more on the model card",
        "default_lang": "",
        "languages": "zh, en, ja, ko and more",
        "notes": "Larger and slower than Kokoro; expressive, takes an instruct string for style.",
    },
    "outetts-0.6b": {
        "id": "outetts-0.6b",
        "hf_repo": "mlx-community/OuteTTS-1.0-0.6B-fp16",
        "engine": "OuteTTS",
        "license_weights": "apache-2.0",
        "size_mb": 1216,
        "checked": "2026-09-26",
        "default_voice": "",
        "voices_hint": "see the model card",
        "default_lang": "",
        "languages": "en",
        "notes": "Language-model based; slower than Kokoro.",
    },
    "dia-1.6b": {
        "id": "dia-1.6b",
        "hf_repo": "mlx-community/Dia-1.6B-fp16",
        "engine": "Dia",
        "license_weights": "apache-2.0",
        "size_mb": 3222,
        "checked": "2026-09-26",
        "default_voice": "",
        "voices_hint": "dialogue model: text uses [S1] and [S2] speaker tags",
        "default_lang": "",
        "languages": "en",
        "notes": "Made for two-speaker dialogue; heavy for a narrator.",
    },
    "csm-1b": {
        "id": "csm-1b",
        "hf_repo": "mlx-community/csm-1b",
        "engine": "CSM",
        "license_weights": "apache-2.0",
        "size_mb": 6218,
        "checked": "2026-09-26",
        "default_voice": "",
        "voices_hint": "conversational; clones a voice from reference audio",
        "default_lang": "",
        "languages": "en",
        "notes": "Sesame-style conversational speech; the largest download here.",
    },
    "chatterbox-v3": {
        "id": "chatterbox-v3",
        "hf_repo": "mlx-community/chatterbox-multilingual-v3",
        "engine": "Chatterbox",
        "license_weights": "mit",
        "size_mb": 5422,
        "checked": "2026-09-26",
        "default_voice": "",
        "voices_hint": "clones a voice from reference audio",
        "default_lang": "",
        "languages": "23 languages",
        "notes": "Expressive multilingual model with voice cloning.",
    },
}


def list_ids() -> list[str]:
    """Catalog ids in listing order."""
    return list(CATALOG)


def resolve_model(name: str) -> str:
    """Return the Hugging Face repository for a catalog id, or `name` itself when it already is one."""
    entry = CATALOG.get(name)
    if entry:
        return entry["hf_repo"]
    return name


def entry_for_repo(repo: str) -> MlxCatalogEntry | None:
    """The catalog entry whose repository is `repo`, if any."""
    for entry in CATALOG.values():
        if entry["hf_repo"] == repo or entry["id"] == repo:
            return entry
    return None
