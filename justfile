# claude-code-tts developer tasks. `just` runs the same recipes CI runs, so local and CI agree.
# Recipes stay one line and dispatch to scripts/: a script can be run and debugged on its own,
# while a recipe body passes through just's templating first. Python before Bash for anything
# with logic; Bash only sequences commands.
# Install just: brew install just  (or: uv tool install rust-just)

set shell := ["bash", "-euo", "pipefail", "-c"]

default:
    @just --list

# Lint (ruff) and type check (mypy)
check: lint typecheck

lint:
    uv tool run ruff check src tests scripts

fmt:
    uv tool run ruff check --fix src tests scripts
    uv tool run ruff format src tests scripts

# mypy is the long-standing checker; ty (Astral) catches what it misses. Both must pass.
typecheck:
    uv tool run mypy src
    uv tool run ty check src

# Interpreter for `just test`; override on the command line: `just test PY=3.14`
PY := "3.12"

# Unit tests on the PY interpreter
test:
    uv run --python {{PY}} --with pytest pytest -q --ignore=tests/docker

# Coverage report
cov:
    uv run --with pytest --with pytest-cov pytest -q --ignore=tests/docker --cov=claude_code_tts --cov-report=term-missing

# Version files agree
version:
    scripts/check-version.sh

# Build the wheel and prove a cold install of it finds its own hooks and commands
build:
    scripts/build-check.sh

# Clean Ubuntu container, real install, synthesized speech (needs docker). TAG or WHEEL=dist/x.whl
e2e TAG="main":
    tests/docker/run-clean-install-test.sh {{TAG}}

# Everything CI runs, in order
ci: lint typecheck version test build

# The local gate: same checks as ci, real exit codes, stops at the first failure. FULL=1 adds build.
gate FULL="":
    @scripts/gate.py {{ if FULL != "" { "--full" } else { "" } }}

# Point git at .githooks: pre-commit runs the fast gate, pre-push the full one
hooks:
    git config core.hooksPath .githooks
    @echo "hooks installed: pre-commit -> gate, pre-push -> gate --full"

# Operator, on the daemon's machine: rebuild from this checkout, deploy hooks, restart, verify; logs in .logs/just/
up:
    @scripts/up.py

# Operator: the timeline of `just up` runs, newest last
timeline N="20":
    @tail -n {{N}} .logs/just/timeline.log 2>/dev/null || echo "no runs yet (.logs/just/timeline.log)"

# Same as `just up`; kept for muscle memory
install: up
