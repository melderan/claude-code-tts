#!/usr/bin/env bash
# check-version.sh - Ensure version consistency across all files
# Run this before committing version bumps

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"

# Extract versions from each file
INSTALL_PY_VERSION=$(grep -E '^__version__ = ' "$REPO_DIR/src/claude_code_tts/install.py" | sed 's/__version__ = "\(.*\)"/\1/')
INIT_PY_VERSION=$(grep -E '^__version__ = ' "$REPO_DIR/src/claude_code_tts/__init__.py" | sed 's/__version__ = "\(.*\)"/\1/')
CLAUDE_MD_VERSION=$(grep -E '^Welcome.*Version [0-9]' "$REPO_DIR/CLAUDE.md" | sed 's/.*Version \([0-9]*\.[0-9]*\.[0-9]*\).*/\1/')

echo "Version check:"
echo "  install.py:  $INSTALL_PY_VERSION"
echo "  __init__.py: $INIT_PY_VERSION"
echo "  CLAUDE.md:   $CLAUDE_MD_VERSION"
echo ""

# Check they all match
if [[ "$INSTALL_PY_VERSION" != "$INIT_PY_VERSION" ]]; then
    echo "ERROR: install.py ($INSTALL_PY_VERSION) != __init__.py ($INIT_PY_VERSION)"
    exit 1
fi

if [[ "$INSTALL_PY_VERSION" != "$CLAUDE_MD_VERSION" ]]; then
    echo "ERROR: install.py ($INSTALL_PY_VERSION) != CLAUDE.md ($CLAUDE_MD_VERSION)"
    exit 1
fi

echo "All versions match: $INSTALL_PY_VERSION"
