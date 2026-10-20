"""F1 rule: building a project must not need REAPER, so `reapy` must not be imported.

Measured before the lazy package root (`reaper_reapy_mcp/__init__.py`):
`import reaper_reapy_mcp.manifest.manifest` left `reapy` in `sys.modules`,
because the root imported `.server`, which imports 8 controllers, each with
`import reapy` at module top level. Python always executes the parent package
first, so no subpackage could escape that.

The runtime check runs in a SUBPROCESS on purpose: this pytest process may
already hold `reapy` from another suite, and `sys.modules` is not a per-test
resource.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"
PACKAGE = SRC / "reaper_reapy_mcp" / "rpp_builder"

_PROBE = r"""
import json
import sys

sys.path.insert(0, %r)

import reaper_reapy_mcp.rpp_builder as builder

root = builder.parse("<X\r\n  NAME a\r\n>\r\n").root
print(json.dumps({
    "reapy_modules": sorted(
        name for name in sys.modules if name == "reapy" or name.startswith("reapy.")
    ),
    "server_loaded": "reaper_reapy_mcp.server" in sys.modules,
    "root_tag": root.tag,
    "roundtrip": builder.render(builder.parse("<X\r\n  NAME a\r\n>\r\n")),
}))
"""

_HOST_MODULES = ("reapy", "reaper_reapy_mcp")


def _imported_modules(tree: ast.AST):
    """Yield `(line, module)` for every real import - docstring examples do not count."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.lineno, node.module


def _is_host_module(module: str) -> bool:
    return any(module == host or module.startswith(host + ".") for host in _HOST_MODULES)


def test_importing_rpp_builder_does_not_import_reapy():
    completed = subprocess.run(
        [sys.executable, "-c", _PROBE % str(SRC)],
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout.strip().splitlines()[-1])

    assert payload["reapy_modules"] == [], (
        "importing reaper_reapy_mcp.rpp_builder pulled the host client in: %r"
        % (payload["reapy_modules"],)
    )
    assert payload["server_loaded"] is False
    assert payload["root_tag"] == "X"
    assert payload["roundtrip"] == "<X\r\n  NAME a\r\n>\r\n"


def test_package_has_no_imports_from_its_parent():
    """ARCHITECTURE section 4: zero imports from `reaper_reapy_mcp`, zero `reapy`.

    Both rules are what make the package movable with a single `mv`, and the
    check is static so a future module cannot break it silently.
    """
    offenders = []
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for line_number, module in _imported_modules(tree):
            if _is_host_module(module):
                offenders.append(
                    "%s:%d imports %s" % (path.relative_to(SRC), line_number, module)
                )
    assert offenders == [], "rpp_builder must stay host-free: %r" % (offenders,)


def test_package_files_are_all_covered():
    """The static scan must actually look at something (guards an empty glob)."""
    files = sorted(PACKAGE.rglob("*.py"))
    assert len(files) >= 4, files
