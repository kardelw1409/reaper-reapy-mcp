"""Vision for the REAPER MCP server: capture the window, move the view, look.

The layer answers one question an agent cannot answer from the project data
alone: **what does REAPER actually look like right now** - and it answers it with
an image that does not depend on which application is active, from a view whose
time range is known, with a witness that the project was not written.

Modules
    capture.py    Win32 window render (PrintWindow), the minimized guard, the
                  settle rule, image statistics - no reapy, no numpy.
    imaging.py    PNG/contact-sheet encoding and frame comparison (numpy/PIL,
                  imported once at import time to keep the tool path free of a
                  lazy heavy import inside the event loop).
    view.py       view/scroll/zoom through a ReaScript API module, every action
                  name-verified before it is fired, every effect measured.
    artifacts.py  where images are written - the repository's existing run-artifact
                  scheme, not a new one.
    tools.py      the `vision_*` MCP tools.
    cli.py        the same operations on the command line, for a human or a test
                  harness that does not want an MCP round trip.
"""

from __future__ import annotations

from .tools import TOOL_NAMES, setup_vision_tools

__all__ = ["TOOL_NAMES", "setup_vision_tools"]
