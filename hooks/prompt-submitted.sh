#!/bin/bash
# UserPromptSubmit: stdout would join the prompt and exit 2 would block it, so neither leaves.
claude-tts supersede --from-hook >/dev/null 2>&1
exit 0
