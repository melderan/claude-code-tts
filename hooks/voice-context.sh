#!/bin/bash
# UserPromptSubmit: stdout joins the prompt as the voice context; exit 2 would block the prompt, so it never leaves.
claude-tts handy tone-context 2>/dev/null
exit 0
