"""P36: the descriptions a client sees must carry the choice guidance (offline).

The brief (§5/§6): a model reading ONE tool description must choose the batch
path for multi-control changes and for whole MIDI parts, without reading the
sources. These tests build the REAL surfaces - the same `FastMCP` factories the
server uses - and assert (a) the significant guidance is present, (b) the tool
names the guidance points at are exact, and (c) the descriptions stay bounded.
Prose is not pinned literally: only the decision-relevant needles.
"""

from __future__ import annotations

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

from reaper_reapy_mcp.plugins import api as A  # noqa: E402
from reaper_reapy_mcp.plugins import tools as T  # noqa: E402


class _UnusedController:
    """Registration only - no tool body runs in these tests."""


@pytest.fixture(scope="module")
def classic_descriptions() -> dict[str, str]:
    mcp = FastMCP("p36-descriptions-classic")
    mcp_tools.setup_mcp_tools(mcp, _UnusedController())
    return {
        name: (mcp._tool_manager.get_tool(name).description or "")
        for name in ("set_fx_param", "get_fx_param", "get_fx_param_list",
                     "add_midi_note", "add_midi_notes", "insert_midi_item", "create_midi_item")
    }


@pytest.fixture(scope="module")
def plugin_descriptions() -> dict[str, str]:
    mcp = FastMCP("p36-descriptions-plugin")
    T.setup_plugin_tools(mcp, config=A.PluginConfig(provider_ids=("vital",), env={}))
    return {
        name: (mcp._tool_manager.get_tool(name).description or "")
        for name in ("plugin_write", "plugin_read", "plugin_find", "plugin_prepare")
    }


def _assert_needles(description: str, needles: tuple[str, ...]) -> None:
    lowered = description.lower()
    missing = [needle for needle in needles if needle.lower() not in lowered]
    assert missing == [], f"missing guidance {missing} in:\n{description}"


# ---------------------------------------------------------------------------
# single vs batch for parameters
# ---------------------------------------------------------------------------


def test_set_fx_param_points_at_the_batch_write_and_the_reads(classic_descriptions):
    _assert_needles(
        classic_descriptions["set_fx_param"],
        ("one fx parameter", "plugin_write", "get_fx_param_list", "normalized"),
    )


def test_get_fx_param_points_at_the_batch_reads(classic_descriptions):
    _assert_needles(classic_descriptions["get_fx_param"], ("get_fx_param_list", "plugin_read"))


def test_get_fx_param_list_points_at_the_prepared_read_path(classic_descriptions):
    _assert_needles(classic_descriptions["get_fx_param_list"], ("plugin_prepare", "plugin_read"))


def test_plugin_write_is_the_batch_tool_and_names_its_prerequisites(plugin_descriptions):
    _assert_needles(
        plugin_descriptions["plugin_write"],
        ("batch write", "one request", "plugin_prepare", "dry_run", "set_fx_param"),
    )


def test_plugin_read_and_find_are_the_named_read_pair(plugin_descriptions):
    _assert_needles(plugin_descriptions["plugin_read"], ("plugin_find", "plugin_prepare"))
    _assert_needles(plugin_descriptions["plugin_find"], ("plugin_read", "plugin_write"))


def test_plugin_prepare_says_it_runs_before_the_others(plugin_descriptions):
    _assert_needles(plugin_descriptions["plugin_prepare"], ("before", "plugin_read", "reuse"))


# ---------------------------------------------------------------------------
# MIDI: whole part in one call; the file-based path is named
# ---------------------------------------------------------------------------


def test_add_midi_note_warns_against_one_call_per_note(classic_descriptions):
    _assert_needles(
        classic_descriptions["add_midi_note"],
        ("add_midi_notes", "insert_midi_item", "do not call this tool once"),
    )


def test_add_midi_notes_is_the_whole_part_call(classic_descriptions):
    _assert_needles(
        classic_descriptions["add_midi_notes"],
        ("whole part", "one call", "insert_midi_item", "entire batch is refused"),
    )


def test_the_file_path_and_the_item_path_name_each_other(classic_descriptions):
    _assert_needles(classic_descriptions["insert_midi_item"], ("add_midi_notes", "create_midi_item"))
    _assert_needles(classic_descriptions["create_midi_item"], ("add_midi_notes", "insert_midi_item"))


# ---------------------------------------------------------------------------
# shape: bounded prose, stable names
# ---------------------------------------------------------------------------


def test_descriptions_stay_bounded_and_non_empty(classic_descriptions, plugin_descriptions):
    for name, description in {**classic_descriptions, **plugin_descriptions}.items():
        assert description.strip(), name
        assert len(description) < 4000, (name, len(description))
