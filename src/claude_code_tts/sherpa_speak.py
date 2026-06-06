"""Sherpa-onnx TTS helper — invoked as a subprocess by audio.py.

This module is NOT imported by the rest of claude-code-tts. It is executed
by the isolated sherpa venv's Python interpreter (~/.claude-tts/venvs/sherpa/bin/python),
which has sherpa_onnx and its native deps installed. The main package stays
dependency-free.

Usage (from audio.py):
    ~/.claude-tts/venvs/sherpa/bin/python -m claude_code_tts.sherpa_speak \\
        --model-dir ~/.claude-tts/sherpa-models/<model-id> \\
        --output /tmp/out.wav \\
        --speaker 42 \\
        --speed 1.0 \\
        < text-on-stdin

The model-dir is expected to contain the artifacts a sherpa-onnx model
ships with: model.onnx, tokens.txt, and (for VITS) optionally lexicon.txt
or espeak-ng-data/. Layout convention is documented in
docs/sherpa-models.md (forthcoming).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _build_tts(model_dir: Path):
    """Build an OfflineTts instance from a model directory.

    Auto-detects model family by looking at the file layout:
      - VITS:   model.onnx + tokens.txt (+ optional lexicon.txt or espeak-ng-data/)
      - Kokoro: model.onnx + tokens.txt + voices.bin (the multi-voice tensor)
      - Matcha: am.onnx + vocoder.onnx + tokens.txt

    Returns a configured OfflineTts. Raises FileNotFoundError if the layout
    doesn't match any supported family.
    """
    import sherpa_onnx  # type: ignore[import-not-found]

    model = model_dir / "model.onnx"
    tokens = model_dir / "tokens.txt"
    voices_bin = model_dir / "voices.bin"
    am = model_dir / "am.onnx"
    vocoder = model_dir / "vocoder.onnx"
    lexicon = model_dir / "lexicon.txt"
    data_dir = model_dir / "espeak-ng-data"

    if voices_bin.is_file() and model.is_file() and tokens.is_file():
        # Kokoro layout
        cfg = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                kokoro=sherpa_onnx.OfflineTtsKokoroModelConfig(
                    model=str(model),
                    voices=str(voices_bin),
                    tokens=str(tokens),
                    data_dir=str(data_dir) if data_dir.is_dir() else "",
                ),
                num_threads=2,
            ),
        )
    elif am.is_file() and vocoder.is_file() and tokens.is_file():
        # Matcha layout
        cfg = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                matcha=sherpa_onnx.OfflineTtsMatchaModelConfig(
                    acoustic_model=str(am),
                    vocoder=str(vocoder),
                    tokens=str(tokens),
                    lexicon=str(lexicon) if lexicon.is_file() else "",
                    data_dir=str(data_dir) if data_dir.is_dir() else "",
                ),
                num_threads=2,
            ),
        )
    elif model.is_file() and tokens.is_file():
        # VITS layout (Piper-compatible, libritts, vctk, ljspeech, etc.)
        cfg = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                    model=str(model),
                    tokens=str(tokens),
                    lexicon=str(lexicon) if lexicon.is_file() else "",
                    data_dir=str(data_dir) if data_dir.is_dir() else "",
                ),
                num_threads=2,
            ),
        )
    else:
        raise FileNotFoundError(
            f"No recognizable sherpa-onnx model layout in {model_dir}. "
            f"Expected one of: VITS (model.onnx + tokens.txt), "
            f"Kokoro (+ voices.bin), or Matcha (am.onnx + vocoder.onnx)."
        )

    return sherpa_onnx.OfflineTts(cfg)


# Kokoro needs a priming phrase before real content or it skips the first
# word's phonemes. We prepend _WARMUP_TEXT, then trim exactly that many
# samples from the output — measured by synthesising the warmup alone so
# there's no hardcoded duration estimate.
_WARMUP_TEXT = "Hey. "


def _serve_mode(model_dir: Path) -> int:
    """Long-lived server: load model once, process JSON-line requests from stdin.

    Protocol (one JSON object per line):
      Request:  {"text": "...", "output": "/path/to/out.wav", "speaker": N, "speed": 1.0}
      Response: {"ok": true} | {"ok": false, "error": "..."}

    Signals readiness after model load:
      {"ready": true}

    Runs until stdin closes or an unrecoverable error occurs.
    All non-JSON diagnostic output goes to stderr only.
    """
    try:
        tts = _build_tts(model_dir)
    except FileNotFoundError as e:
        print(json.dumps({"ready": False, "error": str(e)}), flush=True)
        return 4

    import sherpa_onnx  # type: ignore[import-not-found]

    # One-time warmup: Kokoro's ONNX runtime discards the first ~80-120ms of
    # audio frames during session init. Synthesise a throw-away phrase now so
    # all real requests are clean — no clipping, no audible prefix.
    try:
        tts.generate(_WARMUP_TEXT, sid=0, speed=1.0)
    except Exception:
        pass  # warmup failure is non-fatal; first real request may still clip

    print(json.dumps({"ready": True}), flush=True)

    # Cache: (speaker_id, speed) -> warmup sample count.
    # Populated on first synthesis for each combo; ONNX is deterministic so
    # the same inputs always produce the same number of samples.
    _warmup_trim_cache: dict[tuple[int, float], int] = {}

    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            print(json.dumps({"ok": False, "error": f"bad json: {e}"}), flush=True)
            continue

        text: str = req.get("text", "")
        output: str = req.get("output", "")
        speaker: int = req.get("speaker", 0)
        speed: float = req.get("speed", 1.0)

        if not text.strip():
            print(json.dumps({"ok": False, "error": "empty text"}), flush=True)
            continue
        if not output:
            print(json.dumps({"ok": False, "error": "no output path"}), flush=True)
            continue

        try:
            sid = speaker if speaker >= 0 else 0
            cache_key = (sid, speed)

            # Measure warmup length on first call for this speaker+speed pair.
            if cache_key not in _warmup_trim_cache:
                try:
                    w = tts.generate(_WARMUP_TEXT, sid=sid, speed=speed)
                    _warmup_trim_cache[cache_key] = len(w.samples)
                except Exception:
                    _warmup_trim_cache[cache_key] = 0

            # Synthesise warmup + content, then slice off exactly the warmup
            # samples. This primes Kokoro's phoneme context without the user
            # hearing the preamble word.
            audio = tts.generate(_WARMUP_TEXT + text, sid=sid, speed=speed)
            if not audio.samples:
                print(json.dumps({"ok": False, "error": "no samples generated"}), flush=True)
                continue

            trim_n = _warmup_trim_cache[cache_key]
            clean = audio.samples[trim_n:] if trim_n < len(audio.samples) else audio.samples

            out_path = Path(output)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            sherpa_onnx.write_wave(str(out_path), clean, audio.sample_rate)
            print(json.dumps({"ok": True}), flush=True)
        except Exception as e:
            print(json.dumps({"ok": False, "error": str(e)}), flush=True)

    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Generate speech via sherpa-onnx.")
    p.add_argument("--model-dir", required=True, type=Path,
                   help="Directory containing the sherpa-onnx model artifacts.")
    p.add_argument("--serve", action="store_true",
                   help="Run as a persistent worker: load model once, accept JSON requests "
                        "on stdin, write JSON responses to stdout.")
    p.add_argument("--output", type=Path, default=None,
                   help="Output WAV path (single-shot mode only).")
    p.add_argument("--speaker", type=int, default=-1,
                   help="Speaker ID for multi-speaker models (libritts, vctk, kokoro). "
                        "-1 = model default.")
    p.add_argument("--speed", type=float, default=1.0,
                   help="Speed multiplier passed to OfflineTts.generate.")
    p.add_argument("--text", default=None,
                   help="Text to speak (single-shot mode). If omitted, read from stdin.")
    args = p.parse_args(argv)

    model_dir: Path = args.model_dir.expanduser()
    if not model_dir.is_dir():
        print(f"sherpa_speak: model dir not found: {model_dir}", file=sys.stderr)
        return 2

    if args.serve:
        return _serve_mode(model_dir)

    # Single-shot mode (original behaviour)
    if args.output is None:
        print("sherpa_speak: --output is required in single-shot mode", file=sys.stderr)
        return 1

    text = args.text if args.text is not None else sys.stdin.read()
    if not text.strip():
        print("sherpa_speak: empty text", file=sys.stderr)
        return 3

    try:
        tts = _build_tts(model_dir)
    except FileNotFoundError as e:
        print(f"sherpa_speak: {e}", file=sys.stderr)
        return 4

    sid = args.speaker if args.speaker >= 0 else 0
    audio = tts.generate(text, sid=sid, speed=args.speed)
    if not audio.samples:
        print("sherpa_speak: generation produced no samples", file=sys.stderr)
        return 5

    import sherpa_onnx  # type: ignore[import-not-found]

    args.output.parent.mkdir(parents=True, exist_ok=True)
    sherpa_onnx.write_wave(str(args.output), audio.samples, audio.sample_rate)
    return 0


if __name__ == "__main__":
    sys.exit(main())
