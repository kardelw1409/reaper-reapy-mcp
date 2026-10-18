"""P43 - the tool was registered and the served class could not answer it (first live call, 2026-10-18).

MEASURED, live, through the product surface - the first call these tools ever received:

    get_fx_presets(track_index=14, fx_index=0)
        -> {"result": {"status": "error", "message": "Failed to read FX presets: 'ReaperController'
            object has no attribute 'get_fx_presets'"}}

Two classes carry the name `ReaperController`. The legacy wrapper in `src/reaper_controller.py`
(the one `src/__init__.py`, `src/reaper_reapy_mcp.py` and four `tests/test_*.py` files import)
received `PresetController` in P42. The class `reaper_reapy_mcp.server.create_server()`
instantiates - the one the running MCP server's tools call, and the one the `tests/plugins/live/*`
files build - did not. The four preset tool bodies reach `controller.<method>`, so registration
succeeded, the client listed the tools, and the first live call died on the attribute lookup: an
inventory of tool names says nothing about the instance behind them.

The offline suite stayed green because `tests/test_reaper_presets.py` asked the WRAPPER class. This
file pins the two invariants that fail for the defect itself rather than for one of its instances:

* every controller module in `src/controllers/` must be composed by BOTH combined classes;
* every `controller.<name>` reference in the tool layers must resolve on the SERVED class.

No host contact: both checks are import-level plus one AST pass over the tool sources.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"
CONTROLLERS = SRC / "controllers"

#: The layers whose tool bodies reach `controller.<name>` on the instance the server builds.
TOOL_LAYERS = (SRC / "mcp_tools.py",) + tuple(sorted((SRC / "reaper_reapy_mcp" / "manifest").glob("*.py")))


def _combined_classes() -> dict[str, type]:
    """Both classes named `ReaperController`, keyed by the module that defines them."""
    return {
        "reaper_reapy_mcp.server": importlib.import_module("reaper_reapy_mcp.server").ReaperController,
        "reaper_controller": importlib.import_module("reaper_controller").ReaperController,
    }


def test_every_controller_module_is_composed_by_both_combined_classes():
    """P43: the class the server instantiates must carry the whole controller package."""
    missing: dict[str, list[str]] = {}
    for path in sorted(CONTROLLERS.glob("*_controller.py")):
        module = importlib.import_module(f"controllers.{path.stem}")
        for name, value in vars(module).items():
            if not (isinstance(value, type) and value.__module__ == module.__name__):
                continue
            for label, combined in _combined_classes().items():
                if not issubclass(combined, value):
                    missing.setdefault(label, []).append(name)
    assert missing == {}, (
        f"a combined controller does not compose {missing} - tools that reach those methods still "
        "register and then fail on their first live call (measured P43: get_fx_presets answered "
        "\"'ReaperController' object has no attribute 'get_fx_presets'\")"
    )


def test_every_controller_reference_in_the_tool_layers_resolves_on_the_served_class():
    """P43: a registered tool body must be able to reach its method on the served instance."""
    server_class = importlib.import_module("reaper_reapy_mcp.server").ReaperController
    referenced: dict[str, set[str]] = {}
    for path in TOOL_LAYERS:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "controller"
            ):
                referenced.setdefault(node.attr, set()).add(f"{path.name}:{node.lineno}")
    assert referenced, "the audit must not pass vacuously: the tool layers reach `controller.<name>`"
    unresolved = {
        name: sorted(where) for name, where in referenced.items() if not hasattr(server_class, name)
    }
    assert unresolved == {}, (
        f"the served class cannot answer {unresolved} - the tools are registered from these lines "
        "and would fail on their first live call"
    )
