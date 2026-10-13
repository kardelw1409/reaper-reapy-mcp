"""The registration half of E2: server.py's ONE line must really expose the tool.

This is what catches a broken decorator or a renamed signature - the pure
load/validate logic can be perfect while the tool never appears.
"""

from __future__ import annotations

import asyncio

from mcp.server.fastmcp import FastMCP

from reaper_reapy_mcp.manifest import setup_manifest_tools


def _tools() -> list:
    mcp = FastMCP("manifest-test")
    setup_manifest_tools(mcp, controller=object())
    return asyncio.run(mcp.list_tools())


def test_exactly_one_tool_is_added_and_named():
    tools = _tools()
    assert [t.name for t in tools] == ["apply_audiocpp_manifest"]


def test_signature_matches_the_contract():
    (tool,) = _tools()
    properties = tool.inputSchema["properties"]
    assert set(properties) >= {"bundle_dir", "dry_run", "clean", "save_as"}
    assert properties["bundle_dir"]["default"] == "latest"
    assert properties["dry_run"]["default"] is False
    assert properties["clean"]["default"] is False
    # ctx is injected by the server, not asked of the caller
    assert "ctx" not in properties


def test_manifest_error_becomes_a_user_facing_error_answer(tmp_path, monkeypatch):
    """A refusal must come back as status=error + hints, never as a traceback."""
    monkeypatch.setenv("AUDIOCPP_MANIFEST_POINTER", str(tmp_path / "absent.txt"))
    mcp = FastMCP("manifest-test")
    setup_manifest_tools(mcp, controller=object())

    async def call() -> dict:
        _content, structured = await mcp.call_tool("apply_audiocpp_manifest", {"bundle_dir": "latest"})
        # FastMCP wraps a Dict return under "result" in structuredContent.
        assert structured["result"]["status"] == "error"
        return structured["result"]

    result = asyncio.run(call())
    assert "pointer" in result["message"]
    assert result["hints"]
