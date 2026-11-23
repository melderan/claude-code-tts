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
plists = [n for n in names if n.endswith("/services/com.claude-tts.daemon.plist")]
assert len(hooks) >= 4 and len(cmds) >= 14, (hooks, cmds)
assert len(plists) == 1, plists
print(f"wheel ships {len(hooks)} hooks, {len(cmds)} commands and the LaunchAgent template")
PY
# The installed package finds its LaunchAgent template with no checkout beside it (-I: no cwd, no user site).
"$T/tools/claude-code-tts/bin/python" -I -c '
from claude_code_tts.daemon_service import service_template
template = service_template("com.claude-tts.daemon.plist")
assert template is not None and template.is_file(), "LaunchAgent template not found in the installed package"
print("installed package finds the LaunchAgent template")
'
mkdir -p "$T/home/.claude" && echo '{}' > "$T/home/.claude/settings.json"
out=$(HOME=$T/home PATH=$T/bin:$PATH "$T/bin/claude-tts-install" --dry-run 2>&1 || true)
if echo "$out" | grep -q "Source .* missing"; then
    echo "installer cannot find its hooks/commands"
    exit 1
fi
echo "cold install ok"
rm -rf "$T"
