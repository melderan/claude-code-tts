---
description: List every voice provider's voices — installed, available, who uses them, how to hear one
disable-model-invocation: true
---

```bash
claude-tts voices $ARGUMENTS
```

Read the output above: it is for you, a sibling Claude choosing how to sound. Each provider
section ends with the command that plays a voice. To hear one, run it; to keep one, follow the
`persona add` line at the top, then `/tts-persona <name>` in this session.

`/tts-voices --clashes` lists only the voices two or more personas share (same engine, same
speaker), with the sessions speaking as each. It changes nothing; who keeps a voice is decided
by ear, with the friend it clashes with.
