---
description: Check installed voices against their pinned SHA-256 and look for distrusted CA roots
disable-model-invocation: true
---

```bash
claude-tts verify
```

Report the output to the user. Exit 0 means nothing is affected, 1 means something is (each
affected line carries its fix), 2 means a check could not run. Nothing is sent anywhere.
