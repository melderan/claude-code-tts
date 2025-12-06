#!/usr/bin/env bash
# check-version.sh - Ensure version consistency across all files
# Run this before committing version bumps
# Note: With bump-my-version installed, use `bump-my-version show current_version` instead

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"

# Extract versions from each file
PYPROJECT_VERSION=$(grep -E '^version = ' "$REPO_DIR/pyproject.toml" | head -1 | sed 's/version = "\(.*\)"/\1/')
INSTALL_PY_VERSION=$(grep -E '^__version__ = ' "$REPO_DIR/src/claude_code_tts/install.py" | sed 's/__version__ = "\(.*\)"/\1/')
INIT_PY_VERSION=$(grep -E '^__version__ = ' "$REPO_DIR/src/claude_code_tts/__init__.py" | sed 's/__version__ = "\(.*\)"/\1/')
CLAUDE_MD_VERSION=$(grep -E '^Welcome.*Version [0-9]' "$REPO_DIR/CLAUDE.md" | sed 's/.*Version \([0-9]*\.[0-9]*\.[0-9]*\).*/\1/')

echo "Version check:"
echo "  pyproject.toml: $PYPROJECT_VERSION"
echo "  install.py:     $INSTALL_PY_VERSION"
echo "  __init__.py:    $INIT_PY_VERSION"
echo "  CLAUDE.md:      $CLAUDE_MD_VERSION"
echo ""

# Check they all match
ERRORS=0

if [[ "$PYPROJECT_VERSION" != "$INSTALL_PY_VERSION" ]]; then
    echo "ERROR: pyproject.toml ($PYPROJECT_VERSION) != install.py ($INSTALL_PY_VERSION)"
    ERRORS=1
fi

if [[ "$PYPROJECT_VERSION" != "$INIT_PY_VERSION" ]]; then
    echo "ERROR: pyproject.toml ($PYPROJECT_VERSION) != __init__.py ($INIT_PY_VERSION)"
    ERRORS=1
fi

if [[ "$PYPROJECT_VERSION" != "$CLAUDE_MD_VERSION" ]]; then
    echo "ERROR: pyproject.toml ($PYPROJECT_VERSION) != CLAUDE.md ($CLAUDE_MD_VERSION)"
    ERRORS=1
fi

if [[ "$ERRORS" -eq 1 ]]; then
    echo ""
    echo "Tip: Use 'bump-my-version patch|minor|major' to bump all files at once"
    exit 1
fi

echo "All versions match: $PYPROJECT_VERSION"
