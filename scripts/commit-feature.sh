#!/bin/bash
#
# commit-feature.sh - Commit a feature/fix with proper version bump
#
# This ensures version bump and feature changes are in ONE commit,
# not two separate commits.
#
# Usage:
#   ./scripts/commit-feature.sh feat "add pause/resume toggle"
#   ./scripts/commit-feature.sh fix "handle empty queue gracefully"
#   ./scripts/commit-feature.sh patch "typo in error message"
#
# Arguments:
#   $1 - Type: "feat" (minor bump), "fix" (patch bump), or "patch" (patch bump)
#   $2 - Commit message (without the type prefix)
#

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

cd "$(dirname "$0")/.."

if [[ $# -lt 2 ]]; then
    echo "Usage: $0 <type> <message>"
    echo ""
    echo "Types:"
    echo "  feat   - New feature (bumps minor: 5.4.0 -> 5.5.0)"
    echo "  fix    - Bug fix (bumps patch: 5.4.0 -> 5.4.1)"
    echo "  patch  - Small change (bumps patch: 5.4.0 -> 5.4.1)"
    echo ""
    echo "Examples:"
    echo "  $0 feat \"add pause/resume toggle for TTS\""
    echo "  $0 fix \"handle empty queue gracefully\""
    exit 1
fi

TYPE="$1"
MESSAGE="$2"

# Determine bump type
case "$TYPE" in
    feat|feature)
        BUMP="minor"
        PREFIX="feat"
        ;;
    fix|bugfix)
        BUMP="patch"
        PREFIX="fix"
        ;;
    patch)
        BUMP="patch"
        PREFIX="fix"
        ;;
    *)
        echo -e "${RED}Unknown type: $TYPE${NC}"
        echo "Use: feat, fix, or patch"
        exit 1
        ;;
esac

# Check for uncommitted changes
if [[ -z $(git status --porcelain) ]]; then
    echo -e "${RED}No changes to commit${NC}"
    exit 1
fi

# Show what we're about to do
CURRENT_VERSION=$(grep -m1 'current_version' pyproject.toml | cut -d'"' -f2)
echo ""
echo -e "${YELLOW}Current version:${NC} $CURRENT_VERSION"
echo -e "${YELLOW}Bump type:${NC} $BUMP"
echo -e "${YELLOW}Commit message:${NC} $PREFIX: $MESSAGE"
echo ""

# Show changes
echo -e "${YELLOW}Changes to be committed:${NC}"
git status --short
echo ""

# Confirm
read -p "Proceed? [y/N] " -n 1 -r
echo ""
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Aborted."
    exit 1
fi

# Bump version WITHOUT auto-commit
echo ""
echo -e "${GREEN}Bumping version...${NC}"
bump-my-version bump "$BUMP" --no-commit --no-tag --allow-dirty

# Get new version
NEW_VERSION=$(grep -m1 'current_version' pyproject.toml | cut -d'"' -f2)
echo -e "${GREEN}New version:${NC} $NEW_VERSION"

# Stage all changes (feature + version bump)
echo ""
echo -e "${GREEN}Staging all changes...${NC}"
git add -A

# Commit with proper message
echo ""
echo -e "${GREEN}Committing...${NC}"
git commit -m "$PREFIX: $MESSAGE

Version: $NEW_VERSION

🤖 Generated with [Claude Code](https://claude.com/claude-code)

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>"

# Tag with version
echo ""
echo -e "${GREEN}Tagging v$NEW_VERSION...${NC}"
git tag -a "v$NEW_VERSION" -m "v$NEW_VERSION - $MESSAGE"

echo ""
echo -e "${GREEN}Done!${NC}"
echo ""
echo "To push: git push && git push --tags"
