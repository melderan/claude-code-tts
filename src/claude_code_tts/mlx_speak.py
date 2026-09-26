"""mlx-audio TTS helper, run as a subprocess by audio.py.

This module is NOT imported by the rest of claude-code-tts. The isolated mlx
venv's Python (~/.claude-tts/venvs/mlx/bin/python) runs it, because that is
where mlx-audio and MLX live; the main package stays dependency-free and
this file only imports them inside functions, so its pure parts are
testable anywhere.

Usage (from audio.py):
    ~/.claude-tts/venvs/mlx/bin/python -m claude_code_tts.mlx_speak \\
        --model mlx-community/Kokoro-82M-bf16 --serve

Serve protocol, one JSON object per line:
    ready:    {"ready": true, "model": "...", "sample_rate": 24000}
              {"ready": false, "error": "..."}
    request:  {"text": "...", "output": "/path/out.wav", "voice": "af_heart",
               "speed": 1.0, "lang_code": "a"}
    response: {"ok": true, "seconds": 1.9} | {"ok": false, "error": "..."}

Models differ in what their generate() accepts (Kokoro takes voice, speed and
lang_code; others take a subset, or reference audio), so the request's
fields are matched against the model's signature and only the ones it
accepts are passed. Everything not JSON goes to stderr.
"""

from __future__ import annotations

import argparse
import inspect
import json
import struct
import sys
import time
import wave
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any


def generate_kwargs(generate: Callable[..., Any], *, voice: str, speed: float, lang_code: str) -> dict[str, Any]:
    """Return the keyword arguments `generate` accepts out of voice, speed and lang_code.

    Empty voice and lang_code are never passed, so the model's own default
    stands. A generate() with **kwargs takes everything non-empty.
    """
    params: Mapping[str, inspect.Parameter]
    try:
        params = inspect.signature(generate).parameters
    except (TypeError, ValueError):
        params = {}
    accepts_any = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())

    def accepts(name: str) -> bool:
        return accepts_any or name in params

    kwargs: dict[str, Any] = {}
    if voice and accepts("voice"):
        kwargs["voice"] = voice
    if accepts("speed"):
        kwargs["speed"] = speed
    if lang_code and accepts("lang_code"):
        kwargs["lang_code"] = lang_code
    return kwargs


def _to_floats(audio: Any) -> list[float]:
    """Flatten one generated audio chunk (an mx.array, numpy array or list) to floats."""
    try:
        import numpy as np  # ty: ignore[unresolved-import]  (present in the mlx venv)

        return [float(x) for x in np.asarray(audio, dtype="float32").reshape(-1)]
    except ImportError:
        pass
    if hasattr(audio, "tolist"):
        audio = audio.tolist()
    flat: list[float] = []
    stack = [audio]
    while stack:
        item = stack.pop()
        if isinstance(item, (list, tuple)):
            stack.extend(reversed(item))
        else:
            flat.append(float(item))
    return flat


def write_wav(path: Path, samples: Iterable[float], sample_rate: int) -> int:
    """Write mono 16-bit PCM; returns the number of frames written.

    Samples are floats in [-1, 1] and are clipped, so a hot model cannot
    wrap around into clicks.
    """
    frames = bytearray()
    count = 0
    for s in samples:
        v = max(-1.0, min(1.0, s))
        frames += struct.pack("<h", int(v * 32767))
        count += 1
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(bytes(frames))
    return count


def synthesize(model: Any, text: str, *, voice: str, speed: float, lang_code: str, output: Path) -> float:
    """Run the model over `text` and write the WAV; returns the audio length in seconds.

    `model.generate` yields chunks with `.audio` and usually `.sample_rate`;
    chunks are concatenated in order. Raises RuntimeError when nothing came out.
    """
    kwargs = generate_kwargs(model.generate, voice=voice, speed=speed, lang_code=lang_code)
    samples: list[float] = []
    sample_rate = int(getattr(model, "sample_rate", 0) or 0)
    for chunk in model.generate(text, **kwargs):
        audio = getattr(chunk, "audio", chunk)
        rate = getattr(chunk, "sample_rate", None)
        if rate:
            sample_rate = int(rate)
        samples.extend(_to_floats(audio))
    if not samples:
        raise RuntimeError("generation produced no samples")
    if not sample_rate:
        raise RuntimeError("model reported no sample rate")
    write_wav(output, samples, sample_rate)
    return len(samples) / sample_rate


def _load(model_id: str) -> Any:
    from mlx_audio.tts.utils import load_model  # ty: ignore[unresolved-import]  (mlx venv only)

    return load_model(model_id)


def _serve_mode(model_id: str) -> int:
    """Load the model once, then answer JSON-line requests until stdin closes."""
    try:
        started = time.time()
        model = _load(model_id)
        print(f"mlx_speak: loaded {model_id} in {time.time() - started:.1f}s", file=sys.stderr, flush=True)
    except Exception as e:  # any failure to load is one message to the parent
        print(json.dumps({"ready": False, "error": f"{type(e).__name__}: {e}"}), flush=True)
        return 4

    print(json.dumps({"ready": True, "model": model_id, "sample_rate": int(getattr(model, "sample_rate", 0) or 0)}), flush=True)

    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            print(json.dumps({"ok": False, "error": f"bad json: {e}"}), flush=True)
            continue
        text = str(req.get("text", ""))
        output = str(req.get("output", ""))
        if not text.strip():
            print(json.dumps({"ok": False, "error": "empty text"}), flush=True)
            continue
        if not output:
            print(json.dumps({"ok": False, "error": "no output path"}), flush=True)
            continue
        try:
            seconds = synthesize(
                model, text,
                voice=str(req.get("voice", "")),
                speed=float(req.get("speed", 1.0)),
                lang_code=str(req.get("lang_code", "")),
                output=Path(output),
            )
            print(json.dumps({"ok": True, "seconds": round(seconds, 2)}), flush=True)
        except Exception as e:
            print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}), flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Generate speech via mlx-audio.")
    p.add_argument("--model", required=True, help="Hugging Face model id, e.g. mlx-community/Kokoro-82M-bf16")
    p.add_argument("--serve", action="store_true", help="Load once and answer JSON-line requests on stdin")
    p.add_argument("--output", type=Path, default=None, help="Output WAV path (single-shot mode)")
    p.add_argument("--voice", default="", help="Voice preset, e.g. af_heart")
    p.add_argument("--speed", type=float, default=1.0, help="Speed multiplier, when the model takes one")
    p.add_argument("--lang-code", default="", help="Language code, when the model takes one (Kokoro: a, b, j, z, e)")
    p.add_argument("--text", default=None, help="Text to speak (single-shot); stdin when omitted")
    args = p.parse_args(argv)

    if args.serve:
        return _serve_mode(args.model)

    if args.output is None:
        print("mlx_speak: --output is required in single-shot mode", file=sys.stderr)
        return 1
    text = args.text if args.text is not None else sys.stdin.read()
    if not text.strip():
        print("mlx_speak: empty text", file=sys.stderr)
        return 3
    try:
        model = _load(args.model)
    except Exception as e:
        print(f"mlx_speak: could not load {args.model}: {type(e).__name__}: {e}", file=sys.stderr)
        return 4
    try:
        seconds = synthesize(model, text, voice=args.voice, speed=args.speed, lang_code=args.lang_code, output=args.output)
    except Exception as e:
        print(f"mlx_speak: {type(e).__name__}: {e}", file=sys.stderr)
        return 5
    print(f"mlx_speak: wrote {seconds:.1f}s to {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
