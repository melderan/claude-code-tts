#!/bin/bash
# Runs inside the clean-install container. TAG is the git ref to install (default: main).
# Every step a first-time Linux user takes, asserted. Exit 1 on any failure.
set -uo pipefail
TAG="${TAG:-main}"
REPO="${REPO:-https://github.com/melderan/claude-code-tts}"
PASS=0; FAIL=0
ok()   { echo "  PASS: $1"; PASS=$((PASS+1)); }
bad()  { echo "  FAIL: $1"; echo "$2" | tail -25 | sed "s/^/        /"; FAIL=$((FAIL+1)); }

if [ -n "${WHEEL:-}" ]; then
  echo "== 1. uv tool install from wheel $WHEEL"
  out=$(uv tool install --force "$WHEEL" 2>&1); rc=$?
else
  echo "== 1. uv tool install $REPO@$TAG"
  out=$(uv tool install --force --build "git+$REPO@$TAG" 2>&1); rc=$?
fi
[ $rc -eq 0 ] && ok "uv tool install exits 0" || bad "uv tool install failed" "$out"
command -v claude-tts >/dev/null && ok "claude-tts on PATH" || bad "claude-tts not on PATH" "$PATH"
command -v claude-tts-install >/dev/null && ok "claude-tts-install on PATH" || bad "claude-tts-install missing" ""
echo "   version: $(claude-tts --version 2>&1)"

echo "== 2. installer preflight sees its own hooks and commands (the failure in issue #1)"
out=$(claude-tts-install --dry-run 2>&1); rc=$?
if ! command -v claude-tts-install >/dev/null; then bad "preflight not run: claude-tts-install missing" "";
elif echo "$out" | grep -q "Source .* missing"; then bad "preflight reports missing sources" "$(echo "$out" | grep missing)";
elif ! echo "$out" | grep -q "PREFLIGHT"; then bad "preflight produced no checks (rc=$rc)" "$out";
else ok "preflight finds every hook and command"; fi

echo "== 3. real install (Piper via uv, voice download, hooks, settings.json)"
mkdir -p ~/.claude && echo '{}' > ~/.claude/settings.json
out=$(claude-tts-install --install 2>&1); rc=$?
[ $rc -eq 0 ] && ok "claude-tts-install --install exits 0" || bad "installer failed (rc=$rc)" "$out"
command -v piper >/dev/null && ok "piper on PATH after install" || bad "piper not on PATH after install" "$(echo "$out" | grep -i piper | head -5)"
[ -f ~/.local/share/piper-voices/en_US-hfc_male-medium.onnx ] && ok "default voice model present" || bad "voice model missing" "$(ls ~/.local/share/piper-voices 2>&1)"
for h in speak-response.sh speak-intermediate.sh; do [ -x ~/.claude/hooks/$h ] && ok "hook deployed: $h" || bad "hook missing: $h" ""; done
python3 - <<'PY' && ok "settings.json has Stop and PostToolUse hooks, async, valid JSON" || bad "settings.json hooks wrong" "$(cat ~/.claude/settings.json)"
import json, os
s = json.load(open(os.path.expanduser("~/.claude/settings.json")))
h = s["hooks"]
assert any("speak-response.sh" in k["command"] and k.get("async") is True for e in h["Stop"] for k in e["hooks"])
assert any("speak-intermediate.sh" in k["command"] and k.get("async") is True for e in h["PostToolUse"] for k in e["hooks"])
PY

echo "== 4. speech: the second failure in issue #1"
out=$(claude-tts speak 'hello from a clean machine' 2>&1); rc=$?
if ! command -v claude-tts >/dev/null; then bad "speak not run: claude-tts missing" "";
elif [ $rc -ne 0 ] || echo "$out" | grep -q "Failed to generate speech"; then bad "speak could not synthesize (rc=$rc)" "$out";
else ok "speak synthesized (playback may be silent: no audio device here)"; fi
echo "   speak said: $(echo "$out" | head -3 | tr '\n' '|')"
out=$(echo "direct synthesis check" | piper --model ~/.local/share/piper-voices/en_US-hfc_male-medium.onnx --output_file /tmp/check.wav 2>&1); 
[ -s /tmp/check.wav ] && ok "piper produced a WAV ($(stat -c %s /tmp/check.wav) bytes)" || bad "piper produced no WAV" "$out"

echo "== 5. uninstall leaves settings.json without our hooks and valid"
if [ ! -x ~/.claude/hooks/speak-response.sh ]; then bad "uninstall not tested: install did not deploy hooks" ""; else
out=$(claude-tts-install --uninstall 2>&1) || true
python3 - <<'PY' && ok "uninstall removed all TTS hooks, settings still valid JSON" || bad "uninstall left hooks or broke settings" "$(cat ~/.claude/settings.json)"
import json, os
s = json.load(open(os.path.expanduser("~/.claude/settings.json")))
txt = json.dumps(s)
assert "speak-response.sh" not in txt and "speak-intermediate.sh" not in txt and "voice-context.sh" not in txt
PY
fi

echo; echo "== $PASS passed, $FAIL failed"; [ $FAIL -eq 0 ]
