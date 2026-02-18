# Design Document: kokoro-tts Swift CLI

Swift CLI binary for local, offline text-to-speech using Kokoro-82M via CoreML on macOS.

Status: Pre-implementation research. This document captures everything needed to build the binary.

---

## 1. What We Are Building

A standalone macOS CLI binary named `kokoro-tts` that:

- Reads text from stdin or `--text "..."` flag
- Synthesizes speech using the Kokoro-82M CoreML model (Apple Neural Engine)
- Writes a WAV file to stdout or `--output path.wav`
- Supports `--voice <name>` (e.g. `af_heart`, `am_michael`) from a 48-voice preset library
- Supports `--speed <float>` (0.5 to 2.0, default 1.0)
- Loads all model files from `~/.ai-tts/models/kokoro/`
- Targets macOS 15.0+, arm64 (Apple Silicon only - hard requirement from dependencies)
- Keeps peak RAM under 1.5 GB

This is intended to become the TTS backend for `claude-code-tts`, replacing Piper.

---

## 2. Source of Truth: FluidInference/kokoro-82m-coreml

The HuggingFace repo `FluidInference/kokoro-82m-coreml` contains everything needed.
Total size: 3.12 GB. Files:

### Model Files (choose one variant pair)

| File | Purpose |
|------|---------|
| `kokoro_21_5s.mlmodelc` | Duration + decoder, short utterances (5s audio cap) |
| `kokoro_21_10s.mlmodelc` | Duration + decoder, medium utterances (10s cap) |
| `kokoro_21_15s.mlmodelc` | Duration + decoder, long utterances (15s cap) |
| `kokoro_24_10s.mlmodelc` | Newer variant, medium (10s cap) |
| `kokoro_24_15s.mlmodelc` | Newer variant, long (15s cap) |

The `21` vs `24` prefix likely refers to internal architecture version (the `mattmireles/kokoro-coreml` conversion pipeline uses a different bucketing scheme with 3s/10s/45s). FluidInference uses a combined single-model approach per duration bucket rather than the two-stage split. Start with `kokoro_24_10s.mlmodelc` as the default - it covers most Claude responses and uses the newer architecture.

### Supporting Data Files

| File | Size | Purpose |
|------|------|---------|
| `vocab_index.json` | 2 KB | Maps IPA phoneme characters to integer token IDs |
| `us_gold.json` | 3 MB | High-confidence US English word->phoneme dictionary |
| `us_silver.json` | 3.1 MB | Lower-confidence fallback US dictionary |
| `us_lexicon_cache.json` | 30.7 MB | Pre-cached US pronunciations (speeds up cold start) |
| `gb_gold.json` | 2.84 MB | UK English gold dictionary |
| `gb_silver.json` | 3.66 MB | UK English silver dictionary |
| `config.json` | tiny | Model config (may contain vocab fallback) |
| `voices/<name>.json` | ~2.7 MB each | Voice style embeddings (48 voices, ~144 MB total) |

### Voice Files

48 voice presets, stored as JSON arrays of float32 values. The embedding shape is `[1, 256]` (256-dimensional style vector). Naming convention:
- `af_*` = American female
- `am_*` = American male
- `bf_*` = British female
- `bm_*` = British male

Recommended defaults to expose: `af_heart`, `af_bella`, `af_nova`, `am_michael`, `am_eric`, `bf_emma`, `bm_george`.

---

## 3. Inference Pipeline

This is the most critical section. The pipeline has three stages.

### Stage 1: G2P (Text to Phoneme Token IDs)

**What it does:** Converts English text to a sequence of integer IDs representing IPA phonemes.

**The problem:** There is no pure Swift, lightweight G2P engine that matches Kokoro's vocabulary. The options are:

#### Option A: MisakiSwift (Recommended but heavy)

`MisakiSwift` is a Swift port of hexgrad's Misaki G2P engine. It uses:
- Gold/silver dictionary lookup (same files as the HuggingFace repo)
- Apple's NaturalLanguage framework for POS tagging (replaces SpaCy)
- A BART-based neural network (via MLX) as OOV fallback

API:
```swift
import MisakiSwift
let g2p = EnglishG2P(british: false)  // false = American English
let (phonemes, tokens) = g2p.phonemize(text: "Hello world!")
// phonemes: "həlˈO wˈɜɹld!"  (IPA string)
// tokens: [Token] with per-character detail
```

**Problem:** MisakiSwift depends on MLX (Apple's ML framework). This adds ~200-400 MB of framework overhead and a nontrivial BART model for the fallback. This may push peak RAM over budget.

**Mitigation:** The BART fallback only fires for truly unknown words. For typical Claude output (English prose, code terms, common nouns), dictionary lookup covers ~95%+ of words. The MLX framework is shared with MisakiSwift itself, so if we are already pulling it in, the marginal cost of the BART fallback is low.

**Platform requirement:** macOS 15.0+, arm64 only. This matches our target.

#### Option B: Dictionary-Only Tokenizer (Fallback, lighter)

Implement a Swift tokenizer that:
1. Loads `us_gold.json` and `us_silver.json` from the model directory
2. Looks up each word in the dictionary, gets back an IPA string
3. Converts IPA characters to token IDs using `vocab_index.json`
4. Falls back to eSpeak-NG via subprocess for OOV words

This avoids the MLX dependency entirely but produces noisier output for OOV words. eSpeak-NG is available via `brew install espeak-ng` or can be bundled as a static library.

**Recommendation for v1:** Use Option B (dictionary-only + eSpeak subprocess). It keeps the binary lean, avoids the MLX dependency conflict, and stays under 1.5 GB RAM. Add MisakiSwift as Option A in v2 once we measure actual memory.

### Stage 2: Duration Model + Alignment (CPU/GPU)

The duration model takes the token sequence and predicts how long each phoneme lasts, producing intermediate feature tensors.

**CoreML inputs to the duration model:**
```
input_ids:       [1, 128]   Int32   - phoneme token IDs, zero-padded to 128
attention_mask:  [1, 128]   Int32   - 1 for real tokens, 0 for padding
ref_s:           [1, 256]   Float32 - voice style embedding
speed:           [1]        Float32 - speech rate multiplier (0.5 to 2.0)
```

**CoreML outputs from the duration model:**
```
pred_dur   - per-phoneme duration predictions
d          - duration features
t_en       - text encoder features [512, token_count]
s          - speaker conditioning
ref_s_out  - passed through to decoder
```

**Client-side alignment construction (Swift code we write):**

The decoder requires a fixed-shape `asr` tensor. We build this by:
1. Rounding `pred_dur` to integer frame counts per phoneme
2. Building `pred_aln_trg [token_count, frame_count]` - a matrix where each phoneme spans its predicted duration
3. Computing `asr = t_en @ pred_aln_trg` - matrix multiply
4. Padding or cropping `asr` to `[1, 512, 72]` for the 10s bucket (or 144 for 15s)
5. Deriving simple `F0_pred` and `N_pred` curves from duration spans

Note: `asr` shape depends on bucket. The 10s model uses 72 frames (40fps * ~1.8s... see below). The 15s uses 144 frames. Bucket selection is based on predicted total duration.

**Compute units for duration model:** CPU and GPU only. LSTM layers in the duration model are not ANE-compatible.

### Stage 3: HAR Decoder (Apple Neural Engine)

The decoder synthesizes the actual audio waveform.

**CoreML inputs to the decoder:**
```
asr:    [1, 512, 72]   Float32  - aligned speech representation (72 = 10s bucket)
F0_pred: [1, 144]      Float32  - pitch contour (2x ASR time frames)
N_pred:  [1, 144]      Float32  - loudness contour
ref_s:   [1, 256]      Float32  - voice style embedding (decoder uses first 128 dims internally)
```

**CoreML output:**
```
waveform: [1, N]   Float32  - raw PCM audio at 24,000 Hz mono
```

For a 10s model: `N = 240,000` samples (10s * 24kHz). Not all samples are speech; the model pads output to the fixed bucket size.

**Compute units for decoder:** All (CPU + GPU + ANE). The HAR (Harmonic-Aided Reconstruction) vocoder is designed for ANE acceleration.

### Long Text Chunking

The duration model accepts a maximum of **128 tokens**. Typical English prose runs about 3-5 phoneme tokens per word, so the hard cap is roughly 25-40 words per chunk.

Chunking strategy:
1. Split input text at sentence boundaries (`.`, `!`, `?`, `;`) first
2. If a sentence still exceeds ~100 tokens, split at clause boundaries (`,`, ` - `, conjunctions)
3. Process each chunk independently, collect waveform arrays
4. Concatenate all waveforms end-to-end before writing WAV

Small crossfade (5-10ms, ~120-240 samples) at chunk boundaries prevents clicks.

---

## 4. File Layout on Disk

Model path: `~/.ai-tts/models/kokoro/` (default, overridable with `KOKORO_MODEL_DIR` env var).

```
~/.ai-tts/models/kokoro/
  kokoro_24_10s.mlmodelc/      <- primary model (auto-select based on chunk duration)
  kokoro_24_15s.mlmodelc/      <- fallback for longer chunks
  vocab_index.json
  us_gold.json
  us_silver.json
  us_lexicon_cache.json         <- optional, large; skip for initial install
  voices/
    af_heart.json
    af_bella.json
    am_michael.json
    ... (all 48 voices, ~2.7 MB each)
```

A setup script (shell or Python) handles the HuggingFace download:

```bash
# Proposed: kokoro-setup.sh
pip install -q huggingface_hub
python3 -c "
from huggingface_hub import snapshot_download
snapshot_download(
    repo_id='FluidInference/kokoro-82m-coreml',
    local_dir=os.path.expanduser('~/.ai-tts/models/kokoro'),
    ignore_patterns=['*.mlpackage', 'kokoro_21_*', 'gb_*.json']
)
"
```

This downloads ~1.5 GB (skipping .mlpackage source files and GB variants not needed for US English default).

---

## 5. Swift Binary Architecture

### Package.swift

```swift
// swift-tools-version: 5.10
import PackageDescription

let package = Package(
    name: "kokoro-tts",
    platforms: [.macOS(.v15)],
    dependencies: [
        .package(
            url: "https://github.com/apple/swift-argument-parser",
            from: "1.3.0"
        ),
        // MisakiSwift is optional / v2 feature:
        // .package(url: "https://github.com/mlalma/MisakiSwift", from: "1.0.1"),
    ],
    targets: [
        .executableTarget(
            name: "kokoro-tts",
            dependencies: [
                .product(name: "ArgumentParser", package: "swift-argument-parser"),
                // .product(name: "MisakiSwift", package: "MisakiSwift"),
            ],
            path: "Sources/KokoroTTS",
            swiftSettings: [
                .unsafeFlags(["-O"])  // release optimization
            ]
        ),
    ]
)
```

### Source File Structure

```
Sources/KokoroTTS/
  main.swift              - Entry point, @main KokoroCLI : AsyncParsableCommand
  CLI.swift               - Argument definitions (--text, --voice, --speed, --output, --model-dir)
  ModelPaths.swift        - Path resolution, bucket selection, model directory validation
  Lexicon.swift           - Dictionary loader (us_gold.json, us_silver.json lookups)
  Tokenizer.swift         - Text -> IPA phonemes -> token ID array
  VoiceLoader.swift       - Load voices/<name>.json -> [Float32] of shape [256]
  KokoroInference.swift   - CoreML model loading, prediction, alignment matrix
  AudioWriter.swift       - Float32 PCM array -> WAV Data (RIFF header + PCM body)
  TextChunker.swift       - Sentence/clause splitting with token budget awareness
  WavHeader.swift         - RIFF WAV header construction (44-byte struct)
```

### Key Code Patterns

#### Loading a CoreML model from a path

```swift
import CoreML

func loadModel(at url: URL, preferANE: Bool = false) throws -> MLModel {
    let config = MLModelConfiguration()
    config.computeUnits = preferANE ? .all : .cpuAndGPU
    return try MLModel(contentsOf: url, configuration: config)
}
```

Note: The `.mlmodelc` directory is passed directly. CoreML will use the pre-compiled form with no warmup compilation. If you pass an `.mlpackage`, CoreML must compile it on first run (adds 2-15 seconds on first use).

#### Building an MLMultiArray for input

```swift
import CoreML

func makeInt32Array(values: [Int32], shape: [Int]) throws -> MLMultiArray {
    let nsShape = shape.map { NSNumber(value: $0) }
    let array = try MLMultiArray(shape: nsShape, dataType: .int32)
    for (i, v) in values.enumerated() {
        array[i] = NSNumber(value: v)
    }
    return array
}

func makeFloat32Array(values: [Float], shape: [Int]) throws -> MLMultiArray {
    let nsShape = shape.map { NSNumber(value: $0) }
    let array = try MLMultiArray(shape: nsShape, dataType: .float32)
    // Fast path using withUnsafeMutableBytes for large arrays:
    array.withUnsafeMutableBytes { ptr, strides in
        let floatPtr = ptr.bindMemory(to: Float.self)
        for (i, v) in values.enumerated() {
            floatPtr[i] = v
        }
    }
    return array
}
```

#### Running inference

```swift
func runDurationModel(
    model: MLModel,
    tokenIDs: [Int32],      // padded to 128
    attentionMask: [Int32], // 1s then 0s, length 128
    refS: [Float],          // length 256
    speed: Float
) throws -> MLFeatureProvider {
    let inputIDs = try makeInt32Array(values: tokenIDs, shape: [1, 128])
    let mask     = try makeInt32Array(values: attentionMask, shape: [1, 128])
    let refArr   = try makeFloat32Array(values: refS, shape: [1, 256])
    let speedArr = try makeFloat32Array(values: [speed], shape: [1])

    let features = try MLDictionaryFeatureProvider(dictionary: [
        "input_ids":      MLFeatureValue(multiArray: inputIDs),
        "attention_mask": MLFeatureValue(multiArray: mask),
        "ref_s":          MLFeatureValue(multiArray: refArr),
        "speed":          MLFeatureValue(multiArray: speedArr),
    ])
    return try model.prediction(from: features)
}
```

The exact input key names must be verified by inspecting the actual `.mlmodelc` metadata. Use `mlmodelc_inspector` or CoreML Tools:

```bash
python3 -c "
import coremltools as ct
m = ct.models.MLModel('~/.ai-tts/models/kokoro/kokoro_24_10s.mlmodelc')
print(m.get_spec().description)
"
```

#### WAV file writing (no AudioToolbox required)

WAV is a dead-simple format. We write the 44-byte RIFF header then raw PCM. No framework needed.

```swift
struct WavWriter {
    static func makeWav(samples: [Float], sampleRate: Int = 24000) -> Data {
        let numSamples = samples.count
        let numChannels: Int32 = 1
        let bitsPerSample: Int32 = 16
        let byteRate = Int32(sampleRate) * numChannels * bitsPerSample / 8
        let blockAlign: Int16 = Int16(numChannels) * Int16(bitsPerSample / 8)
        let dataSize = Int32(numSamples * 2)  // 2 bytes per sample (Int16)
        let chunkSize = 36 + dataSize

        var data = Data()

        func append<T: FixedWidthInteger>(_ value: T) {
            var v = value.littleEndian
            data.append(contentsOf: withUnsafeBytes(of: &v) { Array($0) })
        }

        // RIFF header
        data.append(contentsOf: "RIFF".utf8)
        append(chunkSize)
        data.append(contentsOf: "WAVE".utf8)

        // fmt chunk
        data.append(contentsOf: "fmt ".utf8)
        append(Int32(16))           // fmt chunk size
        append(Int16(1))            // PCM format
        append(Int16(numChannels))
        append(Int32(sampleRate))
        append(byteRate)
        append(blockAlign)
        append(bitsPerSample)

        // data chunk
        data.append(contentsOf: "data".utf8)
        append(dataSize)

        // PCM samples: convert Float32 [-1.0, 1.0] to Int16
        for sample in samples {
            let clamped = max(-1.0, min(1.0, sample))
            let pcm = Int16(clamped * 32767.0)
            append(pcm)
        }

        return data
    }
}
```

#### Writing to stdout vs file

```swift
let wavData = WavWriter.makeWav(samples: allSamples)

if let outputPath = arguments.output {
    try wavData.write(to: URL(fileURLWithPath: outputPath))
} else {
    FileHandle.standardOutput.write(wavData)
}
```

---

## 6. CLI Interface

```
USAGE: kokoro-tts [--text <text>] [--voice <voice>] [--speed <speed>]
                  [--output <output>] [--model-dir <dir>] [--list-voices]

OPTIONS:
  --text <text>         Text to synthesize (reads stdin if omitted)
  --voice <voice>       Voice preset name (default: af_heart)
  --speed <speed>       Speech rate 0.5-2.0 (default: 1.0)
  --output <path>       Output WAV file path (writes stdout if omitted)
  --model-dir <dir>     Model directory (default: ~/.ai-tts/models/kokoro)
  --compute <units>     Compute units: all, cpuAndGPU, cpuOnly (default: all)
  --list-voices         Print available voice names and exit
  --version             Print version and exit
  -h, --help            Show help
```

### Reading from stdin

```swift
var text: String
if let inputText = arguments.text {
    text = inputText
} else {
    var lines: [String] = []
    while let line = readLine(strippingNewline: false) {
        lines.append(line)
    }
    text = lines.joined()
}
guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
    fputs("kokoro-tts: no input text\n", stderr)
    exit(1)
}
```

---

## 7. Tokenizer Implementation (Option B: Dictionary + eSpeak)

This is the recommended v1 approach. No MLX dependency.

### vocab_index.json structure

The `vocab_index.json` file maps IPA phoneme characters/digraphs to integer IDs. Expected format:
```json
{
  "ə": 1,
  "h": 2,
  "l": 3,
  "O": 4,
  ...
}
```

Token ID 0 is reserved for padding. The sequence must be padded to length 128.

### Lexicon lookup

```swift
struct Lexicon {
    let goldDict: [String: String]   // word -> IPA string
    let silverDict: [String: String]
    let vocab: [String: Int]         // IPA char/digraph -> token ID

    func tokenize(_ word: String) -> [Int]? {
        let lower = word.lowercased()
        let ipa = goldDict[lower] ?? silverDict[lower]
        guard let ipa else { return nil }
        return ipaToTokens(ipa)
    }

    func ipaToTokens(_ ipa: String) -> [Int] {
        // Greedy longest-match against vocab keys
        var result: [Int] = []
        var i = ipa.startIndex
        while i < ipa.endIndex {
            var matched = false
            for len in [2, 1] {  // try digraphs first
                let end = ipa.index(i, offsetBy: len, limitedBy: ipa.endIndex) ?? ipa.endIndex
                let substr = String(ipa[i..<end])
                if let id = vocab[substr] {
                    result.append(id)
                    i = end
                    matched = true
                    break
                }
            }
            if !matched { i = ipa.index(after: i) }  // skip unknown char
        }
        return result
    }
}
```

### eSpeak fallback for OOV words

```swift
func espeakPhonemes(for word: String) -> String? {
    let process = Process()
    process.executableURL = URL(fileURLWithPath: "/opt/homebrew/bin/espeak-ng")
    process.arguments = ["-q", "--ipa", "-v", "en-us", word]
    let pipe = Pipe()
    process.standardOutput = pipe
    process.standardError = Pipe()  // suppress warnings
    try? process.run()
    process.waitUntilExit()
    let data = pipe.fileHandleForReading.readDataToEndOfFile()
    return String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines)
}
```

eSpeak path probe order: `/opt/homebrew/bin/espeak-ng`, `/usr/local/bin/espeak-ng`, `which espeak-ng` result.

### Full tokenization flow

```swift
func tokenize(text: String, lexicon: Lexicon) -> [Int32] {
    let words = tokenizeText(text)  // split on whitespace/punctuation
    var tokenIDs: [Int32] = []

    for word in words {
        if let ids = lexicon.tokenize(word) {
            tokenIDs.append(contentsOf: ids.map { Int32($0) })
        } else if let ipa = espeakPhonemes(for: word),
                  !ipa.isEmpty {
            let ids = lexicon.ipaToTokens(ipa)
            tokenIDs.append(contentsOf: ids.map { Int32($0) })
        }
        // OOV with no eSpeak result: skip (produces silence for that word)
    }

    return tokenIDs
}
```

---

## 8. Alignment Matrix Construction

This is the "glue code" between the duration model and the decoder. It's the most mathematically fiddly part.

```swift
func buildAlignmentMatrix(durations: [Int], tokenCount: Int, frameCount: Int) -> [[Float]] {
    // pred_aln_trg shape: [tokenCount, frameCount]
    var matrix = Array(repeating: Array(repeating: Float(0), count: frameCount), count: tokenCount)
    var frameIdx = 0
    for (tokenIdx, dur) in durations.enumerated() {
        for _ in 0..<dur {
            if frameIdx < frameCount {
                matrix[tokenIdx][frameIdx] = 1.0
                frameIdx += 1
            }
        }
    }
    return matrix
}

func matMul(a: [[Float]], b: [[Float]]) -> [[Float]] {
    // a: [M, K], b: [K, N] -> result: [M, N]
    // Use Accelerate.framework vDSP for production performance
    let M = a.count, K = a[0].count, N = b[0].count
    var result = Array(repeating: Array(repeating: Float(0), count: N), count: M)
    for i in 0..<M {
        for j in 0..<N {
            var sum: Float = 0
            for k in 0..<K {
                sum += a[i][k] * b[k][j]
            }
            result[i][j] = sum
        }
    }
    return result
}
```

For production, replace the naive matMul with `Accelerate.framework` `cblas_sgemm`.

---

## 9. Memory Management

Benchmark numbers from the FluidInference HuggingFace README on M4 Pro (48 GB):
- PyTorch CPU: 4.85 GB peak
- MLX: 3.37 GB peak
- Swift + CoreML: **1.503 GB peak**

The CoreML version is already the winner. To stay under 1.5 GB:

1. Load one model at a time. The duration model and decoder should not both be resident simultaneously during steady-state operation. Load duration model -> run -> extract outputs -> unload -> load decoder -> run -> unload.

2. Voice embeddings are only 256 floats (1 KB). Load on demand, keep in memory.

3. Lexicon JSON files total ~10 MB uncompressed. Load once at startup, keep resident.

4. Do not use `MLModel` with `.all` compute units for the duration model. Forcing `.cpuAndGPU` prevents the ANE from caching an additional copy.

5. For long text (many chunks), process chunks sequentially and free the MLMultiArray outputs before loading the next chunk.

```swift
// Explicit release pattern
var result: MLFeatureProvider? = try model.prediction(from: input)
let waveform = extractWaveform(from: result!)
result = nil  // allow ARC to release
```

---

## 10. Build System

### Prerequisites

```bash
# macOS 15+, Xcode 16+ (for Swift 6)
xcode-select --install

# eSpeak-NG for OOV phonemization (optional but recommended)
brew install espeak-ng

# Python for model download only (not runtime dependency)
pip install huggingface_hub
```

### Build commands

```bash
# Debug build
swift build

# Release build (required for production performance)
swift build -c release

# Output binary location
.build/release/kokoro-tts

# Run tests
swift test

# Install to local bin
cp .build/release/kokoro-tts /usr/local/bin/kokoro-tts
# or:
install -m 755 .build/release/kokoro-tts /usr/local/bin/
```

### Verify CoreML model input names before writing inference code

```bash
python3 -c "
import coremltools as ct
import sys

path = sys.argv[1] if len(sys.argv) > 1 else \
    '~/.ai-tts/models/kokoro/kokoro_24_10s.mlmodelc'

m = ct.models.MLModel(path)
spec = m.get_spec()
print('=== INPUTS ===')
for inp in spec.description.input:
    print(f'{inp.name}: {inp.type}')
print('=== OUTPUTS ===')
for out in spec.description.output:
    print(f'{out.name}: {out.type}')
" ~/.ai-tts/models/kokoro/kokoro_24_10s.mlmodelc
```

Run this before writing inference code. Input key names in the FluidInference variant may differ from the mattmireles variant.

---

## 11. Model Download Script

```bash
#!/usr/bin/env bash
# scripts/setup-kokoro-models.sh
set -euo pipefail

MODEL_DIR="${KOKORO_MODEL_DIR:-$HOME/.ai-tts/models/kokoro}"
REPO="FluidInference/kokoro-82m-coreml"

echo "Downloading Kokoro-82M CoreML models to $MODEL_DIR..."
mkdir -p "$MODEL_DIR"

python3 - <<'PYEOF'
import os, sys
from pathlib import Path

try:
    from huggingface_hub import snapshot_download
except ImportError:
    print("ERROR: huggingface_hub not installed. Run: pip install huggingface_hub")
    sys.exit(1)

model_dir = os.environ.get("KOKORO_MODEL_DIR",
    str(Path.home() / ".ai-tts/models/kokoro"))

snapshot_download(
    repo_id="FluidInference/kokoro-82m-coreml",
    local_dir=model_dir,
    ignore_patterns=[
        "*.mlpackage",          # skip source packages, keep compiled .mlmodelc
        "kokoro_21_*",          # skip older v21 variants
        "gb_gold.json",         # skip UK English (add if needed)
        "gb_silver.json",
        "us_lexicon_cache.json", # 30 MB cache, optional
    ]
)
print(f"Models downloaded to {model_dir}")
PYEOF
```

Estimated download after filtering: ~1.2 GB (two .mlmodelc files + voices + US dictionaries).

---

## 12. Integration with claude-code-tts

The immediate integration path into the existing `claude-code-tts` Piper workflow:

1. `speak-response.sh` currently pipes text to `piper --model ... --output_raw | aplay`
2. Replace that with: `echo "$text" | kokoro-tts --voice af_heart --speed 1.2 | afplay -`
3. Or write to temp file: `kokoro-tts --text "$text" --output /tmp/tts_$$.wav && afplay /tmp/tts_$$.wav`

The `tts-audition.sh` and `tts-speak.sh` scripts would become thin wrappers around `kokoro-tts`.

The `--voice` flag maps to the existing persona system. Voice preset names from the persona config would be passed directly to `kokoro-tts --voice <name>`.

---

## 13. Gotchas and Known Issues

### The FluidInference model vs. mattmireles model are different binaries

FluidInference's `kokoro_24_10s.mlmodelc` is a single compiled model that handles both duration prediction and decoding internally. The mattmireles conversion produces separate `kokoro_duration.mlpackage` and `KokoroDecoder_HAR_3s.mlpackage`. These are not interchangeable. Use FluidInference's model since it is already compiled and benchmarked.

**Risk:** If FluidInference's model is truly a single stage (not two-stage), the alignment matrix construction described in Section 8 may not apply. The model may accept text tokens directly and output waveform directly. Verify with the Python introspection command in Section 10 before writing any inference code.

### .mlpackage vs .mlmodelc

Always use `.mlmodelc` (pre-compiled). If you load `.mlpackage`, CoreML compiles it on the calling thread on first use - this can block for 2-15 seconds. The HuggingFace repo provides both; we only need `.mlmodelc`.

### Async model loading

`MLModel(contentsOf:configuration:)` is synchronous and blocks. Wrap it in a `Task` or dispatch queue if building any UI or streaming interface. For a pure CLI tool that runs to completion, synchronous is fine.

### arm64 only

MisakiSwift (if used) ships arm64-only binaries. The compiled binary will not run on Intel Macs. Document this clearly. Our target use case (Apple Silicon Macs, M1+) makes this acceptable.

### macOS 15.0 minimum

The `MLTensor` API (new in macOS 15 / WWDC 2024) is not required - we use `MLMultiArray` which works on macOS 12+. However, MisakiSwift requires macOS 15. If we avoid MisakiSwift in v1, we could target macOS 13+ with the dictionary-only tokenizer.

### CoreML model name keys

The exact string keys for model inputs (`"input_ids"`, `"attention_mask"`, etc.) must match what the compiled model expects. These are set at conversion time and are not documented anywhere other than the model's own spec. Always verify with the Python introspection command before writing Swift inference code.

### eSpeak-NG path on different setups

`/opt/homebrew/bin/espeak-ng` is the arm64 Homebrew path. Intel Homebrew is `/usr/local/bin/`. Always probe both. If neither exists, log a warning and continue without eSpeak (OOV words produce silence).

### Speed parameter clamping

The model was trained with speed in the range [0.5, 2.0]. Values outside this range produce artifacts. Clamp in the CLI before passing to inference.

### WAV output to stdout and terminal detection

If the user runs `kokoro-tts "hello"` without `--output` and without piping stdout, they will receive raw WAV binary in their terminal. Detect `isatty(STDOUT_FILENO)` and warn:

```swift
import Darwin
if arguments.output == nil && isatty(STDOUT_FILENO) != 0 {
    fputs("kokoro-tts: warning: writing binary WAV to terminal. Use --output or pipe to afplay.\n", stderr)
}
```

---

## 14. Phased Implementation Plan

**Phase 1 (MVP):** Build the scaffold and verify the model loads.
- Package.swift with ArgumentParser
- ModelPaths.swift - resolve model directory, verify files exist
- Minimal CLI that loads the .mlmodelc and prints input/output spec to stderr
- WavWriter.swift - write a 1-second silence WAV to verify output pipeline

**Phase 2:** Wire up inference with hardcoded test input.
- VoiceLoader.swift - load af_heart.json, parse to [Float32]
- KokoroInference.swift - call the model with test token IDs
- Verify waveform output is non-zero and sounds like speech

**Phase 3:** Implement tokenizer.
- Load us_gold.json + us_silver.json + vocab_index.json
- Implement word-level lookup
- Add eSpeak subprocess fallback
- End-to-end: "Hello world" -> WAV file

**Phase 4:** Long text support.
- TextChunker.swift
- Chunk concatenation with crossfade

**Phase 5:** Integration and polish.
- Wire into speak-response.sh
- Add `--list-voices`
- Add `KOKORO_MODEL_DIR` env var support
- Error messages for missing models, bad voice names, etc.

---

## 15. Reference Links

- [FluidInference/kokoro-82m-coreml on HuggingFace](https://huggingface.co/FluidInference/kokoro-82m-coreml)
- [FluidInference/FluidAudio on GitHub](https://github.com/FluidInference/FluidAudio)
- [FluidAudio TTS documentation](https://docs.fluidinference.com/tts/kokoro)
- [mattmireles/kokoro-coreml - conversion pipeline + two-stage architecture detail](https://github.com/mattmireles/kokoro-coreml)
- [mlalma/kokoro-ios - Swift MLX implementation](https://github.com/mlalma/kokoro-ios)
- [mlalma/MisakiSwift - Swift G2P engine](https://github.com/mlalma/MisakiSwift)
- [hexgrad/Kokoro-82M - original model](https://huggingface.co/hexgrad/Kokoro-82M)
- [hexgrad/misaki - Python G2P engine](https://github.com/hexgrad/misaki)
- [apple/swift-argument-parser](https://github.com/apple/swift-argument-parser)
- [MLModelConfiguration Apple docs](https://developer.apple.com/documentation/coreml/mlmodelconfiguration)
- [FluidAudio benchmarks](https://github.com/FluidInference/FluidAudio/blob/main/Documentation/Benchmarks.md)
