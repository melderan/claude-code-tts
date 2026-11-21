#!/bin/bash
# PostToolUse. A subagent's tool call (agent_id in the input) writes nothing to the parent transcript this hook
# reads, so while a brother works each of its calls finds what the last scan found. When the last PostToolUse found
# nothing to speak it wrote the transcript's byte size to /tmp/claude_tts_spoken_<transcript>.quiet (cli.py
# _mark_quiet); a subagent hook whose transcript is still that size, and no newer than the mark, ends here with no
# Python start (9.92.0, JMO's CPU worry 2026-11-21). Anything else, or anything this shell cannot read plainly (an
# escaped path, no mark, a size that is not a number), runs the CLI as before. A parent hook always runs it.
input="$(cat)"
re_agent='"agent_id"[[:space:]]*:[[:space:]]*"[^"]'
re_path='"transcript_path"[[:space:]]*:[[:space:]]*"([^"\\]+)"'
if [[ $input =~ $re_agent ]] && [[ $input =~ $re_path ]]; then
  transcript="${BASH_REMATCH[1]}"
  key="${transcript##*/}"
  key="${key%.jsonl}"
  mark="/tmp/claude_tts_spoken_${key}.quiet"
  if [ -f "$transcript" ] && [ -f "$mark" ] && ! [ "$transcript" -nt "$mark" ]; then
    quiet=""
    read -r quiet < "$mark" 2>/dev/null
    size="$(wc -c < "$transcript" 2>/dev/null)"
    size="${size//[[:space:]]/}"
    case "$quiet" in
      ''|*[!0-9]*) ;;
      *) [ "$size" = "$quiet" ] && exit 0 ;;
    esac
  fi
fi
exec claude-tts speak --from-hook --hook-type post_tool_use <<<"$input"
