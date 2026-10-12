"""A minimal newline-JSON-RPC MCP stdio client for this repository's own server.

Why this exists: the editor's chat tool picker disables a subset of the
registered tools (`plugin_instances`/`plugin_find`/`plugin_write`, `add_fx`,
`create_track`, `get_fx_list` were all disabled in P27's session), so the live
work of a round cannot be driven from the chat surface alone. This client starts
**the repository's own MCP server as a child process** and speaks the MCP stdio
protocol to it, i.e. calls go through exactly the server the editor talks to -
a second *client*, never a bypass of the MCP layer.

It is deliberately dependency-free (no `mcp` package): the server's transport is
newline-delimited JSON-RPC, and a harness that shares no code with the thing it
measures is the only kind that can see a transport-level defect.

Measured cost note: every client construction starts a fresh server process
(import + `create_server`, ~1.5 s, plus startup preload). A probe that needs
many calls should keep ONE client for its whole run.
"""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

__all__ = ["McpClient", "ServerStartup"]

REPO = str(Path(__file__).resolve().parents[3])
BOOT = (
    "import sys; sys.path.insert(0, r'" + REPO + r"\src'); "
    "from reaper_reapy_mcp import main; main()"
)


class ServerStartup:
    """The measured startup cost of one server process (per client)."""

    def __init__(self) -> None:
        self.seconds: float = 0.0
        self.server_info: dict[str, Any] = {}


class McpClient:
    """One MCP stdio session against the repository's own server."""

    def __init__(self, *, name: str = "p28-probe", env: dict[str, str] | None = None) -> None:
        self.startup = ServerStartup()
        self._started = time.perf_counter()
        child_env = None
        if env is not None:
            import os

            child_env = {**os.environ, **env}
        self.proc = subprocess.Popen(
            ["py", "-3.11", "-c", BOOT],
            cwd=REPO,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=child_env,
        )
        self.lines: "queue.Queue[str]" = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        self._id = 0
        self._send(
            {
                "jsonrpc": "2.0",
                "id": self._next(),
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": name, "version": "0"},
                },
            }
        )
        init = self._wait(1, 60.0)
        if "result" not in init:
            raise RuntimeError(f"MCP handshake failed: {init}")
        self.startup.server_info = init["result"]["serverInfo"]
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        self.startup.seconds = time.perf_counter() - self._started

    # -- plumbing ---------------------------------------------------------

    def _pump(self) -> None:
        for line in self.proc.stdout:
            self.lines.put(line)

    def _next(self) -> int:
        self._id += 1
        return self._id

    def _send(self, obj: dict) -> None:
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def _wait(self, req_id: int, timeout: float) -> dict:
        end = time.monotonic() + timeout
        while True:
            try:
                line = self.lines.get(timeout=max(0.1, end - time.monotonic()))
            except queue.Empty:
                raise TimeoutError(f"no response for id={req_id} within {timeout}s")
            try:
                msg = json.loads(line.strip())
            except json.JSONDecodeError:
                continue
            if msg.get("id") == req_id:
                return msg

    # -- measurements -----------------------------------------------------

    def list_tools(self, timeout: float = 30.0) -> dict[str, Any]:
        self._send({"jsonrpc": "2.0", "id": self._next(), "method": "tools/list", "params": {}})
        response = self._wait(self._id, timeout)
        tools = (response.get("result") or {}).get("tools") or []
        return {
            "count": len(tools),
            "names": sorted(tool["name"] for tool in tools),
            "schemas": {tool["name"]: tool.get("inputSchema") for tool in tools},
        }

    def call(
        self, tool: str, arguments: dict[str, Any], timeout: float = 300.0
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Call one tool; return (envelope, measurement).

        The measurement carries the wall-clock seconds and the **response text
        size in bytes** - the number a "compact response" claim has to be made
        of, since JSON-RPC has no other place to put a payload.
        """
        started = time.perf_counter()
        self._send(
            {
                "jsonrpc": "2.0",
                "id": self._next(),
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments},
            }
        )
        response = self._wait(self._id, timeout)
        seconds = time.perf_counter() - started
        result = response.get("result") or {}
        text = None
        for block in result.get("content") or []:
            if block.get("type") == "text":
                text = block.get("text")
        measured = {
            "seconds": round(seconds, 4),
            "bytes": len(text.encode("utf-8")) if isinstance(text, str) else None,
            "is_error": bool(result.get("isError")),
        }
        if text is None:
            raise RuntimeError(f"{tool} returned no text block: {response}")
        return json.loads(text), measured

    def close(self) -> None:
        try:
            self.proc.stdin.close()
        except Exception:  # noqa: BLE001 - best-effort shutdown
            pass
        self.proc.terminate()


def _smoke() -> int:  # pragma: no cover - manual harness check
    client = McpClient(name="p28-smoke")
    try:
        listing = client.list_tools()
        print(json.dumps({"server": client.startup.server_info, "count": listing["count"]}, indent=1))
    finally:
        client.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_smoke())
