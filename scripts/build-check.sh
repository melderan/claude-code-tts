#!/usr/bin/env bash
# build-check.sh - build the wheel and prove a cold install of it finds its own hooks and
# commands (`just build`, also the last step of `just ci`).

set -euo pipefail
cd "$(dirname "$0")/.."

rm -rf dist && uv build
T=$(mktemp -d)
export UV_TOOL_DIR=$T/tools UV_TOOL_BIN_DIR=$T/bin
uv tool install -q dist/*.whl
"$T/bin/claude-tts" --version
python3 - dist/*.whl <<'PY'
import sys, zipfile
names = zipfile.ZipFile(sys.argv[1]).namelist()
hooks = [n for n in names if "/hooks/" in n]; cmds = [n for n in names if "/commands/" in n]
assert len(hooks) >= 4 and len(cmds) >= 14, (hooks, cmds)
print(f"wheel ships {len(hooks)} hooks and {len(cmds)} commands")
PY
mkdir -p "$T/home/.claude" && echo '{}' > "$T/home/.claude/settings.json"
out=$(HOME=$T/home PATH=$T/bin:$PATH "$T/bin/claude-tts-install" --dry-run 2>&1 || true)
if echo "$out" | grep -q "Source .* missing"; then
    echo "installer cannot find its hooks/commands"
    exit 1
fi
echo "cold install ok"
rm -rf "$T"
