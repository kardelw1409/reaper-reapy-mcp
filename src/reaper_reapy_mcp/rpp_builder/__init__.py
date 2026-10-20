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

from .compile import from_manifest
from .model import (
    AudioSource,
    Item,
    Marker,
    MidiEvent,
    MidiSource,
    Project,
    Region,
    Send,
    StretchMarker,
    Track,
    make_guid,
)
from .reader import from_nodes
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
from .validate import validate
from .writer import FileSink, ValidationError, WriteReport, WriteVerificationError
from .writer.to_nodes import to_nodes

__all__ = [
    "AudioSource",
    "CRLF",
    "FileSink",
    "Item",
    "Marker",
    "MidiEvent",
    "MidiSource",
    "Project",
    "Region",
    "RppDocument",
    "RppField",
    "RppNode",
    "RppParseError",
    "Send",
    "StretchMarker",
    "Track",
    "ValidationError",
    "WriteReport",
    "WriteVerificationError",
    "from_manifest",
    "from_nodes",
    "make_guid",
    "parse",
    "parse_file",
    "render",
    "render_bytes",
    "to_nodes",
    "tokenize",
    "unquote",
    "validate",
]
