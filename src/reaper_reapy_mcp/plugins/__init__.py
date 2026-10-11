"""Universal Plugin layer (Universal Plugin MCP V3).

Importing this package must be side-effect free: no host connection, no host
module import, no provider configuration read, no filesystem access to
registry/cache data (13 §4). Provider configuration is read on the first
plugin-tool call, never at import.

Topology (frozen, 04 §6): core modules flat, `host/contract.py` (protocol) and
`host/reaper.py` (the ONLY host importer inside `plugins/`), providers under
`providers/` (P7).
"""

from __future__ import annotations

from .tools import setup_plugin_tools

__all__ = ["setup_plugin_tools"]
