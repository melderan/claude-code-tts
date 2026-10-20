#!/bin/bash
#
# commit-feature.sh - Commit a change and its version bump as ONE commit
#
# Usage:
#   ./scripts/commit-feature.sh [-y] [-F FILE] <type> <subject>
#
#   ./scripts/commit-feature.sh feat "add pause/resume toggle"
#   ./scripts/commit-feature.sh -y -F /tmp/body.txt fix "handle empty queue gracefully"
#
# Arguments:
#   <type>     feat (minor bump); fix, docs, chore, refactor, perf, test (patch bump);
#              patch is fix
#   <subject>  The subject line without the type prefix
#   -F FILE    The commit body: everything after the subject, trailers included
#   -y         Commit without asking; needed when stdin is not a terminal
#
# Stages every change (git add -A). Tags nothing: `just release` makes the signed tag,
# and a tag made here would make it refuse.
#

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

usage() {
    echo "Usage: $0 [-y] [-F FILE] <type> <subject>"
    echo ""
    echo "Types:"
    echo "  feat                                - bumps minor: 5.4.0 -> 5.5.0"
    echo "  fix docs chore refactor perf test   - bump patch: 5.4.0 -> 5.4.1"
    echo "  patch                               - same as fix"
    echo ""
    echo "  -F FILE  commit body (everything after the subject, trailers included)"
    echo "  -y       do not ask before committing"
}

BODY_FILE=""
YES=0
while getopts "F:yh" opt; do
    case "$opt" in
        F) BODY_FILE="$OPTARG" ;;
        y) YES=1 ;;
        h) usage; exit 0 ;;
        *) usage; exit 2 ;;
    esac
done
shift $((OPTIND - 1))

if [[ $# -ne 2 ]]; then
    usage
    exit 2
fi

TYPE="$1"
SUBJECT="$2"

if [[ -n "$BODY_FILE" ]]; then
    if [[ ! -s "$BODY_FILE" ]]; then
        echo -e "${RED}Body file missing or empty: $BODY_FILE${NC}"
        exit 2
    fi
    BODY_FILE="$(cd "$(dirname "$BODY_FILE")" && pwd)/$(basename "$BODY_FILE")"
fi

cd "$(dirname "$0")/.."

case "$TYPE" in
    feat|feature) BUMP="minor"; PREFIX="feat" ;;
    fix|bugfix|patch) BUMP="patch"; PREFIX="fix" ;;
    docs|chore|refactor|perf|test) BUMP="patch"; PREFIX="$TYPE" ;;
    *)
        echo -e "${RED}Unknown type: $TYPE${NC}"
        usage
        exit 2
        ;;
esac

if [[ -z $(git status --porcelain) ]]; then
    echo -e "${RED}No changes to commit${NC}"
    exit 1
fi

version() {
    sed -n 's/^__version__ = "\(.*\)"$/\1/p' src/claude_code_tts/__init__.py
}

CURRENT_VERSION=$(version)
echo ""
echo -e "${YELLOW}Current version:${NC} $CURRENT_VERSION"
echo -e "${YELLOW}Bump type:${NC} $BUMP"
echo -e "${YELLOW}Subject:${NC} $PREFIX: $SUBJECT"
echo -e "${YELLOW}Body:${NC} ${BODY_FILE:-(none)}"
echo ""
echo -e "${YELLOW}Changes to be committed:${NC}"
git status --short
echo ""

if [[ $YES -eq 0 ]]; then
    if [[ ! -t 0 ]]; then
        echo -e "${RED}stdin is not a terminal; pass -y to commit without asking${NC}"
        exit 2
    fi
    read -p "Proceed? [y/N] " -n 1 -r
    echo ""
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Aborted."
        exit 1
    fi
fi

echo -e "${GREEN}Bumping version...${NC}"
bump-my-version bump "$BUMP" --no-commit --no-tag --allow-dirty
NEW_VERSION=$(version)
echo -e "${GREEN}New version:${NC} $NEW_VERSION"

git add -A

if [[ -n "$BODY_FILE" ]]; then
    # git refuses -m with -F, so the subject and body go in one message file
    MSG_FILE=$(mktemp)
    trap 'rm -f "$MSG_FILE"' EXIT
    { printf '%s: %s\n\n' "$PREFIX" "$SUBJECT"; cat "$BODY_FILE"; } > "$MSG_FILE"
    git commit -F "$MSG_FILE"
else
    git commit -m "$PREFIX: $SUBJECT"
fi

echo ""
echo -e "${GREEN}Committed $PREFIX: $SUBJECT ($CURRENT_VERSION -> $NEW_VERSION)${NC}"
echo "Next: git push, then just release once ci is green"
