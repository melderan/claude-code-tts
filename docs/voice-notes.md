# Voice Notes - Piper TTS Knowledge Base

Hard-won knowledge about Piper voices, speed methods, and their quirks.

## Speed Methods

### playback

Generates audio at normal speed, then speeds up during playback (macOS `afplay -r`).

**Pros:**
- Simple and reliable
- Works at any speed (tested up to 3x)
- Consistent across all voices

**Cons:**
- Higher speeds raise pitch ("chipmunk effect")
- Only works on macOS (afplay specific)

### length_scale

Piper generates speech faster by reducing the `--length_scale` parameter.

**Pros:**
- Preserves natural pitch at all speeds
- Cross-platform (works on Linux/WSL)
- Deeper, more natural sound

**Cons:**
- Some voices break down at higher speeds (see voice-specific notes)
- Quality varies significantly by voice model

### hybrid (planned)

Combine both: generate with moderate length_scale, then also speed up playback.

- Get natural pitch from length_scale
- Get extra speed from playback boost
- Example: length_scale 1.5x + playback 1.3x = ~2x total with natural tone

## Voice-Specific Notes

### en_US-ryan-medium

**Status: BROKEN with length_scale**

Ryan does not work with length_scale at any speed. Even at 1.0x (no speedup), the output degenerates into unintelligible "blah blah blah" repetition. This appears to be a fundamental incompatibility with how this model was trained.

- length_scale 2.2x: Complete gibberish
- length_scale 2.0x: Complete gibberish
- length_scale 1.8x: Mostly gibberish, some words
- length_scale 1.6x: First sentence gibberish, rest spotty
- length_scale 1.0x: Still gibberish

**Recommendation:** Use playback method only with Ryan.

Test phrase that reliably triggers the bug:
> "Done. The TTS system already had claude-metermaid configured for this project directory, and I've added a reference to the persona name in CLAUDE.md so it's documented."

### en_US-joe-medium

Works well with both methods.

- length_scale: Tested up to 2.0x, sounds good
- playback: Works at any speed

Joe maintains clarity even at higher length_scale values where Ryan fails.

### en_US-hfc_male-medium

The original "claude-prime" voice. Works well with both methods.

- playback 2.0x: Classic fast chipmunk Claude
- length_scale 1.5x: Natural pitch, good clarity

### en_GB-northern_english_male-medium

Works well with playback. Length_scale untested.

### en_GB-alan-medium

Untested. Needs evaluation.

## Testing Methodology

When evaluating a voice/method combination:

1. Use a consistent test phrase (the metermaid sentence works well)
2. Start at 1.0x to establish baseline
3. Increment by 0.2x until quality degrades
4. Document the threshold where it breaks
5. Note whether it's gradual degradation or sudden failure

## Contributing

Found a quirk with a voice? Add it here! Include:
- Voice model name
- Speed method and value
- What happened (gradual degradation vs sudden failure)
- Test phrase used
- Your recommendation
