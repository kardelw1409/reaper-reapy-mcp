"""RPP Builder: assemble REAPER `.rpp` projects as text, without a running REAPER.

Layer map: ARCHITECTURE section 4. Phase F1 provides `rpp/` only - the block
syntax and a lossless reader/writer. `model/`, `writer/`, `reader/`, `compile/`
and `sinks/` arrive with F2+.

Two rules hold this package together, and both are tested:

* it imports NOTHING from `reaper_reapy_mcp` (so it can be moved out with a
  single `mv`), and
* it never imports `reapy`, so a project can be built on a machine where
  REAPER is not running or not installed.
"""

from __future__ import annotations

from .rpp import (
    CRLF,
    RppDocument,
    RppField,
    RppNode,
    RppParseError,
    parse,
    parse_file,
    render,
    render_bytes,
    tokenize,
    unquote,
)

__all__ = [
    "CRLF",
    "RppDocument",
    "RppField",
    "RppNode",
    "RppParseError",
    "parse",
    "parse_file",
    "render",
    "render_bytes",
    "tokenize",
    "unquote",
]
