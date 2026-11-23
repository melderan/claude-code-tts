#!/bin/bash
# PostToolUse: exec claude-tts speak --from-hook --hook-type post_tool_use, except where that CLI would speak nothing
# and write nothing the skip would lose. Then this gate exits 0 and starts no Python. Each check names the cli.py line
# it mirrors (HEAD, 9.92.1). A payload or state this shell cannot read plainly runs the CLI, as before. A parent hook
# and a subagent hook are judged alike: the transcript decides. CLAUDE_TTS_GATE=off runs every call through the CLI.
# Geordi's gate (2026-11-21), ported and rechecked; it reads the claude ancestor with ps, as session.py does, so it
# runs the same on macOS and Linux.

# The payload, read once. The trailing x keeps trailing newlines, which $(...) would strip.
input=$(cat; printf x)
input=${input%x}

# json_str KEY: the value of a plain string KEY, given exactly once in the payload; else return 1 (doubt).
json_str() {
  local key=$1 q='"' re rest
  re="\"$key\"[[:space:]]*:[[:space:]]*\"([^\"\\\\]*)\""
  rest=${input#*"$q$key$q"}
  case $rest in *"$q$key$q"*) return 1 ;; esac
  [[ $input =~ $re ]] || return 1
  JS_VAL=${BASH_REMATCH[1]}
}

# claude_pid: the nearest ancestor of $PPID named claude, 20 hops (session.py:57-79). ps -o ppid=,comm= is the query
# _ps_query runs (session.py:35-56); a comm's directory part is dropped, as Path(comm).name does.
claude_pid() {
  local pid=$PPID i info ppid comm
  for ((i = 0; i < 20; i++)); do
    [ "$pid" -gt 1 ] 2>/dev/null || return 1
    info=$(ps -o ppid=,comm= -p "$pid" 2>/dev/null) || return 1
    read -r ppid comm <<<"$info" || return 1
    if [ "${comm##*/}" = claude ]; then CPID=$pid; return 0; fi
    pid=$ppid
  done
  return 1
}

# post_ok: the skips after the pins (cli.py:3066, 3078). This session's two pins under ~/.claude-tts/active must hold
# this session's ids (session.py:93-145), so the CLI would write them as they are, and this session's sessions.d file
# must exist, so neither _inherit_session_settings (cli.py:2766) nor migrate_session (config.py:377-403, from
# load_config at config.py:460) has a write left. Two writes a skip leaves to the last CLI run, on purpose: the
# breadcrumb active/latest.session (session.py:119-122) and the voice card voice.d/<sid>.json (config.py:475, 542).
post_ok() {
  [ -n "$cs" ] && [ -n "$sid" ] || return 1
  claude_pid || return 1
  local h pv cv act=$HOME/.claude-tts/active
  pv=''; cv=''
  h=$(uname -n 2>/dev/null) || return 1
  h=${h%%.*}; [ -n "$h" ] || h=host
  [[ $h =~ ^[A-Za-z0-9_-]+$ ]] || return 1
  h=${h:0:64}
  read -r pv 2>/dev/null < "$act/$h-$CPID.session" || [ -n "$pv" ] || return 1
  read -r cv 2>/dev/null < "$act/$h-$CPID.claude-session" || [ -n "$cv" ] || return 1
  [ "$pv" = "$sid" ] && [ "$cv" = "$cs" ] || return 1
  [ -e "$HOME/.claude-tts/sessions.d/$sid.json" ]
}

# own_muted: this Claude session is muted on its own (cli.py:3079-3082; claude_session_own, config.py:271-281). Only a
# names file in the shape atomic_write_json writes (config.py:177-196: json.dump indent=2, then a newline) with one
# "muted": true line counts. Any other shape is doubt.
own_muted() {
  [[ $cs =~ ^[0-9A-Za-z-]{8,64}$ ]] || return 1  # CLAUDE_SESSION_ID_RE, config.py:258
  local nf=$HOME/.claude-tts/names/$cs.json
  [ -f "$nf" ] || return 1
  [ "$(grep -c -E '"muted"' "$nf")" = 1 ] || return 1
  grep -q -E '^  "muted": true,?$' "$nf" || return 1
  ! grep -q -v -E '^(\{|\}|  "[A-Za-z_][A-Za-z0-9_]*": ("[^"\\]*"|true|false|null|-?[0-9][0-9.eE+-]*),?)$' "$nf"
}

skip_ok() {
  # cli.py:3044 CLAUDE_TTS_ENABLED other than "1" returns before any write.
  [ "${CLAUDE_TTS_ENABLED-1}" = 1 ] || return 0
  # cli.py:3028-3030 empty stdin returns; cli.py:3038-3041 no transcript path, or no file there, returns.
  [ -n "$input" ] || return 0
  json_str transcript_path || return 1
  tp=$JS_VAL
  [ -n "$tp" ] && [ -f "$tp" ] || return 0
  # cli.py:3070-3071 the Claude session id, a string or none. cli.py:3049-3060 the session: CLAUDE_TTS_SESSION (its
  # inheritance at cli.py:3051 writes only a sessions.d file that does not exist yet, which post_ok rules out), else
  # the /projects/<id>/ folder of the path; none returns before any write.
  cs=
  if json_str session_id; then cs=$JS_VAL; fi
  sid=${CLAUDE_TTS_SESSION-}
  if [ -z "$sid" ]; then
    [[ $tp =~ /projects/([^/]+)/ ]] || return 0
    sid=${BASH_REMATCH[1]}
  fi
  # cli.py:3079-3082 muted on its own: the CLI returns after the pins.
  if own_muted; then post_ok; return; fi
  # cli.py:3112-3113 the watermark is /tmp/claude_tts_spoken_<stem>.state, stem = Path(tp).stem; a name that is not
  # <stem>.jsonl is doubt.
  case $tp in -*) return 1 ;; esac
  local base=${tp##*/} stem sf wm=0 n region
  case $base in ?*.jsonl) stem=${base%.jsonl} ;; *) return 1 ;; esac
  sf=/tmp/claude_tts_spoken_$stem.state
  # cli.py:3835-3847 _load_watermark: no file is 0; digits (int() of the stripped text) are the mark; else doubt.
  if [ -e "$sf" ]; then
    read -r wm < "$sf" || [ -n "$wm" ] || return 1
    [[ $wm =~ ^[0-9]{1,12}$ ]] || return 1
    wm=$((10#$wm))
  fi
  # cli.py:3132-3134 and 4052: the lines from index wm on are the new ones.
  region=$(tail -n "+$((wm + 1))" "$tp") || return 1
  if [ -z "$region" ]; then
    # cli.py:3143-3154 nothing new: the same three rereads, 0.2 s apart (cli.py:3640-3641).
    for n in 1 2 3; do
      sleep 0.2
      region=$(tail -n "+$((wm + 1))" "$tp") || return 1
      [ -n "$region" ] && break
    done
    if [ -z "$region" ]; then
      # cli.py:3156-3159 still nothing new: the CLI returns after the quiet mark. Only an equal count qualifies; a lower
      # one is the reset at cli.py:3883-3886, which writes. cli.py:3803-3809 counts a last unterminated line too, as grep does.
      n=$(grep -c '' "$tp") || return 1
      [ "$n" = "$wm" ] || return 1
      post_ok; return
    fi
  fi
  # cli.py:3165-3169 and 4018-4096: no speech comes from the new lines when no assistant line (4057) has a text block
  # (4076-4079), a string content (4066-4070) or a non-empty thinking block (4080-4087). Any of those, or a \u escape
  # that could hide one, is doubt. Lines that are not assistant lines are skipped by the CLI too.
  local asst
  asst=$(grep -E '"type"[[:space:]]*:[[:space:]]*"assistant"' <<<"$region")
  if [ -n "$asst" ] && grep -q -E '"type"[[:space:]]*:[[:space:]]*"text"|"thinking"[[:space:]]*:[[:space:]]*"[^"]|"content"[[:space:]]*:[[:space:]]*"|\\u' <<<"$asst"; then
    return 1
  fi
  post_ok
}

if [ "${CLAUDE_TTS_GATE:-on}" != off ] && skip_ok; then
  exit 0
fi
exec claude-tts speak --from-hook --hook-type post_tool_use <<<"$input"
