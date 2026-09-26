# mlx-audio backend

A fourth speech engine beside Piper, swift-kokoro and sherpa-onnx: [mlx-audio](https://github.com/Blaizzy/mlx-audio)
(MIT) runs text-to-speech models on Apple silicon through MLX. Kokoro is the first pick; the
same backend serves KittenTTS, Qwen3-TTS, OuteTTS, Dia, CSM and Chatterbox from one venv.

It is additive and opt-in, exactly like sherpa: existing personas do not change, and a persona
speaks through mlx only when it carries `voice_mlx`.

## Enable, fetch, try, adopt

```bash
claude-tts-install --enable-mlx          # venv at ~/.claude-tts/venvs/mlx with mlx-audio[tts] and misaki[en]
claude-tts mlx list-available            # curated models, license and size as checked on Hugging Face
claude-tts mlx pull kokoro               # 389 MB into the Hugging Face cache, before the daemon needs it
claude-tts speak --voice-mlx kokoro --speaker-mlx af_heart "hello there"     # one-shot ear test
claude-tts persona add lyra --mlx kokoro --mlx-voice bf_emma --mlx-lang b --project
claude-tts daemon restart                # the daemon warms one worker per mlx model at start
claude-tts mlx status                    # platform, venv, which models are cached, who uses mlx
```

`--enable-mlx` refuses anywhere but macOS on Apple silicon (exit 7) and points at `--enable-sherpa`,
which runs everywhere. It downloads no models; `mlx pull` does, and accepts a catalog id or any
Hugging Face repository (a repository outside the catalog prints a reminder to check its license).

## How it runs

The daemon keeps one `mlx_speak.py --serve` process per model, in the venv, with the model in
memory; requests and answers are JSON lines (the contract is in the module docstring). A model
not yet in the cache is downloaded on that first start, which is why `mlx pull` comes first: the
worker waits up to ten minutes for a ready line, and the play loop would wait with it. At daemon
start, personas with `voice_mlx` are warmed in a background thread so playback never blocks on a
load; `daemon.log` says `mlx worker(s) ready: ...` when they are up, and `debug.log` carries the
`mlx worker:` lines when they are not.

Persona keys:

| key | meaning | Kokoro example |
|-----|---------|----------------|
| `voice_mlx` | Hugging Face repository | `mlx-community/Kokoro-82M-bf16` |
| `speaker_mlx` | the model's voice preset | `af_heart`, `bm_george` |
| `lang_mlx` | language code, when the model takes one | `a` American, `b` British, `j`, `z`, `e` |

Every model's `generate()` accepts a different set of arguments, so the worker matches the
request against the model's signature and passes only what it takes; an empty voice or language
leaves the model's own default in place.

**Speed** follows the Piper rule. With `speed_method: "length_scale"` the speed is synthesised
into the audio (Kokoro honours it); with `"playback"` the model runs at 1.0 and the player speeds
it up, because not every model honours a speed argument.

## Licenses

mlx-audio is MIT. The catalog in `mlx_catalog.py` lists only models whose weights license was
read as permissive from the model card through the Hugging Face API on the date each entry
records; Spark-TTS (cc-by-nc-sa-4.0), Voxtral TTS (cc-by-nc-4.0) and Soprano (no license
stated) are left out on purpose. Kokoro's English text processing, misaki[en], depends on
espeak-ng (GPLv3) through espeakng-loader; the operator installs it from PyPI on their own
machine, and this project redistributes none of it. That is the same fact the sherpa catalog
records for espeak-ng-data.

## Where it cannot be tested

MLX has no Linux or Intel build, so a Linux checkout (a sandbox, CI) proves the parent side only:
routing, the worker protocol against a stand-in child, the installer's prompt discipline, and
the pure parts of `mlx_speak.py` (argument matching, WAV writing). The first real synthesis is
the `speak --voice-mlx` line above, on a Mac, by ear.
