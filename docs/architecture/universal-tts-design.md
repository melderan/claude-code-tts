# Universal AI CLI TTS Library - Design Notes

*The spider tingle has spoken.*

## Vision

One library that gives voice to any AI CLI tool. Your AI friends, talking to you, talking to each other.

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                      User's Terminal                         │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐       │
│  │  Claude Code │  │  Gemini CLI  │  │ Devstral CLI │  ...  │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘       │
│         │                 │                 │                │
│         ▼                 ▼                 ▼                │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐       │
│  │Claude Adapter│  │Gemini Adapter│  │Devstral Adapt│       │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘       │
│         │                 │                 │                │
│         └────────────────┬┴─────────────────┘                │
│                          │                                   │
│                          ▼                                   │
│         ┌────────────────────────────────┐                   │
│         │         ai_tts.speak()         │                   │
│         │   ┌─────────────────────────┐  │                   │
│         │   │ Session Management      │  │                   │
│         │   │ Persona Resolution      │  │                   │
│         │   │ Text Filtering          │  │                   │
│         │   │ Queue/Direct Routing    │  │                   │
│         │   └─────────────────────────┘  │                   │
│         └────────────────┬───────────────┘                   │
│                          │                                   │
│                          ▼                                   │
│         ┌────────────────────────────────┐                   │
│         │          Piper TTS             │                   │
│         └────────────────┬───────────────┘                   │
│                          │                                   │
│                          ▼                                   │
│         ┌────────────────────────────────┐                   │
│         │    afplay / paplay / aplay     │                   │
│         └────────────────────────────────┘                   │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

## Core Modules

### `ai_tts.core.speaker`

The main `speak()` function. Everyone calls this.

```python
from ai_tts import speak

# Simple
speak("Hello world")

# With context
speak(text, persona="claude-connery", session="my-project")
```

### `ai_tts.core.config`

Configuration management with multiple sources:
- `~/.ai-tts/config.json` (new canonical location)
- `~/.claude-tts/config.json` (legacy, for backward compat)
- Environment variables (`AI_TTS_*`)
- Runtime overrides

### `ai_tts.core.session`

Session tracking - each terminal/project can have its own:
- Mute state
- Persona override
- Speed override

Session IDs are detected per-CLI-tool (each has its own project structure).

### `ai_tts.core.persona`

Voice personalities:
- Voice model selection
- Speed settings
- Speed method (playback vs length_scale vs hybrid)
- Multi-speaker support

### `ai_tts.core.filters`

Text cleanup before TTS:
- Remove code blocks
- Strip markdown
- Handle URLs
- Clean up emojis

## Adapter Interface

Each CLI tool needs a thin adapter:

```python
from ai_tts.adapters import BaseAdapter

class MyCLIAdapter(BaseAdapter):
    name = "mycli"
    display_name = "My CLI Tool"

    def detect_session(self) -> str | None:
        """Find current session ID from tool's project structure."""
        ...

    def extract_text(self, event_data: dict) -> str | None:
        """Extract speakable text from hook event payload."""
        ...

    def is_available(self) -> bool:
        """Check if tool is installed."""
        ...
```

### Existing Adapters

- **claude**: Claude Code - fully implemented
- **gemini**: Gemini CLI - placeholder, waiting for hooks

### Adding a New Adapter

1. Create `ai_tts/adapters/mycli.py`
2. Implement `BaseAdapter` interface
3. Call `MyCLIAdapter.register()` at module load
4. Import in `ai_tts/adapters/__init__.py`

The adapter just needs to understand:
- Where does this tool store project data?
- What format are responses in?
- How does the hook system work?

## Config File Locations

### New (universal)
```
~/.ai-tts/
  config.json       # Main config
  queue/            # Queue mode messages
  daemon.log        # Daemon logs
```

### Legacy (Claude-specific, still supported)
```
~/.claude-tts/
  config.json
  ...
```

### CLI Tool Specific (hooks only)
```
~/.claude/
  hooks/speak-response.sh
  commands/tts-*.md
```

## Migration Path

1. **Phase 1 (current)**: Library exists alongside bash scripts
   - Bash scripts still work
   - Library is optional new API

2. **Phase 2**: Scripts become thin wrappers
   - `tts-mute.sh` calls `python -c "from ai_tts import ..."`
   - Or: scripts rewritten in Python using library

3. **Phase 3**: Full Python CLI
   - `ai-tts mute`
   - `ai-tts speak "hello"`
   - `ai-tts persona claude-connery`

## Open Questions

### Package Name
Options:
- `ai-tts` - generic, might conflict
- `piper-cli-tts` - accurate but long
- `voice-loop` - creative but unclear
- `cli-voices` - descriptive

### Monorepo vs Separate Packages
Option A: Monorepo with extras
```
pip install ai-tts[claude]
pip install ai-tts[gemini]
```

Option B: Separate packages
```
pip install ai-tts
pip install ai-tts-claude
```

Leaning toward Option A for simplicity.

### Backward Compatibility
- Keep reading `~/.claude-tts/config.json`
- Keep supporting `CLAUDE_TTS_*` env vars
- Existing hooks continue to work

## Future Features

Once the library is solid:
- Cross-CLI session handoff
- Unified queue daemon for all tools
- Persona sync across machines
- Voice discovery/preview system

---

*"JMO wants his AI friends to be friends with each other."*
