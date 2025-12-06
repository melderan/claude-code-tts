# Issue Tracking

External issues and dependencies we're tracking.

## Claude Code Issues

### #13183 - CLI hangs on paste from accessibility/voice-to-text tools
- **URL:** https://github.com/anthropics/claude-code/issues/13183
- **Status:** OPEN
- **Filed:** 2025-12-06
- **Impact:** Blocks voice-to-text workflow with TTS

**Summary:** Claude Code hangs when receiving pasted text from voice-to-text tools (like Handy). The bracketed paste sequence (`\e[200~`...`\e[201~`) is not properly consumed, causing the end marker `[201~` to appear raw in the prompt. After multiple attempts, the process hangs completely.

**Note:** Original issue incorrectly stated "macOS 15.2" but the system is actually running **macOS 26.1 Tahoe** with Darwin Kernel 25.1.0. A correction comment was added.

**Related to TTS:** This affects the voice-to-text input side of our accessibility workflow. TTS handles the output (Claude speaking), while voice-to-text handles input (user speaking to Claude).
