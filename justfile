# claude-code-tts developer tasks. `just` runs the same recipes CI runs, so local and CI agree.
# Install just: brew install just  (or: uv tool install rust-just)

set shell := ["bash", "-euo", "pipefail", "-c"]

default:
    @just --list

# Lint (ruff) and type check (mypy)
check: lint typecheck

lint:
    uv tool run ruff check src tests

fmt:
    uv tool run ruff check --fix src tests
    uv tool run ruff format src tests

typecheck:
    uv tool run mypy src

# Unit tests; PY selects an interpreter, e.g. `just test PY=3.14`
test PY="3.12":
    uv run --python {{PY}} --with pytest pytest -q --ignore=tests/docker

# Coverage report
cov:
    uv run --with pytest --with pytest-cov pytest -q --ignore=tests/docker --cov=claude_code_tts --cov-report=term-missing

# Version files agree
version:
    scripts/check-version.sh

# Build the wheel and prove a cold install of it finds its own hooks and commands
build:
    #!/usr/bin/env bash
    set -euo pipefail
    rm -rf dist && uv build
    T=$(mktemp -d); export UV_TOOL_DIR=$T/tools UV_TOOL_BIN_DIR=$T/bin
    uv tool install -q dist/*.whl
    $T/bin/claude-tts --version
    python3 - dist/*.whl <<'PY'
    import sys, zipfile
    names = zipfile.ZipFile(sys.argv[1]).namelist()
    hooks = [n for n in names if "/hooks/" in n]; cmds = [n for n in names if "/commands/" in n]
    assert len(hooks) >= 4 and len(cmds) >= 14, (hooks, cmds)
    print(f"wheel ships {len(hooks)} hooks and {len(cmds)} commands")
    PY
    mkdir -p $T/home/.claude && echo '{}' > $T/home/.claude/settings.json
    out=$(HOME=$T/home PATH=$T/bin:$PATH $T/bin/claude-tts-install --dry-run 2>&1 || true)
    if echo "$out" | grep -q "Source .* missing"; then echo "installer cannot find its hooks/commands"; exit 1; fi
    echo "cold install ok"; rm -rf $T

# Clean Ubuntu container, real install, synthesized speech (needs docker). TAG or WHEEL=dist/x.whl
e2e TAG="main":
    tests/docker/run-clean-install-test.sh {{TAG}}

# Everything CI runs, in order
ci: lint typecheck version test build

# Operator, on the daemon's machine: rebuild from this checkout, deploy hooks, restart, verify, log one line to ~/.claude-tts/up.log
up:
    #!/usr/bin/env bash
    set -euo pipefail
    LOG="$HOME/.claude-tts/up.log"; mkdir -p "$HOME/.claude-tts"
    REF=$(git describe --always --dirty --tags 2>/dev/null || echo unknown)
    BRANCH=$(git branch --show-current 2>/dev/null || echo detached)
    VER=$(grep -E '^__version__' src/claude_code_tts/__init__.py | cut -d'"' -f2)
    START=$(date +%s)
    stamp() { date -u +%Y-%m-%dT%H:%M:%SZ; }
    fail() { echo "$(stamp) up v$VER $REF $BRANCH FAILED at $1" >> "$LOG"; echo "up failed at $1; see $LOG"; exit 1; }
    echo "up: v$VER $REF ($BRANCH)"
    uv tool install . --force --build -q || fail install
    claude-tts-install --upgrade >/dev/null 2>&1 || fail hooks
    claude-tts daemon restart >/dev/null 2>&1 || claude-tts daemon start >/dev/null 2>&1 || fail restart
    sleep 2
    HB="$HOME/.claude-tts/daemon.heartbeat"
    if [ -f "$HB" ] && [ $(( $(date +%s) - $(stat -f %m "$HB" 2>/dev/null || stat -c %Y "$HB") )) -lt 10 ]; then D=running; else D=DOWN; fi
    PID=$(cat "$HOME/.claude-tts/daemon.pid" 2>/dev/null || echo -)
    CFG="$HOME/.claude-tts/config.json"
    B=$(jq -r 'if .http.enabled then "on" else "off" end' "$CFG" 2>/dev/null || echo "?")
    M=$(jq -r 'if .mic_aware_pause then "on" else "off" end' "$CFG" 2>/dev/null || echo "?")
    LINE="$(stamp) up v$VER $REF $BRANCH daemon=$D pid=$PID bridge=$B mic=$M took=$(( $(date +%s) - START ))s"
    echo "$LINE" >> "$LOG"; echo "$LINE"
    [ "$D" = running ]

# Operator: the timeline of `just up` runs, newest last
timeline N="20":
    @tail -n {{N}} "$HOME/.claude-tts/up.log" 2>/dev/null || echo "no runs yet: $HOME/.claude-tts/up.log"

# Rebuild the local CLI and redeploy hooks/commands
install:
    uv tool install . --force && claude-tts-install --upgrade
