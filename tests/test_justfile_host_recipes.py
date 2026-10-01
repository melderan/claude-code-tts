"""Recipes the operator runs on the daemon's machine use only what that machine has.

On 2026-09-30 `just voices-capture` ran the project through `uv run` on a Mac whose uv config
forbids source builds: the recipe failed, and uv replaced the `.venv` inside the checkout the
sandbox shares. Recipes in HOST_RECIPES must not reach for uv or .venv; the rest are developer
recipes and may.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

JUSTFILE = Path(__file__).parent.parent / "justfile"
HOST_RECIPES = {"up", "timeline", "release", "voices-capture", "voices-spread", "voices-verify", "playpen"}
FORBIDDEN = ("uv run", "uv tool run", "uvx", ".venv")


def recipes() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    current = None
    for line in JUSTFILE.read_text().splitlines():
        m = re.match(r"^([a-z][a-z0-9-]*)\b[^:]*:\s*(\S.*)?$", line)
        if m and not line.startswith((" ", "\t")) and ":=" not in line:
            current = m.group(1)
            out[current] = []
        elif current and line.startswith(("    ", "\t")):
            out[current].append(line.strip())
    return out


def test_every_host_recipe_exists():
    missing = HOST_RECIPES - set(recipes())
    assert not missing, f"HOST_RECIPES names recipes the justfile lacks: {missing}"


def test_host_recipes_use_only_what_the_host_has():
    bad = []
    for name, body in recipes().items():
        if name not in HOST_RECIPES:
            continue
        for line in body:
            for word in FORBIDDEN:
                if word in line:
                    bad.append(f"{name}: {line}")
    assert bad == [], "host-side recipes must not use uv or .venv:\n" + "\n".join(bad)


def test_host_recipes_that_import_the_package_use_host_py_with_src_on_the_path():
    """macOS ships python3 3.9; the package needs 3.10, so HOST_PY picks the newest 3.10+."""
    for name, body in recipes().items():
        if name in HOST_RECIPES and any("claude_code_tts" in ln or "scripts/voice-signatures" in ln for ln in body):
            assert any("PYTHONPATH=src" in ln for ln in body), name
            assert any("{{HOST_PY}}" in ln for ln in body), f"{name} must run under HOST_PY, not python3"
            assert not any(re.search(r"\bpython3\b", ln) for ln in body), f"{name} uses bare python3"


def test_host_py_comes_from_the_resolver_script():
    text = JUSTFILE.read_text()
    m = re.search(r"^HOST_PY := `(.+)`$", text, re.M)
    assert m, "HOST_PY definition missing"
    assert m.group(1) == "scripts/host-python.sh"
    assert os.access(JUSTFILE.parent / "scripts" / "host-python.sh", os.X_OK)


def test_forwarding_recipes_quote_their_arguments():
    """`just release --notes "a b"` must reach the CLI as one argument.

    {{ARGS}} is pasted into the shell line and re-split by bash; "$@" (with
    `set positional-arguments`) forwards each argument intact.
    """
    text = JUSTFILE.read_text()
    assert re.search(r"^set positional-arguments\s*$", text, re.M), "justfile needs `set positional-arguments`"
    body = recipes()
    for name in ("release", "up"):
        assert any('"$@"' in ln for ln in body[name]), f"{name} must forward arguments as \"$@\""
        assert not any("{{ARGS}}" in ln for ln in body[name]), f"{name} still pastes {{{{ARGS}}}} into the shell line"
