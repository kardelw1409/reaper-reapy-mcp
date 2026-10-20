"""The raw `.rpp` format layer: block tree, no domain knowledge.

Nothing here knows what a track or an item is (ARCHITECTURE section 4). The
public names are re-exported so callers use one import:

    from reaper_reapy_mcp.rpp_builder.rpp import parse, render
"""

from __future__ import annotations

from .node import CRLF, RppDocument, RppField, RppNode, RppParseError, unquote
from .parse import iter_lines, parse, parse_file, tokenize
from .render import render, render_bytes

__all__ = [
    "CRLF",
    "RppDocument",
    "RppField",
    "RppNode",
    "RppParseError",
    "iter_lines",
    "parse",
    "parse_file",
    "render",
    "render_bytes",
    "tokenize",
    "unquote",
]
