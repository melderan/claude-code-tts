---
description: Show every stored TTS config key next to the shipped default; CHANGED marks the ones that differ
disable-model-invocation: true
---

```bash
claude-tts config $ARGUMENTS
```

Report the output to the user. A key marked CHANGED holds a value the file kept while the shipped
default moved on, or one someone set on purpose; say which it looks like. `--changed` shows only those.
