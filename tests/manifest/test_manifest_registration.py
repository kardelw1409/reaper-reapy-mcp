"""The registration half of E2/F4: server.py's ONE line must really expose the tools.

This is what catches a broken decorator or a renamed signature - the pure
load/validate logic can be perfect while the tool never appears.

TWO tools since F4, and they are different acts: `build_project_file` writes the
bundle as TEXT (no project touched) and `apply_audiocpp_manifest` fills a live one.
"""

from __future__ import annotations

import asyncio

from mcp.server.fastmcp import FastMCP

from reaper_reapy_mcp.manifest import setup_manifest_tools


def _tools() -> list:
    mcp = FastMCP("manifest-test")
    setup_manifest_tools(mcp, controller=object())
    return asyncio.run(mcp.list_tools())


def test_both_tools_are_added_and_named():
    tools = _tools()
    assert sorted(tool.name for tool in tools) == [
        "apply_audiocpp_manifest",
        "build_project_file",
    ]


def test_signatures_match_the_contract():
    by_name = {tool.name: tool for tool in _tools()}

    live = by_name["apply_audiocpp_manifest"].inputSchema["properties"]
    assert set(live) >= {"bundle_dir", "dry_run", "clean", "save_as"}
    assert live["bundle_dir"]["default"] == "latest"
    assert live["dry_run"]["default"] is False
    assert live["clean"]["default"] is False
    # ctx is injected by the server, not asked of the caller
    assert "ctx" not in live

    build = by_name["build_project_file"].inputSchema["properties"]
    assert set(build) >= {"bundle_dir", "out_path", "title", "open_after"}
    assert build["bundle_dir"]["default"] == "latest"
    # NOT opening by default: the build itself needs no host at all
    assert build["open_after"]["default"] is False
    assert "ctx" not in build


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


def test_the_build_tool_refuses_a_missing_bundle_with_a_user_facing_answer(tmp_path):
    """The same contract as the fill: a refusal is status=error plus hints, never a traceback."""
    mcp = FastMCP("manifest-test")
    setup_manifest_tools(mcp, controller=object())

    async def call() -> dict:
        _content, structured = await mcp.call_tool(
            "build_project_file", {"bundle_dir": str(tmp_path / "absent"), "open_after": False}
        )
        return structured["result"]

    result = asyncio.run(call())
    assert result["ok"] is False
    assert result["status"] == "error"
