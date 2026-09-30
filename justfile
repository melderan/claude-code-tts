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

# Interpreter for `just test`; override before the recipe name: `just PY=3.14 test`
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

# Teach .private-words every non-public repository name in the orgs listed in .private-orgs
private-sync:
    scripts/private-words-sync.py

# Which tests would notice a one-line change to the source: per-test mutation audit, lists to read
# in .logs/test-audit/<time>/report.txt. `just test-audit -k filter` for a subset, --workers N
test-audit *ARGS:
    uv run --with pytest --with pytest-cov scripts/test-audit.py {{ARGS}}

# ---- Host-side recipes: run on the machine that owns the daemon (up, timeline, release, the
# voices-* trio, playpen). They use only what that machine has: python3 and this checkout on
# PYTHONPATH. Never `uv run` or `.venv` here: a uv config that forbids source builds cannot
# build the project, and `uv run` recreates .venv inside a checkout a sandbox may share.
# tests/test_justfile_host_recipes.py enforces this list.

# Voice signatures on the machine that owns the engines: baseline from the daemon's own speech
# history, measure run-to-run spread, re-synthesize and compare (exit 1 on drift). Baseline
# stays in ~/.claude-tts/signatures/, never in the repo, because it holds spoken text.
# Plain python3 with src on the path: the package is stdlib-only, and `uv run` on a machine
# whose uv config forbids source builds cannot build the project (and it recreates .venv in a
# checkout another machine may share).
voices-capture *ARGS:
    PYTHONPATH=src python3 scripts/voice-signatures.py capture {{ARGS}}

voices-spread *ARGS:
    PYTHONPATH=src python3 scripts/voice-signatures.py spread {{ARGS}}

voices-verify *ARGS:
    PYTHONPATH=src python3 scripts/voice-signatures.py verify {{ARGS}}

# Run any command against a throwaway HOME with src on the path. ~/.claude-tts is live daemon
# state (in a sandbox, the host's): every ad-hoc script or repro that imports the package goes
# through here. tests/conftest.py does the same for the suite; config.py refuses a pytest import
# with any other HOME.
playpen +CMD:
    HOME="$(mktemp -d "${TMPDIR:-/tmp}/claude-tts-test-home-XXXXXX")" PYTHONPATH=src {{CMD}}

# Point git at .githooks: pre-commit runs the fast gate, pre-push the full one
hooks:
    git config core.hooksPath .githooks
    @echo "hooks installed: pre-commit -> gate, pre-push -> gate --full"

# Maintainer: signed tag on HEAD's version, push, wait for GitHub to publish. `just release --check` to rehearse.
release *ARGS:
    PYTHONPATH=src python3 -m claude_code_tts.cli release {{ARGS}}

# Operator, on the daemon's machine: rebuild from this checkout, deploy hooks, restart, verify; logs in .logs/just/
up *ARGS:
    @scripts/up.py {{ARGS}}

# Operator: the timeline of `just up` runs, newest last
timeline N="20":
    @tail -n {{N}} .logs/just/timeline.log 2>/dev/null || echo "no runs yet (.logs/just/timeline.log)"

# Same as `just up`; kept for muscle memory
install: up
