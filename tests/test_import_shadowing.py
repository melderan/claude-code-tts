"""A function-local import must not rebind a name the module already imports at the top.

Python makes a name local to the whole function the moment any statement in it binds that name.
So a second `import re` inside one branch turns every other `re.` in the function into a reference
to a local that may never have been assigned. 9.25.1 shipped exactly that in _speak_from_hook: with
CLAUDE_TTS_SESSION set the branch was skipped and the PAI summary path raised UnboundLocalError.
Lazy imports of names the module does not import at the top are fine and common here.
"""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "claude_code_tts"
MODULES = sorted(SRC.glob("*.py"))


def _bound_names(node: ast.Import | ast.ImportFrom) -> set[str]:
    names = set()
    for alias in node.names:
        if alias.name == "*":
            continue
        names.add(alias.asname or alias.name.split(".")[0])
    return names


def shadowed_imports(path: Path) -> list[str]:
    """Return 'file:line name' for each function-local import that rebinds a module-level import."""
    tree = ast.parse(path.read_text(), filename=str(path))
    top_level: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            top_level |= _bound_names(node)

    findings = []
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(func):
            if isinstance(node, (ast.Import, ast.ImportFrom)) and node.lineno != func.lineno:
                for name in sorted(_bound_names(node) & top_level):
                    findings.append(f"{path.name}:{node.lineno} {name} (in {func.name})")
    return findings


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_no_function_local_import_shadows_a_module_import(path):
    assert shadowed_imports(path) == []
