#!/usr/bin/env bash
# check-version.sh - The version lives in src/claude_code_tts/__init__.py (hatch reads it at build
# time, install.py imports it, CLAUDE.md @-imports the file). The only other copy is bump-my-version's
# own record in pyproject.toml; this script checks the two agree.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"

INIT_PY_VERSION=$(grep -E '^__version__ = ' "$REPO_DIR/src/claude_code_tts/__init__.py" | sed 's/__version__ = "\(.*\)"/\1/')
BUMP_VERSION=$(grep -E '^current_version = ' "$REPO_DIR/pyproject.toml" | head -1 | sed 's/current_version = "\(.*\)"/\1/')

echo "Version check:"
echo "  __init__.py:              $INIT_PY_VERSION"
echo "  pyproject current_version: $BUMP_VERSION"
echo ""

if [[ -z "$INIT_PY_VERSION" || "$INIT_PY_VERSION" != "$BUMP_VERSION" ]]; then
    echo "ERROR: __init__.py ($INIT_PY_VERSION) != bump-my-version record ($BUMP_VERSION)"
    echo ""
    echo "Tip: bump-my-version bump patch|minor|major --no-commit --no-tag --allow-dirty"
    exit 1
fi

echo "All versions match: $INIT_PY_VERSION"
