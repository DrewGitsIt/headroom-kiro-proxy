"""Test that install.sh and update.sh download all modules required by connect_proxy.py.

This test walks the transitive local imports starting from connect_proxy.py
and asserts that every required module appears in both installer manifests.
It would have caught the broken-install bug introduced when proxy logic was
split into proxy.py, interceptor.py, stats.py, and reporter.py.
"""

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SRC_DIR = REPO_ROOT / "src"
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"
UPDATE_SH = REPO_ROOT / "scripts" / "update.sh"


def _local_imports(filepath: Path) -> set[str]:
    """Extract local (in-repo) imports from a Python file."""
    tree = ast.parse(filepath.read_text())
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            candidate = SRC_DIR / f"{node.module}.py"
            if candidate.exists():
                imports.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                candidate = SRC_DIR / f"{alias.name}.py"
                if candidate.exists():
                    imports.add(alias.name)
    return imports


def _transitive_imports(entrypoint: str) -> set[str]:
    """Walk transitive local imports from the given module name."""
    seen: set[str] = set()
    queue = [entrypoint]
    while queue:
        mod = queue.pop()
        if mod in seen:
            continue
        seen.add(mod)
        path = SRC_DIR / f"{mod}.py"
        if path.exists():
            for dep in _local_imports(path):
                queue.append(dep)
    return seen


def _extract_manifest(script_path: Path) -> set[str]:
    """Extract src/*.py module names from a shell script's download commands."""
    text = script_path.read_text()
    # Match: download ".../.../src/MODULE.py" "..."
    pattern = r'download\s+"[^"]*?/src/([a-zA-Z_][a-zA-Z0-9_]*)\.py"'
    return set(re.findall(pattern, text))


def test_install_manifest_covers_all_imports():
    required = _transitive_imports("connect_proxy")
    manifest = _extract_manifest(INSTALL_SH)
    missing = required - manifest
    assert not missing, (
        f"install.sh is missing downloads for: {sorted(missing)}. "
        f"Required (transitive from connect_proxy.py): {sorted(required)}. "
        f"Manifest has: {sorted(manifest)}."
    )


def test_update_manifest_covers_all_imports():
    required = _transitive_imports("connect_proxy")
    manifest = _extract_manifest(UPDATE_SH)
    missing = required - manifest
    assert not missing, (
        f"update.sh is missing downloads for: {sorted(missing)}. "
        f"Required (transitive from connect_proxy.py): {sorted(required)}. "
        f"Manifest has: {sorted(manifest)}."
    )


def test_install_includes_wrapper_script():
    """kiro-wrapper.sh must be downloaded since install.sh aliases kiro-cli to it."""
    text = INSTALL_SH.read_text()
    assert "kiro-wrapper.sh" in text and "download" in text.split("kiro-wrapper.sh")[0].split("\n")[-1], (
        "install.sh references kiro-wrapper.sh in the alias but never downloads it"
    )
