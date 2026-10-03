---
description: Hold every other friend's voice; this session keeps speaking (add to an existing hold)
argument-hint: [--let ROOM ...]
disable-model-invocation: true
---

Run this command:

```bash
claude-tts hold --me $ARGUMENTS
```

Summarize the output to the user in one line. Everyone else queues behind the scenes until
`/tts-kraken` lets them speak.
