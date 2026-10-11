"""L0 static boundary checker for the `plugins/` package (08 §8, AUD-013).

Scope: `plugins/` ONLY. The repository's pre-existing modules already import the
host API and are deliberately out of scope. The check is static (AST + text) so
it can run offline and cannot be satisfied vacuously by an import that never
happens; the caller supplies an explicit non-empty path list.

Rules checked:
1. `reapy` / `reascript_api` may be imported ONLY by `host/reaper.py`.
2. No provider identifier (`the first provider's names`) may appear anywhere
   under `plugins/` except inside `providers/` - comments included.
"""

from __future__ import annotations

import ast
from pathlib import Path

#: Host modules banned outside the single allowed importer.
HOST_IMPORTERS = ("reapy", "reascript_api")

#: The only module inside `plugins/` allowed to import the host API (08 §8.2).
ALLOWED_HOST_IMPORTERS = frozenset({"host/reaper.py"})

#: Provider identifiers banned from core modules and the host bridge,
#: comments included (08 §8.1/.5). Lower-cased comparison.
BANNED_PROVIDER_IDENTIFIERS = ("vital", "osc_", "filter_1")


def plugin_python_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*.py"))


def _imports_host_module(tree: ast.AST, module_names: tuple[str, ...]) -> list[str]:
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                head = alias.name.split(".")[0]
                if head in module_names:
                    found.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                head = node.module.split(".")[0]
                if head in module_names:
                    found.append(node.module)
    return found


def check_boundaries(
    root: Path,
    *,
    allowed_host_importers: frozenset[str] = ALLOWED_HOST_IMPORTERS,
    banned_identifiers: tuple[str, ...] = BANNED_PROVIDER_IDENTIFIERS,
) -> tuple[list[Path], list[str]]:
    """Return (files examined, violations). An empty file list is a failure of
    the caller's non-vacuity assertion, not a pass."""
    files = plugin_python_files(root)
    violations: list[str] = []
    for path in files:
        rel = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text, filename=str(path))
        host_imports = _imports_host_module(tree, HOST_IMPORTERS)
        if host_imports and rel not in allowed_host_importers:
            violations.append(f"{rel}: imports host module(s) {host_imports} outside {sorted(allowed_host_importers)}")
        is_provider_package = rel.startswith("providers/")
        if not is_provider_package:
            lowered = text.lower()
            for identifier in banned_identifiers:
                if identifier.lower() in lowered:
                    violations.append(f"{rel}: contains provider identifier {identifier!r}")
    return files, violations
