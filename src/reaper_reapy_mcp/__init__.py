"""reaper_reapy_mcp: REAPER control through reapy.

The package root is deliberately LAZY (PEP 562). Importing a submodule must not
drag the host client in: measured before this change,
`import reaper_reapy_mcp.manifest.manifest` left `reapy` in `sys.modules`,
because this file imported `.server`, which imports 8 controllers, each with
`import reapy` at module top level. `manifest/manifest.py` promises "pure
Python, no reapy" in its own docstring, and F1 requires that
`import reaper_reapy_mcp.rpp_builder` does not import `reapy` - neither is
possible while the root eagerly imports the server.

Nothing in the public API changes: `reaper_reapy_mcp.main`, the names in
`__all__`, `from reaper_reapy_mcp import ReaperController` and the console
script entry point `reaper_reapy_mcp = "reaper_reapy_mcp:main"` all still work,
because they go through `__getattr__`, which imports `.server` on first use and
caches the result.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only, never executed at runtime
    from .server import (
        ReaperController,
        create_server,
        get_first_selected_audio_item_core,
        get_selected_items_core,
        insert_midi_files_core,
        main,
    )

__all__ = [
    "ReaperController",
    "create_server",
    "get_first_selected_audio_item_core",
    "get_selected_items_core",
    "insert_midi_files_core",
    "main",
]


def __getattr__(name: str) -> Any:
    """Import `.server` only when one of its public names is actually requested."""
    if name in __all__:
        from . import server

        value = getattr(server, name)
        globals()[name] = value  # cache: the next access is a plain lookup
        return value
    raise AttributeError("module %r has no attribute %r" % (__name__, name))


def __dir__() -> list[str]:
    """Keep introspection useful: whatever is already imported plus the API."""
    return sorted(set(globals()) | set(__all__))
