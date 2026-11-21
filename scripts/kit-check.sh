#!/usr/bin/env bash
# kit-check.sh - the kit's hook shims are byte copies of hooks/, and its config is valid JSON.
# One script for ci (the "kit shims match hooks/" step) and the local gate, so the two cannot drift:
# 9.92.0 changed hooks/speak-intermediate.sh, passed the local gate, and failed only on GitHub here.

set -euo pipefail
cd "$(dirname "$0")/.."

for f in speak-response.sh speak-intermediate.sh; do
    cmp "hooks/$f" "kits/claude-tts/files/home/.claude/hooks/$f"
done
python3 -c "import json; json.load(open('kits/claude-tts/files/home/.config/claude/claude-tts.json'))"
