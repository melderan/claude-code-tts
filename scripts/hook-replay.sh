#!/bin/bash
# hook-replay.sh: run the hook sequence that made one reply play three times (2026-10-01) with
# real hook processes against a throwaway HOME, and count what reached the queue.
#
#   scripts/hook-replay.sh <label> [command...]     default command: claude-tts (the installed CLI)
#   PYTHONPATH=src scripts/hook-replay.sh x python3 -c 'import sys; from claude_code_tts.cli import main; sys.argv=["claude-tts"]+sys.argv[1:]; main()'
#
# Sequence: a first turn's Stop sets the watermark; two Stops fire together for the second
# turn with the same last_assistant_message (Claude Code does this at the end of a turn that
# took a message mid-turn); the reply lands in the transcript; two PostToolUse hooks fire
# together for the next turn's first tool call. Correct output: the second turn's reply is
# spoken once. 9.39.0 spoke it three times (two Stops, then the PostToolUse that read no
# record between the other's clear and its watermark write); 9.39.2 speaks it once. Since the
# spoken store the Stops carry session_id and prompt_id as Claude Code sends them, and
# the twin that loses the claim logs "already claimed this utterance".
# Unit tests cannot sit inside the watermark lock, so this replay is the proof for that race.
set -u
label=$1; shift; [ $# -eq 0 ] && set -- claude-tts
H=$(mktemp -d); export HOME=$H TMPDIR=$H/tmp   # TMPDIR: the hook's spoken store starts empty
mkdir -p $H/.claude/projects/-x-proj $H/.claude-tts $H/tmp
T=$H/.claude/projects/-x-proj/replay-$label.jsonl
rm -f /tmp/claude_tts_spoken_replay-$label.* ; rm -rf /tmp/claude_tts_wm_replay-$label.lock
j() { python3 -c 'import json,sys; print(json.dumps(json.loads(sys.argv[1])))' "$1"; }
j '{"type":"user","message":{"content":[{"type":"text","text":"hi"}]}}' >> $T
j '{"type":"assistant","message":{"id":"m0","content":[{"type":"text","text":"an earlier reply that was spoken before"}]}}' >> $T
X="The Mac is on nine thirty nine and the first ledger row exists, so the update line is live end to end for this replay."
IN=$(python3 -c 'import json,sys; print(json.dumps({"transcript_path": sys.argv[1], "last_assistant_message": sys.argv[2], "hook_event_name":"Stop", "session_id":"replay", "prompt_id":"p1"}))' "$T" "$X")
echo "$IN" | "$@" speak --from-hook --hook-type stop >/dev/null 2>&1   # first turn's stop, sets the watermark
j '{"type":"user","message":{"content":[{"type":"text","text":"go"}]}}' >> $T
j '{"type":"assistant","message":{"id":"m1","content":[{"type":"tool_use","name":"Bash","input":{}}]}}' >> $T
j '{"type":"user","message":{"content":[{"type":"tool_result","content":"ok"}]}}' >> $T
X2="$X second turn."
IN2=$(python3 -c 'import json,sys; print(json.dumps({"transcript_path": sys.argv[1], "last_assistant_message": sys.argv[2], "hook_event_name":"Stop", "session_id":"replay", "prompt_id":"p2"}))' "$T" "$X2")
( echo "$IN2" | "$@" speak --from-hook --hook-type stop >/dev/null 2>&1 ) &
( echo "$IN2" | "$@" speak --from-hook --hook-type stop >/dev/null 2>&1 ) &
wait
ls /tmp/claude_tts_spoken_replay-$label.pending >/dev/null 2>&1 && echo "$label: pending present after the two Stops: $(cat /tmp/claude_tts_spoken_replay-$label.pending | cut -c1-30)" || echo "$label: NO pending after the two Stops"
python3 -c 'import json,sys; print(json.dumps({"type":"assistant","message":{"id":"m2","content":[{"type":"text","text":sys.argv[1]}]}}))' "$X2" >> $T
j '{"type":"user","message":{"content":[{"type":"text","text":"next"}]}}' >> $T
j '{"type":"assistant","message":{"id":"m3","content":[{"type":"tool_use","name":"Bash","input":{}}]}}' >> $T
j '{"type":"user","message":{"content":[{"type":"tool_result","content":"ok"}]}}' >> $T
IN3=$(python3 -c 'import json,sys; print(json.dumps({"transcript_path": sys.argv[1], "tool_name":"Bash", "hook_event_name":"PostToolUse", "session_id":"replay"}))' "$T")
( echo "$IN3" | "$@" speak --from-hook --hook-type post_tool_use >/dev/null 2>&1 ) & ( echo "$IN3" | "$@" speak --from-hook --hook-type post_tool_use >/dev/null 2>&1 ) & wait
echo "--- $label debug.log (stop/ptu outcomes)"; grep -E 'stop: (response from|speech queued)|already claimed this utterance|post_tool_use: (line|no assistant|speech queued)|is the response|watermark updated' $H/.claude-tts/debug.log | sed -E 's/^\[[^]]*\] //' | cut -c1-110
echo "--- $label queue files written: $(ls $H/.claude-tts/queue 2>/dev/null | wc -l)"
