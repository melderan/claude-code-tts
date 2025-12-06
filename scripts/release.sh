#!/usr/bin/env bash
# release.sh - Guided release workflow for claude-code-tts
# Ensures all checks pass before bumping version and pushing
#
# Usage:
#   ./scripts/release.sh              # Interactive release
#   ./scripts/release.sh patch        # Direct patch release
#   ./scripts/release.sh minor        # Direct minor release
#   ./scripts/release.sh major        # Direct major release
#   ./scripts/release.sh --install-hooks  # Install git hooks
#   ./scripts/release.sh --check      # Run checks only

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"

cd "$REPO_DIR"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

# --- Install hooks ---
install_hooks() {
    echo "Installing git hooks..."
    cp scripts/pre-push .git/hooks/pre-push
    chmod +x .git/hooks/pre-push
    echo -e "${GREEN}Pre-push hook installed${NC}"
    exit 0
}

# --- Run all checks ---
run_checks() {
    echo ""
    echo -e "${BLUE}========================================${NC}"
    echo -e "${BLUE}  Pre-release Checks${NC}"
    echo -e "${BLUE}========================================${NC}"
    echo ""

    ERRORS=0
    WARNINGS=0

    # Check 1: Uncommitted changes
    echo -n "1. Checking for uncommitted changes... "
    if [[ -n "$(git status --porcelain)" ]]; then
        echo -e "${YELLOW}UNCOMMITTED CHANGES${NC}"
        git status --short
        echo ""
        WARNINGS=1
    else
        echo -e "${GREEN}OK${NC}"
    fi

    # Check 2: Version consistency
    echo -n "2. Checking version consistency... "
    if ./scripts/check-version.sh > /dev/null 2>&1; then
        CURRENT_VERSION=$(grep -E '^current_version' pyproject.toml | sed 's/current_version = "\(.*\)"/\1/')
        echo -e "${GREEN}OK${NC} (v${CURRENT_VERSION})"
    else
        echo -e "${RED}FAIL${NC}"
        ./scripts/check-version.sh
        ERRORS=1
    fi

    # Check 3: Commands in installer
    echo -n "3. Checking all commands wired in installer... "
    COMMANDS_IN_DIR=$(ls commands/*.md 2>/dev/null | xargs -I{} basename {} .md | sort)
    COMMANDS_IN_INSTALLER=$(grep -oE 'tts-[a-z]+\.md' src/claude_code_tts/install.py | sed 's/\.md//' | sort | uniq)

    MISSING=""
    for cmd in $COMMANDS_IN_DIR; do
        if ! echo "$COMMANDS_IN_INSTALLER" | grep -q "^${cmd}$"; then
            MISSING="$MISSING $cmd"
        fi
    done

    if [[ -z "$MISSING" ]]; then
        echo -e "${GREEN}OK${NC}"
    else
        echo -e "${RED}FAIL${NC}"
        echo "   Missing from installer:$MISSING"
        ERRORS=1
    fi

    # Check 4: README has all commands
    echo -n "4. Checking README documents all commands... "
    MISSING_README=""
    for cmd in $COMMANDS_IN_DIR; do
        if ! grep -q "/$cmd" README.md 2>/dev/null; then
            MISSING_README="$MISSING_README $cmd"
        fi
    done

    if [[ -z "$MISSING_README" ]]; then
        echo -e "${GREEN}OK${NC}"
    else
        echo -e "${YELLOW}WARN${NC}"
        echo "   Not in README:$MISSING_README"
        WARNINGS=1
    fi

    # Check 5: Python syntax
    echo -n "5. Checking Python syntax... "
    if python3 -m py_compile src/claude_code_tts/install.py 2>/dev/null; then
        echo -e "${GREEN}OK${NC}"
    else
        echo -e "${RED}FAIL${NC}"
        ERRORS=1
    fi

    # Check 6: bump-my-version installed
    echo -n "6. Checking bump-my-version installed... "
    if command -v bump-my-version &> /dev/null; then
        echo -e "${GREEN}OK${NC}"
    else
        echo -e "${RED}FAIL${NC} - Install with: uv tool install bump-my-version"
        ERRORS=1
    fi

    # Check 7: Git hooks installed
    echo -n "7. Checking pre-push hook installed... "
    if [[ -x .git/hooks/pre-push ]]; then
        echo -e "${GREEN}OK${NC}"
    else
        echo -e "${YELLOW}WARN${NC} - Run: ./scripts/release.sh --install-hooks"
        WARNINGS=1
    fi

    echo ""

    if [[ "$ERRORS" -eq 1 ]]; then
        echo -e "${RED}Checks failed. Fix errors before releasing.${NC}"
        return 1
    fi

    if [[ "$WARNINGS" -eq 1 ]]; then
        echo -e "${YELLOW}Checks passed with warnings.${NC}"
    else
        echo -e "${GREEN}All checks passed!${NC}"
    fi

    return 0
}

# --- Show bump options ---
show_bump_options() {
    echo ""
    echo -e "${CYAN}Available version bumps:${NC}"
    bump-my-version show-bump
    echo ""
}

# --- Do the release ---
do_release() {
    local BUMP_TYPE="$1"

    echo ""
    echo -e "${BLUE}Bumping version (${BUMP_TYPE})...${NC}"
    bump-my-version bump "$BUMP_TYPE"

    echo ""
    echo -e "${BLUE}Pushing to origin with tags...${NC}"
    git push origin main --tags

    echo ""
    NEW_VERSION=$(grep -E '^current_version' pyproject.toml | sed 's/current_version = "\(.*\)"/\1/')
    echo -e "${GREEN}========================================${NC}"
    echo -e "${GREEN}  Released v${NEW_VERSION}${NC}"
    echo -e "${GREEN}========================================${NC}"
    echo ""

    # Sync local install
    echo "Syncing local installation..."
    python3 src/claude_code_tts/install.py --upgrade 2>&1 | grep -E "Version|Complete" || true
    echo ""
    echo -e "${GREEN}Done!${NC}"
}

# --- Main ---

# Handle flags
case "${1:-}" in
    --install-hooks)
        install_hooks
        ;;
    --check)
        run_checks
        exit $?
        ;;
    --help|-h)
        echo "Usage: ./scripts/release.sh [patch|minor|major|--check|--install-hooks]"
        echo ""
        echo "Options:"
        echo "  (none)          Interactive release"
        echo "  patch           Bump patch version (bug fixes)"
        echo "  minor           Bump minor version (new features)"
        echo "  major           Bump major version (breaking changes)"
        echo "  --check         Run checks only, don't release"
        echo "  --install-hooks Install git pre-push hook"
        exit 0
        ;;
esac

echo ""
echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}  Claude Code TTS Release Manager${NC}"
echo -e "${BLUE}========================================${NC}"

# Run checks
if ! run_checks; then
    exit 1
fi

# Direct release if type specified
if [[ "${1:-}" =~ ^(patch|minor|major)$ ]]; then
    do_release "$1"
    exit 0
fi

# Interactive mode
show_bump_options

echo "Select release type:"
echo "  [1] patch - Bug fixes, no new features"
echo "  [2] minor - New features, backwards compatible"
echo "  [3] major - Breaking changes"
echo "  [4] Cancel"
echo ""
read -p "Choice [1-4]: " choice

case "$choice" in
    1) do_release "patch" ;;
    2) do_release "minor" ;;
    3) do_release "major" ;;
    *) echo "Cancelled."; exit 0 ;;
esac
