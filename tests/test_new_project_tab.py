"""`new_project_tab`: the read-back is the feature, and the live property is the trap.

The fill writes into whatever project is CURRENT, and with `clean=True` it deletes that
project's AI_* tracks and regions. So "align the next take" in a session that already holds an
aligned project has to start from a fresh tab - and doing that from a script is what the previous
run did, which left the tab as the one step of this toolchain living outside it.

Two things are pinned here because neither fails loudly:

  * `Main_OnCommand` answers NOTHING. A command id that does not exist in this REAPER build, a
    modal dialog eating the action, a tab that opens without becoming current - all of them look
    exactly like success. So the tool reads the current project and the open-tab list before and
    after, and `created` is true only when one of them MOVED.
  * `reapy.Project().tracks` is a LIVE property (it queries reapy on every access). Read after the
    switch it returns the NEW tab's tracks, so the "what did I leave open" line would describe a
    15-track project as empty - the fakes below re-read the current tab on every access, which is
    what makes that mistake a failure instead of a wrong number in a report.

Hermetic: REAPER is not contacted, `RPR`/`reapy` are replaced. Run with the interpreter the
REAPER server itself uses (`py -3.11`), because that is the environment the tool will live in.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import controllers.project_controller as project_module  # noqa: E402
from controllers.project_controller import ProjectController  # noqa: E402


class _Project:
    """`reapy.Project()`: `id` and `tracks` are read from the CURRENT tab on EVERY access."""

    def __init__(self, state) -> None:
        self._state = state

    @property
    def id(self):
        return self._state.tabs[-1]["pointer"]

    @property
    def tracks(self):
        return [object()] * self._state.tabs[-1]["tracks"]


class _FakeRPR:
    def __init__(self, state, *, opens: int) -> None:
        self.state = state
        self.opens = opens
        self.commands: list[tuple[int, int]] = []

    def EnumProjects(self, index, buf, size):
        if index >= len(self.state.tabs):
            return ["(ReaProject*)0x0000000000000000", index, "", size]
        tab = self.state.tabs[index]
        return [tab["pointer"], index, tab["file"], size]

    def Main_OnCommand(self, command, flags):
        self.commands.append((int(command), int(flags)))
        for _ in range(self.opens):
            self.state.tabs.append(
                {
                    "pointer": "(ReaProject*)0xNEW%d" % len(self.state.tabs),
                    "file": "",
                    "tracks": self.state.new_tracks,
                }
            )


class _State:
    def __init__(self, tabs, *, opens: int, new_tracks: int = 0) -> None:
        self.tabs = [dict(tab) for tab in tabs]
        self.new_tracks = new_tracks
        self.opens = opens


@pytest.fixture()
def controller(monkeypatch):
    def _make(tabs, *, opens: int = 1, new_tracks: int = 0):
        state = _State(tabs, opens=opens, new_tracks=new_tracks)
        fake = _FakeRPR(state, opens=state.opens)
        monkeypatch.setattr(project_module, "RPR", fake)
        monkeypatch.setattr(project_module.reapy, "Project", lambda: _Project(state))
        return ProjectController(debug=False), fake, state
    return _make


ALIGNED = {"pointer": "(ReaProject*)0xDnB", "file": r"C:\bundles\dnb\project\aligned.rpp", "tracks": 15}


def test_a_new_tab_is_opened_and_verified_against_the_host(controller):
    """The tool's whole claim: the tab EXISTS and is CURRENT, read back rather than assumed."""
    tab, fake, _state = controller([ALIGNED])

    answer = tab.new_project_tab()

    assert answer["status"] == "success", answer
    assert answer["created"] is True, answer
    assert answer["track_count"] == 0, answer
    assert answer["tabs_before"] == 1 and answer["tabs_open"] == 2, answer
    assert answer["project"] == "(ReaProject*)0xNEW1", answer
    assert fake.commands == [(41929, 0)], fake.commands  # ignore-template by default
    assert answer["command"] == 41929, answer
    assert "warning" not in answer, answer


def test_the_project_left_open_is_named_not_just_counted(controller):
    """A tab is not destructive, so the answer has to say WHAT stayed open - file and track count.

    ALIGNED is LAST because `reapy.Project()` answers the current tab and the fake makes that the
    last entry of the enumeration - the current tab is NOT always the first one (probed in the
    live session: four tabs open, the current one at index 3).
    """
    tab, _fake, _state = controller([{"pointer": "(ReaProject*)0xOTHER", "file": "", "tracks": 0}, ALIGNED])

    answer = tab.new_project_tab()

    assert answer["left_behind"]["project"] == ALIGNED["pointer"], answer
    assert answer["left_behind"]["file"] == ALIGNED["file"], answer
    # read BEFORE the switch: a live `tracks` property read after it would say 0
    assert answer["left_behind"]["track_count"] == 15, answer
    assert "warning" not in answer["left_behind"], answer


def test_a_never_saved_project_left_open_is_flagged(controller):
    """It exists in memory only - one crash from gone, and this is the tool that can see it."""
    tab, _fake, _state = controller([{"pointer": "(ReaProject*)0xLIVE", "file": "", "tracks": 4}])

    answer = tab.new_project_tab()

    assert answer["created"] is True, answer
    assert answer["left_behind"]["file"] == "", answer
    assert "never been saved" in answer["left_behind"]["warning"], answer


def test_a_command_that_changes_nothing_is_an_error_not_a_success(controller):
    """`Main_OnCommand` returns nothing: a build without that action id must not read as a tab."""
    tab, fake, _state = controller([ALIGNED], opens=0)

    answer = tab.new_project_tab()

    assert fake.commands == [(41929, 0)], fake.commands  # it WAS fired
    assert answer["created"] is False, answer
    assert answer["status"] == "error", answer
    assert answer["hints"], answer
    assert "NOT" in answer["message"], answer


def test_a_tab_that_is_not_empty_is_flagged_because_a_template_was_applied(controller):
    """A template's tracks are indistinguishable from the fill's own in the read-back."""
    tab, _fake, _state = controller([ALIGNED], new_tracks=3)

    answer = tab.new_project_tab()

    assert answer["track_count"] == 3, answer
    assert "template" in answer["warning"], answer
    assert answer["status"] == "success", answer


def test_the_template_can_be_asked_for(controller):
    tab, fake, _state = controller([ALIGNED])

    tab.new_project_tab(ignore_template=False)

    assert fake.commands == [(40859, 0)], fake.commands


def test_the_enumeration_stops_at_the_null_pointer(controller):
    """`CountProjects` does not exist in reapy, so the count comes from walking the list."""
    tab, _fake, _state = controller([ALIGNED, {"pointer": "(ReaProject*)0xA", "file": "", "tracks": 1}])

    answer = tab.new_project_tab()

    # two tabs before, three after - and not 64, which is what a walk without the null stop returns
    assert answer["tabs_before"] == 2, answer
    assert answer["tabs_open"] == 3, answer
    assert [entry["index"] for entry in answer["tabs"]] == [0, 1, 2], answer["tabs"]
    assert answer["tabs"][0]["file"] == ALIGNED["file"], answer["tabs"]


def test_the_tool_is_registered_with_the_name_the_caller_uses():
    """A tool that exists in the controller while the transport never exposes it is the defect
    this workspace has already paid for once (see test_project_save.py)."""
    import asyncio

    from mcp.server.fastmcp import FastMCP

    import mcp_tools

    mcp = FastMCP("new-project-tab-test")
    mcp_tools.setup_mcp_tools(mcp, object())
    tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}

    assert "new_project_tab" in tools, sorted(tools)
    schema = tools["new_project_tab"].inputSchema
    # nothing is REQUIRED: the default is the safe half of the pair (no template), and a caller
    # that has nothing to say about templates should still be able to open a tab
    assert schema.get("required", []) == [], schema
    assert "ignore_template" in schema["properties"], schema
    assert "ctx" not in schema["properties"], schema
