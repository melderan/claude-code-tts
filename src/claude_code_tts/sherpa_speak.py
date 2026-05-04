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


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Generate speech via sherpa-onnx.")
    p.add_argument("--model-dir", required=True, type=Path,
                   help="Directory containing the sherpa-onnx model artifacts.")
    p.add_argument("--output", required=True, type=Path,
                   help="Output WAV path.")
    p.add_argument("--speaker", type=int, default=-1,
                   help="Speaker ID for multi-speaker models (libritts, vctk, kokoro). "
                        "-1 = model default.")
    p.add_argument("--speed", type=float, default=1.0,
                   help="Speed multiplier passed to OfflineTts.generate.")
    p.add_argument("--text", default=None,
                   help="Text to speak. If omitted, read from stdin.")
    args = p.parse_args(argv)

    model_dir: Path = args.model_dir.expanduser()
    if not model_dir.is_dir():
        print(f"sherpa_speak: model dir not found: {model_dir}", file=sys.stderr)
        return 2

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
