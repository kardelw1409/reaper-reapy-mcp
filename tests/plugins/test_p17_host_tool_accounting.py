"""P17 host-tool accounting: `add_fx` must not answer success for a failed add.

Measured on the current code before this fix: ``FXController.add_fx`` answers a
**bool**, while the tool tested ``fx_index >= 0`` - and ``False >= 0`` is ``True``
in Python, so a failed add produced ``{"status": "success", "message": "... at
index False"}``. The false success is exactly the kind of accounting defect the
practical workflow would hit while placing three Vital instances.

The test drives the real registered tool through a real ``FastMCP`` instance and
a fake controller (no host, no REAPER): the tool factory is the same one the
server uses, so the envelope under test is the envelope clients receive.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from mcp.server.fastmcp import FastMCP

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"
for _path in (SRC, REPO_ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import mcp_tools  # noqa: E402


class FakeController:
    """The two controller calls the `add_fx` tool is allowed to make."""

    def __init__(self, added: bool, fx_list: list[dict] | None = None) -> None:
        self.added = added
        self.fx_list = fx_list if fx_list is not None else []
        self.fx_list_calls = 0

    def add_fx(self, track_index: int, fx_name: str) -> bool:
        return self.added

    def get_fx_list(self, track_index: int) -> list[dict]:
        self.fx_list_calls += 1
        return self.fx_list

    def create_track(self, name: str | None = None) -> int:
        return 7


def make_tool(controller: FakeController, name: str):
    mcp = FastMCP("p17-accounting-test")
    mcp_tools.setup_mcp_tools(mcp, controller)
    registered = mcp._tool_manager.get_tool(name)
    assert registered is not None and registered.fn is not None, f"the tool {name!r} did not register"
    return registered.fn


def test_failed_add_fx_is_an_error_not_a_false_success():
    controller = FakeController(added=False)
    add_fx = make_tool(controller, "add_fx")

    payload = add_fx(ctx=None, track_index=2, fx_name="Vital")

    assert payload["status"] == "error", (
        "a failed add must never answer success; the old code's `False >= 0` bug "
        f"did exactly that (payload: {payload})"
    )
    assert "index" not in payload.get("message", ""), payload
    assert controller.fx_list_calls == 0, "a failed add must not spend an fx-list read"


def test_successful_add_fx_reports_the_re_read_index():
    # the add appends to the end: the track now has two FX, the new one at index 1
    controller = FakeController(
        added=True,
        fx_list=[{"index": 0, "name": "ReaEQ"}, {"index": 1, "name": "Vital"}],
    )
    add_fx = make_tool(controller, "add_fx")

    payload = add_fx(ctx=None, track_index=2, fx_name="Vital")

    assert payload["status"] == "success", payload
    assert payload["track_index"] == 2, payload
    assert payload["fx_index"] == 1, payload
    assert "at index 1" in payload["message"], payload
    assert controller.fx_list_calls == 1, "the index must come from a live re-read, not from the bool"


def test_successful_add_with_unreadable_fx_list_claims_no_index():
    controller = FakeController(added=True, fx_list=[])
    add_fx = make_tool(controller, "add_fx")

    payload = add_fx(ctx=None, track_index=2, fx_name="Vital")

    assert payload["status"] == "success", payload
    assert payload["fx_index"] is None, (
        "when the track cannot be listed the tool must not invent an index"
    )
    assert "at index" not in payload["message"], payload


def test_create_track_returns_the_index_machine_readably():
    controller = FakeController(added=True)
    create_track = make_tool(controller, "create_track")

    payload = create_track(ctx=None, name="V3-A Deep 808")

    assert payload["status"] == "success", payload
    assert payload["track_index"] == 7, (
        "the workflow `create track -> add fx` needs the index as a value, not as prose"
    )
