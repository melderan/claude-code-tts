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
    voices: list[str]
    voices_hint: str
    default_lang: str
    languages: str
    notes: str


# Kokoro's 54 voice presets, read from the voices/ tree of mlx-community/Kokoro-82M-bf16
# on 2026-09-26. The first letter is the language (misaki code), the second the sex.
KOKORO_VOICES: list[str] = [
    "af_alloy", "af_aoede", "af_bella", "af_heart", "af_jessica", "af_kore", "af_nicole", "af_nova",
    "af_river", "af_sarah", "af_sky",
    "am_adam", "am_echo", "am_eric", "am_fenrir", "am_liam", "am_michael", "am_onyx", "am_puck", "am_santa",
    "bf_alice", "bf_emma", "bf_isabella", "bf_lily",
    "bm_daniel", "bm_fable", "bm_george", "bm_lewis",
    "ef_dora", "em_alex", "em_santa",
    "ff_siwis",
    "hf_alpha", "hf_beta", "hm_omega", "hm_psi",
    "if_sara", "im_nicola",
    "jf_alpha", "jf_gongitsune", "jf_nezumi", "jf_tebukuro", "jm_kumo",
    "pf_dora", "pm_alex", "pm_santa",
    "zf_xiaobei", "zf_xiaoni", "zf_xiaoxiao", "zf_xiaoyi",
    "zm_yunjian", "zm_yunxi", "zm_yunxia", "zm_yunyang",
]

# Kokoro language codes by voice prefix (misaki): the same letter the voice name starts with.
KOKORO_LANGS: dict[str, str] = {
    "a": "American English", "b": "British English", "e": "Spanish", "f": "French", "h": "Hindi",
    "i": "Italian", "j": "Japanese", "p": "Brazilian Portuguese", "z": "Mandarin Chinese",
}

# KittenTTS 0.8 voice aliases, from config.json `voice_aliases` of mlx-community/kitten-tts-nano-0.8.
KITTEN_VOICES: list[str] = ["Bella", "Jasper", "Luna", "Bruno", "Rosie", "Hugo", "Kiki", "Leo"]

# Qwen3-TTS CustomVoice speakers, from the Qwen model card's "Supported Speakers" table.
QWEN3_SPEAKERS: list[str] = ["Vivian", "Serena", "Uncle_Fu", "Dylan", "Eric", "Ryan", "Aiden", "Ono_Anna", "Sohee"]


def kokoro_lang_for(voice: str) -> str:
    """The Kokoro language code implied by a voice name: bm_george speaks 'b', jf_alpha 'j'; unknown names get ''."""
    if len(voice) >= 3 and voice[0] in KOKORO_LANGS and voice[1] in "fm" and voice[2] == "_":
        return voice[0]
    return ""


def _kokoro(id_: str, repo: str, size_mb: int, notes: str) -> MlxCatalogEntry:
    return {
        "id": id_, "hf_repo": repo, "engine": "Kokoro", "license_weights": "apache-2.0",
        "size_mb": size_mb, "checked": "2026-09-26", "default_voice": "af_heart",
        "voices": KOKORO_VOICES,
        "voices_hint": "54 presets; af_/am_ American, bf_/bm_ British, then e f h i j p z by language",
        "default_lang": "a",
        "languages": "en (a American, b British), es (e), fr (f), hi (h), it (i), ja (j), pt (p), zh (z)",
        "notes": notes,
    }


def _kitten(id_: str, repo: str, size_mb: int, notes: str) -> MlxCatalogEntry:
    return {
        "id": id_, "hf_repo": repo, "engine": "KittenTTS", "license_weights": "apache-2.0",
        "size_mb": size_mb, "checked": "2026-09-26", "default_voice": "Bella",
        "voices": KITTEN_VOICES, "voices_hint": " ".join(KITTEN_VOICES),
        "default_lang": "", "languages": "en", "notes": notes,
    }


def _qwen(id_: str, repo: str, size_mb: int, notes: str, voices: list[str] | None = None) -> MlxCatalogEntry:
    return {
        "id": id_, "hf_repo": repo, "engine": "Qwen3-TTS", "license_weights": "apache-2.0",
        "size_mb": size_mb, "checked": "2026-09-26",
        "default_voice": "Ryan" if voices else "",
        "voices": voices or [],
        "voices_hint": " ".join(voices) + " (Ryan and Aiden are the English natives)" if voices else "voice design from a text description; no presets",
        "default_lang": "", "languages": "zh, en, ja, ko and more", "notes": notes,
    }


CATALOG: dict[str, MlxCatalogEntry] = {
    "kokoro": _kokoro("kokoro", "mlx-community/Kokoro-82M-bf16", 389,
                      "82M parameters, fast, 54 voice presets; the natural first pick. Speed is honoured."),
    "kokoro-8bit": _kokoro("kokoro-8bit", "mlx-community/Kokoro-82M-8bit", 678,
                           "Quantised Kokoro; lower memory, same voices."),
    "kokoro-4bit": _kokoro("kokoro-4bit", "mlx-community/Kokoro-82M-4bit", 672,
                           "Most quantised Kokoro; lowest memory, judge the quality by ear."),
    "kitten-nano": _kitten("kitten-nano", "mlx-community/kitten-tts-nano-0.8", 64,
                           "Tiny edge model; the smallest download here."),
    "kitten-micro": _kitten("kitten-micro", "mlx-community/kitten-tts-micro-0.8", 269,
                            "Mid-size KittenTTS."),
    "kitten-mini": _kitten("kitten-mini", "mlx-community/kitten-tts-mini-0.8", 581,
                           "Largest KittenTTS."),
    "qwen3-tts-0.6b": _qwen("qwen3-tts-0.6b", "mlx-community/Qwen3-TTS-12Hz-0.6B-CustomVoice-8bit", 2931,
                            "Nine named speakers, takes an instruct string for style; slower than Kokoro.", QWEN3_SPEAKERS),
    "qwen3-tts-1.7b": _qwen("qwen3-tts-1.7b", "mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit", 5112,
                            "The larger CustomVoice model, same nine speakers.", QWEN3_SPEAKERS),
    "qwen3-tts-design": _qwen("qwen3-tts-design", "mlx-community/Qwen3-TTS-12Hz-1.7B-VoiceDesign-bf16", 4515,
                              "Designs a voice from a text description instead of a preset."),
    "outetts-0.6b": {
        "id": "outetts-0.6b",
        "hf_repo": "mlx-community/OuteTTS-1.0-0.6B-fp16",
        "engine": "OuteTTS",
        "license_weights": "apache-2.0",
        "size_mb": 1216,
        "checked": "2026-09-26",
        "default_voice": "",
        "voices": [],
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
        "voices": [],
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
        "voices": [],
        "voices_hint": "conversational; clones a voice from reference audio",
        "default_lang": "",
        "languages": "en",
        "notes": "Sesame-style conversational speech; the largest download here.",
    },
    "chatterbox-v2": {
        "id": "chatterbox-v2",
        "hf_repo": "mlx-community/chatterbox-fp16",
        "engine": "Chatterbox",
        "license_weights": "apache-2.0",
        "size_mb": 5271,
        "checked": "2026-09-26",
        "default_voice": "",
        "voices": [],
        "voices_hint": "clones a voice from reference audio",
        "default_lang": "",
        "languages": "en",
        "notes": "The English-only Chatterbox; v3 below is the multilingual one.",
    },
    "chatterbox-v3": {
        "id": "chatterbox-v3",
        "hf_repo": "mlx-community/chatterbox-multilingual-v3",
        "engine": "Chatterbox",
        "license_weights": "mit",
        "size_mb": 5422,
        "checked": "2026-09-26",
        "default_voice": "",
        "voices": [],
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


def voices_for(name: str) -> list[str]:
    """The named voice presets of a catalog id or repository, [] when the model has none we know."""
    entry = CATALOG.get(name) or entry_for_repo(name)
    return list(entry["voices"]) if entry else []


def default_lang_for(name: str, voice: str = "") -> str:
    """The language code to pass for a model and voice: Kokoro's from the voice prefix, else the catalog default."""
    entry = CATALOG.get(name) or entry_for_repo(name)
    if entry and entry["engine"] == "Kokoro":
        return kokoro_lang_for(voice) or entry["default_lang"]
    return entry["default_lang"] if entry else ""


def entry_for_repo(repo: str) -> MlxCatalogEntry | None:
    """The catalog entry whose repository is `repo`, if any."""
    for entry in CATALOG.values():
        if entry["hf_repo"] == repo or entry["id"] == repo:
            return entry
    return None
