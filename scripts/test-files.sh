#!/bin/bash
#
# test-files.sh - Run some tests, keep the whole log, print its last line and the real exit code
#
# Usage: scripts/test-files.sh [pytest args...]     (just t tests/test_x.py -k name)
#
# One call for what was three (run to a log, read the exit code, tail the log): never judge
# pytest through a pipe, whose exit code is the tail's. The log is .logs/just/t-<time>.log.
#

set -uo pipefail

cd "$(dirname "$0")/.."
mkdir -p .logs/just
log=".logs/just/t-$(date -u +%Y%m%dT%H%M%SZ).log"
rc=0
uv run --python "${PY:-3.12}" --with pytest pytest -q "$@" > "$log" 2>&1 || rc=$?
grep -E '^(FAILED|ERROR) ' "$log" || true
echo "$(tail -n 1 "$log") rc=$rc log=$log"
exit "$rc"
