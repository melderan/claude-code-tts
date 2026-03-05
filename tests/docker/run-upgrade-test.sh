#!/bin/bash
# Run the end-to-end upgrade test in a container.
#
# Usage:
#   ./tests/docker/run-upgrade-test.sh             # Full build + test
#   ./tests/docker/run-upgrade-test.sh --no-cache  # Force rebuild base
#
# Requires: nerdctl (Rancher Desktop) or docker
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_DIR"

EXTRA_ARGS="${1:-}"

# Detect container runtime
if command -v nerdctl >/dev/null 2>&1; then
    CTR=nerdctl
elif command -v docker >/dev/null 2>&1; then
    CTR=docker
else
    echo "Error: neither nerdctl nor docker found"
    exit 1
fi

echo ""
echo "========================================"
echo "  Docker E2E Upgrade Test ($CTR)"
echo "========================================"
echo ""

# Step 1: Build base image (cached across runs)
echo "--- Building base image ---"
$CTR build \
    -t claude-tts-test-base \
    -f tests/docker/Dockerfile.base \
    ${EXTRA_ARGS} \
    .
echo ""

# Step 2: Build upgrade test image (always rebuilds test layers)
echo "--- Building upgrade test image ---"
$CTR build \
    -t claude-tts-upgrade-test \
    -f tests/docker/Dockerfile.upgrade-test \
    .
echo ""

# Step 3: Run the test
echo "--- Running upgrade test ---"
echo ""
$CTR run --rm claude-tts-upgrade-test
