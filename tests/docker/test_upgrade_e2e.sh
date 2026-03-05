#!/bin/bash
# End-to-end upgrade test: v6.x -> v7.x
#
# Runs inside Docker container with a simulated v6.x install state.
# Exercises the actual upgrade path a real user would follow.
#
set -euo pipefail

PASS=0
FAIL=0
TESTS=()

pass() {
    echo "  PASS: $1"
    PASS=$((PASS + 1))
    TESTS+=("PASS: $1")
}

fail() {
    echo "  FAIL: $1"
    echo "        $2"
    FAIL=$((FAIL + 1))
    TESTS+=("FAIL: $1 -- $2")
}

check_exists() {
    if [ -e "$2" ]; then
        pass "$1"
    else
        fail "$1" "File not found: $2"
    fi
}

check_not_exists() {
    if [ ! -e "$2" ]; then
        pass "$1"
    else
        fail "$1" "File should not exist: $2"
    fi
}

check_contains() {
    if grep -q "$3" "$2" 2>/dev/null; then
        pass "$1"
    else
        fail "$1" "'$3' not found in $2"
    fi
}

check_not_contains() {
    if ! grep -q "$3" "$2" 2>/dev/null; then
        pass "$1"
    else
        fail "$1" "'$3' unexpectedly found in $2"
    fi
}

echo ""
echo "========================================"
echo "  v6.x -> v7.x Upgrade E2E Test"
echo "========================================"
echo ""

# -----------------------------------------------------------------------
# Phase 1: Verify v6.x state
# -----------------------------------------------------------------------

echo "--- Phase 1: Verify v6.x state ---"
echo ""

check_exists "v6: config.json exists" "$HOME/.claude-tts/config.json"
check_exists "v6: tts-lib.sh exists" "$HOME/.claude-tts/tts-lib.sh"
check_exists "v6: tts-mute.sh exists" "$HOME/.claude-tts/tts-mute.sh"
check_exists "v6: tts-daemon.py exists" "$HOME/.claude-tts/tts-daemon.py"
check_exists "v6: tts-builder.py exists" "$HOME/.claude-tts/tts-builder.py"
check_exists "v6: session project-alpha" "$HOME/.claude-tts/sessions.d/project-alpha.json"
check_exists "v6: session project-beta" "$HOME/.claude-tts/sessions.d/project-beta.json"
check_exists "v6: old hook" "$HOME/.claude/hooks/speak-response.sh"
check_contains "v6: hook references tts-lib.sh" "$HOME/.claude/hooks/speak-response.sh" "tts-lib.sh"

# Count legacy scripts
LEGACY_COUNT=$(find "$HOME/.claude-tts" -maxdepth 1 \( -name "tts-*.sh" -o -name "tts-*.py" \) | grep -v builder | wc -l || echo 0)
if [ "$LEGACY_COUNT" -eq 18 ]; then
    pass "v6: all 18 legacy scripts present"
else
    fail "v6: expected 18 legacy scripts" "found $LEGACY_COUNT"
fi

echo ""

# -----------------------------------------------------------------------
# Phase 2: Run the upgrade
# -----------------------------------------------------------------------

echo "--- Phase 2: Run upgrade ---"
echo ""

cd "$HOME/claude-code-tts"

# Step 1: Install the CLI binary (what uv tool install does)
echo "  Installing claude-tts CLI..."
uv tool install . --force 2>&1 | tail -3
echo ""

# Verify CLI binary exists
if command -v claude-tts >/dev/null 2>&1; then
    pass "claude-tts binary on PATH"
else
    fail "claude-tts binary not on PATH" "$(which claude-tts 2>&1 || echo 'not found')"
fi

if command -v claude-tts-install >/dev/null 2>&1; then
    pass "claude-tts-install binary on PATH"
else
    fail "claude-tts-install binary not on PATH" "$(which claude-tts-install 2>&1 || echo 'not found')"
fi

# Step 2: Run the installer in upgrade mode
echo ""
echo "  Running installer --upgrade..."
claude-tts-install --upgrade 2>&1 | tail -20
echo ""

# -----------------------------------------------------------------------
# Phase 3: Verify v7.x state
# -----------------------------------------------------------------------

echo "--- Phase 3: Verify v7.x state ---"
echo ""

# --- CLI binary works ---
echo "  Testing CLI subcommands..."

if claude-tts --help >/dev/null 2>&1; then
    pass "claude-tts --help works"
else
    fail "claude-tts --help" "exit code $?"
fi

# Test subcommands parse correctly (--help exits 0)
for subcmd in status mute unmute speed persona mode cleanup sounds intermediate discover speak audition daemon release test pause random; do
    if claude-tts $subcmd --help >/dev/null 2>&1; then
        pass "claude-tts $subcmd --help"
    else
        fail "claude-tts $subcmd --help" "exit code $?"
    fi
done

# --- Legacy scripts removed ---
echo ""
echo "  Checking legacy script cleanup..."

for script in \
    tts-daemon.py tts-mode.sh tts-mute.sh tts-unmute.sh \
    tts-status.sh tts-speed.sh tts-persona.sh tts-cleanup.sh \
    tts-random.sh tts-test.sh tts-speak.sh tts-audition.sh \
    tts-discover.sh tts-pause.sh tts-lib.sh tts-filter.py \
    tts-sounds.sh tts-intermediate.sh; do
    check_not_exists "legacy removed: $script" "$HOME/.claude-tts/$script"
done

# --- Survivor files preserved ---
echo ""
echo "  Checking preserved files..."

check_exists "preserved: tts-builder.py" "$HOME/.claude-tts/tts-builder.py"
check_exists "preserved: tts-builder.sh" "$HOME/.claude-tts/tts-builder.sh"
check_exists "preserved: config.json" "$HOME/.claude-tts/config.json"
check_exists "preserved: sessions.d/" "$HOME/.claude-tts/sessions.d"
check_exists "preserved: project-alpha session" "$HOME/.claude-tts/sessions.d/project-alpha.json"
check_exists "preserved: project-beta session" "$HOME/.claude-tts/sessions.d/project-beta.json"

# --- Config content preserved ---
echo ""
echo "  Checking config content..."

CONFIG_PERSONA=$(jq -r '.active_persona' "$HOME/.claude-tts/config.json")
if [ "$CONFIG_PERSONA" = "claude-prime" ]; then
    pass "config: active_persona preserved"
else
    fail "config: active_persona" "expected 'claude-prime', got '$CONFIG_PERSONA'"
fi

CUSTOM_VOICE=$(jq -r '.personas["custom-voice"].voice' "$HOME/.claude-tts/config.json")
if [ "$CUSTOM_VOICE" = "en_US-joe-medium" ]; then
    pass "config: custom persona preserved"
else
    fail "config: custom persona" "expected 'en_US-joe-medium', got '$CUSTOM_VOICE'"
fi

SESSION_PERSONA=$(jq -r '.persona' "$HOME/.claude-tts/sessions.d/project-alpha.json")
if [ "$SESSION_PERSONA" = "custom-voice" ]; then
    pass "session: project-alpha persona preserved"
else
    fail "session: project-alpha persona" "expected 'custom-voice', got '$SESSION_PERSONA'"
fi

SESSION_MUTED=$(jq -r '.muted' "$HOME/.claude-tts/sessions.d/project-beta.json")
if [ "$SESSION_MUTED" = "true" ]; then
    pass "session: project-beta muted preserved"
else
    fail "session: project-beta muted" "expected 'true', got '$SESSION_MUTED'"
fi

# --- Hooks are thin shims ---
echo ""
echo "  Checking hooks..."

check_exists "hook: speak-response.sh deployed" "$HOME/.claude/hooks/speak-response.sh"
check_exists "hook: speak-intermediate.sh deployed" "$HOME/.claude/hooks/speak-intermediate.sh"
check_contains "hook: speak-response calls claude-tts" "$HOME/.claude/hooks/speak-response.sh" "exec claude-tts speak --from-hook"
check_contains "hook: speak-intermediate calls claude-tts" "$HOME/.claude/hooks/speak-intermediate.sh" "exec claude-tts speak --from-hook"
check_not_contains "hook: no tts-lib.sh reference" "$HOME/.claude/hooks/speak-response.sh" "tts-lib.sh"
check_not_contains "hook: no jq reference" "$HOME/.claude/hooks/speak-response.sh" "jq"

# Hook should be 2-3 lines max
HOOK_LINES=$(wc -l < "$HOME/.claude/hooks/speak-response.sh")
if [ "$HOOK_LINES" -le 3 ]; then
    pass "hook: speak-response.sh is thin ($HOOK_LINES lines)"
else
    fail "hook: speak-response.sh too long" "$HOOK_LINES lines (expected <= 3)"
fi

# --- Commands call claude-tts ---
echo ""
echo "  Checking commands..."

for cmd in mute unmute speed sounds mode persona status cleanup random test discover intermediate; do
    CMD_FILE="$HOME/.claude/commands/tts-${cmd}.md"
    check_exists "command: tts-${cmd}.md deployed" "$CMD_FILE"
    check_contains "command: tts-${cmd} calls claude-tts" "$CMD_FILE" "claude-tts"
    check_not_contains "command: tts-${cmd} no old path" "$CMD_FILE" '.claude-tts/'
done

# --- Version recorded ---
echo ""
echo "  Checking version..."

INSTALLED_VERSION=$(jq -r '.installed_version' "$HOME/.claude-tts/config.json")
if [[ "$INSTALLED_VERSION" == 7.* ]]; then
    pass "version: installed_version is 7.x ($INSTALLED_VERSION)"
else
    fail "version: installed_version" "expected 7.x, got '$INSTALLED_VERSION'"
fi

# -----------------------------------------------------------------------
# Phase 4: Verify idempotent re-upgrade
# -----------------------------------------------------------------------

echo ""
echo "--- Phase 4: Idempotent re-upgrade ---"
echo ""

# Snapshot key files
HOOK_HASH_BEFORE=$(md5sum "$HOME/.claude/hooks/speak-response.sh" | awk '{print $1}')
CMD_HASH_BEFORE=$(md5sum "$HOME/.claude/commands/tts-mute.md" | awk '{print $1}')

# Run upgrade again
claude-tts-install --upgrade 2>&1 | tail -5
echo ""

HOOK_HASH_AFTER=$(md5sum "$HOME/.claude/hooks/speak-response.sh" | awk '{print $1}')
CMD_HASH_AFTER=$(md5sum "$HOME/.claude/commands/tts-mute.md" | awk '{print $1}')

if [ "$HOOK_HASH_BEFORE" = "$HOOK_HASH_AFTER" ]; then
    pass "idempotent: hook unchanged after re-upgrade"
else
    fail "idempotent: hook changed" "hash before=$HOOK_HASH_BEFORE after=$HOOK_HASH_AFTER"
fi

if [ "$CMD_HASH_BEFORE" = "$CMD_HASH_AFTER" ]; then
    pass "idempotent: command unchanged after re-upgrade"
else
    fail "idempotent: command changed" "hash before=$CMD_HASH_BEFORE after=$CMD_HASH_AFTER"
fi

# Still no legacy scripts after re-upgrade
REMAINING=$(find "$HOME/.claude-tts" -maxdepth 1 \( -name "tts-*.sh" -o -name "tts-*.py" \) 2>/dev/null | grep -cv builder 2>/dev/null || echo 0)
REMAINING=$(echo "$REMAINING" | tr -d '[:space:]')
if [ "$REMAINING" -eq 0 ]; then
    pass "idempotent: no legacy scripts after re-upgrade"
else
    fail "idempotent: legacy scripts reappeared" "found $REMAINING"
fi

# -----------------------------------------------------------------------
# Results
# -----------------------------------------------------------------------

echo ""
echo "========================================"
echo "  Results: $PASS passed, $FAIL failed"
echo "========================================"
echo ""

if [ $FAIL -gt 0 ]; then
    echo "FAILED TESTS:"
    for t in "${TESTS[@]}"; do
        if [[ "$t" == FAIL* ]]; then
            echo "  $t"
        fi
    done
    echo ""
    exit 1
else
    echo "All tests passed!"
    exit 0
fi
