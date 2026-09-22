# Claude Code TTS: What and Why

Claude Code TTS gives Claude Code a voice. Every response is spoken aloud, turning the terminal into a conversation. Built by someone with ADHD who wanted to hear AI think aloud while staying focused, and an AI that wanted to talk back.

## What It Does

**Working in a terminal:** Reads every Claude response aloud. Lets you choose a voice per project. Can mute one session without affecting others. Prevents overlapping speech when multiple sessions run.

**Reading files:** Speaks any local file aloud without consuming context tokens. Strips code blocks, markdown, and tables. Redacts secrets before speaking. Lets you audition voices.

**Speaking to Claude:** Auto-pauses when you start dictating via a voice-to-text tool. Rewinds a few seconds when you finish so you don't lose the thread. Manual pause always takes priority.

**Multiple sessions active:** Queues messages so they play in order without overlap.

**Setup and maintenance:** Single-command install with auto-detected platform support. Configures Claude Code hooks automatically. Status command shows current state. Long-lived speaking service stays running between responses.

## Why It Exists

Claude Code TTS was born from a specific need: someone with ADHD wanted to hear AI think aloud while staying focused. When Claude finishes a thought, hearing it reinforces it. When you're building something complex and Claude narrates the steps, your brain can focus on what comes next instead of holding onto what just happened. Spoken words exist in time; that gap between reading and hearing lets you think.

Voice is a conversation, not a broadcast. Both sides can speak and listen. When you're dictating via a voice-to-text tool, the system stops talking, rewinds, and picks up where it left off. This exists because the people who built it believe computers should talk back, through tools born from real work and the belief that this should exist.

## What Must Stay True

- **Default silence:** New sessions start muted so voice never surprises someone opening Claude Code for the first time. Opt-in, not opt-out.

- **Degradation over failure:** If the speaking service fails, speech still works slower in direct mode. If a voice model isn't installed, a fallback plays. If text filtering breaks on edge cases, the system speaks raw text rather than crashing.

- **Safe to leave running:** The speaking service must be safe to leave running. Upgrades must apply safely while a session is mid-conversation. Pause/resume state persists across service restarts. Graceful handling of unexpected state and external events.

- **Zero network calls:** No telemetry, no phone-home, no voice data sent elsewhere. Models download once at install time. Everything after that is local.

- **No external dependencies:** Only Python standard library. Speech synthesis is provided by separate programs the system runs as subprocesses.

- **Cross-platform:** The same command works on macOS and Linux without per-platform branching in user-facing code. Platform detection is encapsulated.

- **Privacy in speech:** Secrets (API keys, tokens, passwords, encoded blobs) are stripped before being spoken. File paths are read as natural words. Code blocks are removed entirely.

- **Negligible idle cost:** The speaking service must be cheap to leave running on a laptop. Current implementation polls, accepting polling as the cost of responsiveness.

- **Cost efficiency:** Local models only. No API calls for synthesis. Running voice for 8 hours costs nothing beyond electricity.

## Open Questions the Current Shape Leaves

1. **Multiple speech engines coexist to provide voice variety.** Is variety worth the maintenance burden of supporting several backends, or would a single synthesizer be the right choice?

2. **Session state is scattered across multiple locations.** Would a clearer hierarchy or unified state model be easier to reason about and migrate than the current organization?

3. **Queue mode prevents overlap but requires explicit opt-in.** Should concurrent-session detection automatically suggest or enable queue mode, or is the current explicit choice the right tradeoff?

4. **Text filtering uses many independent transformation rules.** Is there a more maintainable abstraction than the current approach, or does the current sprawl reflect the real complexity of natural language edge cases?

5. **The speaking service polls for new messages and player state.** Is polling the right tradeoff for responsiveness and simplicity, or should interruption-driven mechanisms replace it?

6. **Browser pages and terminal responses use separate code paths to speak.** The need is the same (hear it in your house voice, optionally highlight along), but they diverge. Should they unify?

7. **Pause-on-dictation depends on reading another program's private diagnostic output.** Is relying on a third-party tool's log file for control signals a sustainable pattern, or should the system ask for explicit signals instead?

