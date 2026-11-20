#!/bin/bash
# UserPromptSubmit: stdout joins the prompt as the voice context; exit 2 would block the prompt, so it never leaves.
# This hook runs in step with every prompt, so it starts no Python when nothing was dictated lately (9.91.3): the
# daemon writes the epoch second of its newest analysis to ~/.claude-tts/handy_last_dictation, and a marker older
# than 180 s (the CLI's 120 s window plus 60 s for clocks) means a typed prompt with no tone to add. No marker, or a
# line that is not a number, runs the CLI as before (a daemon older than 9.91.3 writes none).
marker="$HOME/.claude-tts/handy_last_dictation"
if [ -r "$marker" ]; then
  last=""
  read -r last < "$marker" 2>/dev/null
  case "$last" in
    ''|*[!0-9]*) ;;
    *)
      now="$(date +%s)"
      if [ $((now - last)) -gt 180 ]; then
        cat >/dev/null  # take the hook's input, so its writer never meets a closed pipe
        exit 0
      fi
      ;;
  esac
fi
claude-tts handy tone-context 2>/dev/null
exit 0
