#!/bin/sh
# host-python.sh - the Python that host-side just recipes run on; prints one path.
#
# First choice is the interpreter behind the installed `claude-tts` tool, so `just release` and
# the voices-* recipes run on exactly the Python the daemon runs on. Before the tool exists (a
# fresh clone) the newest Python 3.10+ on the PATH is used: macOS ships a 3.9 as `python3` and
# the package uses 3.10 syntax. Falls back to `python3` and lets it fail with a clear message.
set -eu
tool="$(command -v claude-tts 2>/dev/null || true)"
if [ -n "$tool" ]; then
    shebang="$(sed -n '1s/^#!//p' "$tool" 2>/dev/null || true)"
    case "$shebang" in
        */python*) if [ -x "$shebang" ]; then printf '%s\n' "$shebang"; exit 0; fi ;;
    esac
fi
for v in 3.15 3.14 3.13 3.12 3.11 3.10; do
    if p="$(command -v "python$v" 2>/dev/null)"; then printf '%s\n' "$p"; exit 0; fi
done
command -v python3
