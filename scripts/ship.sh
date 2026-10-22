#!/bin/bash
#
# ship.sh - Commit, push and release in one call
#
# Usage:
#   ./scripts/ship.sh [-F FILE] <type> <subject>
#
# Runs scripts/commit-feature.sh -y with the same arguments, then git push (the pre-push
# hook runs the full gate), then `claude-tts release --wait-ci` from this checkout. Stops at
# the first step that fails and exits with its code, saying what already landed, so the rest
# can be run by hand. ci-wait is checked before anything is committed: without it release
# --wait-ci would refuse after the push.
#

set -euo pipefail

cd "$(dirname "$0")/.."

if ! command -v ci-wait >/dev/null; then
    echo "ship: ci-wait is not on the PATH; nothing committed"
    echo "ship: install it, or commit-feature.sh, git push, and just release once ci is green"
    exit 2
fi

echo "== ship 1/3: commit"
scripts/commit-feature.sh -y "$@"
version=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' src/claude_code_tts/__init__.py)

echo "== ship 2/3: push $version"
rc=0
git push || rc=$?
if [[ $rc -ne 0 ]]; then
    echo "ship: push exited $rc; $version is committed locally, not pushed"
    exit "$rc"
fi

echo "== ship 3/3: release $version"
host_py=$(scripts/host-python.sh)
rc=0
PYTHONPATH=src "$host_py" -m claude_code_tts.cli release --wait-ci || rc=$?
if [[ $rc -ne 0 ]]; then
    echo "ship: release exited $rc; $version is pushed, not released (just release --wait-ci)"
    exit "$rc"
fi
echo "ship: $version committed, pushed and released"
