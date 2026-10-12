"""P29 stage C: listing loaded FX is the host's job, not a provider's.

P28 closed half of this: a *selector* naming a slot could be answered from the
host's own facts with no provider configured. What remained was the listing itself
- "what is loaded here" - which still depended on a configured provider, making
every new plugin a configuration task. `plugin_instances(scope="project")` answers
it from the host bridge: track list, per-slot identity facts (name, class id,
module, parameter count), and a ref-shaped row a caller can pass straight back.

The tests below are the brief's list, plus the properties that make the answer
trustworthy: a slot whose identity cannot be read is reported rather than dropped,
the selector rule is the provider path's own, and the default scope is unchanged.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

import fixtures as F  # noqa: E402
from mock_host import MockFxSlot, MockHostBridge, MockTrack  # noqa: E402

from reaper_reapy_mcp.plugins import api as A
from reaper_reapy_mcp.plugins import host_scope as HS
from reaper_reapy_mcp.plugins import tools as T

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

#: A second build: same name, different class id/module - i.e. a plugin no
#: configured provider claims.
FOREIGN_MODULE = "other.vst3"
FOREIGN_CLASS = "00112233445566778899AABBCCDDEEFF"


def call(mcp: Any, name: str, arguments: dict | None = None) -> dict:
    blocks = asyncio.run(mcp.call_tool(name, arguments or {}))
    text = next(block.text for block in blocks if getattr(block, "text", None))
    return json.loads(text)


def build_project() -> MockHostBridge:
    """Four tracks: a claimed FX, two unclaimed ones (one with a twin name), an empty track."""
    return MockHostBridge(
        [
            MockTrack(0, "Bass", [MockFxSlot()]),
            MockTrack(1, "Keys", [MockFxSlot(name="Demo Plugin", module=FOREIGN_MODULE, class_id=FOREIGN_CLASS)]),
            MockTrack(2, "Pad", [MockFxSlot(name="Demo Plugin", module=FOREIGN_MODULE, class_id=FOREIGN_CLASS)]),
            MockTrack(3, "Empty", []),
        ]
    )


@pytest.fixture
def project(tmp_path, monkeypatch):
    from mcp.server.fastmcp import FastMCP

    from mock_provider import MockProvider
    from reaper_reapy_mcp.plugins.providers import registry as PR

    bridge = build_project()
    monkeypatch.setattr(A, "_BRIDGE", bridge)
    monkeypatch.setattr(A, "_REGISTRY", None)
    monkeypatch.setenv("REAPER_MCP_PLUGIN_DISCOVERY", str(tmp_path / "discovery"))
    # The repo ships one production provider; the test double registers itself under
    # its own id (the P9 pattern), so "a project with one claimed and two unclaimed
    # slots" is a real mixed answer rather than a mocked one.
    monkeypatch.setitem(PR._FACTORIES, "demo", lambda: MockProvider())

    def _server(provider_ids: tuple[str, ...] = ("demo",)):
        mcp = FastMCP("probe")
        T.setup_plugin_tools(
            mcp, config=A.PluginConfig(provider_ids=provider_ids, cache_dir=str(tmp_path / "cache"), env={})
        )
        return mcp

    return {"bridge": bridge, "server": _server, "tmp": tmp_path}


# ---------------------------------------------------------------------------
# The listing
# ---------------------------------------------------------------------------


def test_the_project_scope_lists_every_slot_with_and_without_a_provider(project):
    mcp = project["server"](("demo",))
    envelope = call(mcp, "plugin_instances", {"scope": "project"})
    assert envelope["ok"] is True, envelope
    data = envelope["data"]
    assert data["scope"] == "project"
    assert data["slots_scanned"] == 3, "four tracks, three slots (one track is empty)"
    assert data["tracks_scanned"] == 4
    rows = data["instances"]
    assert [(row["location"]["track_index"], row["location"]["fx_index"]) for row in rows] == [
        (0, 0),
        (1, 0),
        (2, 0),
    ]
    claimed = [row for row in rows if not row["providerless"]]
    assert [row["location"]["track_index"] for row in claimed] == [0]
    assert claimed[0]["provider"] == "demo"
    assert [row["provider"] for row in rows if row["providerless"]] == [HS.SCOPE_ID, HS.SCOPE_ID]
    # the facts a caller needs to choose a target, straight from the host
    for row in rows:
        assert row["fx_name"]
        assert row["module"]
        assert isinstance(row["param_count"], int) and row["param_count"] > 0
        assert row["identity"]["class_id"]


def test_a_listed_row_addresses_the_same_instance(project):
    """The row is the object the other tools accept: a caller passes back what it
    read instead of re-deriving an address."""
    mcp = project["server"](("demo",))
    row = call(mcp, "plugin_instances", {"scope": "project"})["data"]["instances"][0]
    describe = call(mcp, "plugin_describe", {"instance": {"location": row["location"], "identity": row["identity"]}})
    assert describe["ok"] is True, describe
    assert describe["meta"]["instance"]["location"] == row["location"]


def test_no_provider_configured_still_lists_everything(project):
    """The defect this stage closes: listing used to require a provider."""
    mcp = project["server"](())
    envelope = call(mcp, "plugin_instances", {"scope": "project"})
    assert envelope["ok"] is True, envelope
    rows = envelope["data"]["instances"]
    assert len(rows) == 3
    assert all(row["providerless"] for row in rows)
    assert [row["code"] for row in envelope["warnings"]] == ["NoProviderConfigured"]


def test_an_empty_project_is_an_empty_list_not_an_error(tmp_path, monkeypatch):
    from mcp.server.fastmcp import FastMCP

    bridge = MockHostBridge([MockTrack(0, "Only", [])])
    monkeypatch.setattr(A, "_BRIDGE", bridge)
    monkeypatch.setattr(A, "_REGISTRY", None)
    monkeypatch.setenv("REAPER_MCP_PLUGIN_DISCOVERY", str(tmp_path / "discovery"))
    mcp = FastMCP("probe")
    T.setup_plugin_tools(mcp, config=A.PluginConfig(provider_ids=(), env={}))
    envelope = call(mcp, "plugin_instances", {"scope": "project"})
    assert envelope["ok"] is True, envelope
    assert envelope["data"]["instances"] == []
    assert envelope["data"]["ambiguous"] is False
    assert envelope["data"]["tracks_scanned"] == 1


def test_duplicate_fx_names_on_different_tracks_stay_distinguishable(project):
    mcp = project["server"](("demo",))
    rows = call(mcp, "plugin_instances", {"scope": "project"})["data"]["instances"]
    twins = [row for row in rows if row["fx_name"] == "Demo Plugin"]
    assert len(twins) == 2
    assert {row["location"]["track_index"] for row in twins} == {1, 2}
    assert {row["track_name"] for row in twins} == {"Keys", "Pad"}
    assert twins[0]["identity"] == twins[1]["identity"], (
        "the fingerprint is deliberately the same - location is what disambiguates"
    )
    assert twins[0]["display"] != twins[1]["display"]


def test_a_name_selector_that_matches_twins_is_ambiguous_not_a_pick(project):
    mcp = project["server"](("demo",))
    envelope = call(mcp, "plugin_instances", {"scope": "project", "selector": {"name": "Demo Plugin"}})
    assert envelope["ok"] is True, envelope
    assert len(envelope["data"]["instances"]) == 2
    assert envelope["data"]["ambiguous"] is True


def test_a_track_selector_narrows_the_listing(project):
    mcp = project["server"](("demo",))
    envelope = call(mcp, "plugin_instances", {"scope": "project", "selector": {"track_index": 2}})
    assert envelope["ok"] is True, envelope
    assert [row["location"]["track_index"] for row in envelope["data"]["instances"]] == [2]
    assert envelope["data"]["tracks_scanned"] == 1


def test_a_stale_slot_is_reported_not_dropped(project, monkeypatch):
    """An index whose identity cannot be read must not look like an absent FX."""
    from reaper_reapy_mcp.plugins.errors import Unreadable

    bridge = project["bridge"]
    original = bridge.fx_facts

    def flaky(track_index, fx_index):
        if (track_index, fx_index) == (2, 0):
            raise Unreadable(
                "the FX slot does not exist",
                detail={"track_index": 2, "fx_index": 0, "n_fx": 1},
            )
        return original(track_index, fx_index)

    monkeypatch.setattr(bridge, "fx_facts", flaky)
    mcp = project["server"](("demo",))
    envelope = call(mcp, "plugin_instances", {"scope": "project"})
    assert envelope["ok"] is True, envelope
    assert [row["location"]["track_index"] for row in envelope["data"]["instances"]] == [0, 1]
    refused = envelope["data"]["refused"]
    assert [row["location"] for row in refused] == [{"track_index": 2, "fx_index": 0}]
    assert refused[0]["code"] == "Unreadable"
    assert refused[0]["detail"]["n_fx"] == 1
    assert [row["code"] for row in envelope["warnings"]] == ["SlotsUnreadable"]
    assert "not offered as a target" in refused[0]["note"]


def test_readiness_is_opt_in_and_the_default_listing_stays_cheap(project):
    mcp = project["server"](("demo",))
    cheap = call(mcp, "plugin_instances", {"scope": "project"})["data"]
    assert cheap["readiness_included"] is False
    assert all("artifact" not in row for row in cheap["instances"])
    assert all("present" in row["artifact_file"] for row in cheap["instances"])

    full = call(mcp, "plugin_instances", {"scope": "project", "include_readiness": True})["data"]
    assert full["readiness_included"] is True
    for row in full["instances"]:
        assert row["artifact"]["status"] in ("absent", "ok", "refused", "corrupt")
        assert row["ready"] is False, "nothing was prepared in this fixture"
        assert row["remedy"]


def test_the_scope_argument_is_validated(project):
    mcp = project["server"](("demo",))
    envelope = call(mcp, "plugin_instances", {"scope": "everything"})
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "InvalidValue"
    assert envelope["error"]["detail"]["accepted"] == ["provider", "project"]


def test_the_default_scope_is_unchanged(project):
    """The provider scope keeps its own answers: only claimed instances, and the
    historical refusal when nothing configured and no selector was given."""
    mcp = project["server"](("demo",))
    envelope = call(mcp, "plugin_instances", {})
    assert envelope["ok"] is True, envelope
    assert len(envelope["data"]["instances"]) == 1, "only the claimed slot"
    assert envelope["data"]["instances"][0]["provider"] == "demo"
    assert "scope" not in envelope["data"]

    zero = project["server"](())
    refused = call(zero, "plugin_instances", {})
    assert refused["ok"] is False
    assert refused["error"]["code"] == "UnsupportedProvider"


def test_include_identity_false_still_leaves_an_addressable_row(project):
    mcp = project["server"](("demo",))
    envelope = call(mcp, "plugin_instances", {"scope": "project", "include_identity": False})
    assert envelope["ok"] is True, envelope
    for row in envelope["data"]["instances"]:
        assert "identity" not in row
        assert row["location"]["track_index"] >= 0
