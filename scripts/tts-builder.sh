#!/bin/bash
#
# tts-builder.sh - Wrapper for TTS Persona Builder
#
# Launches the Python textual-based persona builder with uv.
#
# Usage:
#   tts-builder.sh                    # Start the persona builder
#   tts-builder.sh --voice NAME       # Start with a specific voice
#   tts-builder.sh --multi            # Browse multi-speaker models only
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Check for uv
if ! command -v uv &>/dev/null; then
    echo "Error: uv is required but not installed."
    echo ""
    echo "Install with:"
    echo "  curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi

# Run the Python TUI
exec uv run "$SCRIPT_DIR/tts-builder.py" "$@"
