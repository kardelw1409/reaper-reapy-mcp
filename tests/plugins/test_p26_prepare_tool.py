"""P26: the `plugin_prepare` tool and the host bridge's metadata channels.

Brief §17 scenario map for this file:

- **16** a prepared profile is re-used without a second sweep (the property is
  measured: the host call counters must not move, and the answer must say
  `served`);
- **19** identity / engine-version validation on the way in;
- §6 conformance: the bridge's parsing of the host's *measured* tuple layouts
  (`TrackFX_GetParameterStepSizes`, `TrackFX_FormatParamValueNormalized`,
  `TrackFX_GetNamedConfigParm`, `TrackFX_GetParamNormalized`) is pinned against the
  field positions the P22/P23 probes read, because a wrong slot would make every
  derived property wrong while looking plausible.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from reaper_reapy_mcp.plugins import api as A
from reaper_reapy_mcp.plugins import tools as T
from reaper_reapy_mcp.plugins.discovery.store import PreparedStore
from reaper_reapy_mcp.plugins.host.reaper import ReaperHostBridge
from reaper_reapy_mcp.plugins.model import PluginInstanceRef

import discovery_double as D

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

REF = PluginInstanceRef(
    host="reaper",
    location={"track_index": 0, "fx_index": 0},
    provider="host-scope",
    identity={},
    display="fake",
)


def build_bridge() -> D.FakeDiscoveryBridge:
    return D.FakeDiscoveryBridge(
        [
            D.FakeControl(0, "alpha_gain", display=D.linear_display(0.0, 100.0, "%")),
            D.FakeControl(1, "alpha_mode", display=D.labels_display(["low", "mid", "high"])),
            D.FakeControl(
                2, "alpha_on", display=D.labels_display(["off", "on"]), is_toggle=True, step=1.0
            ),
            D.FakeControl(3, "beta_gain", display=D.linear_display(-60.0, 12.0, "dB")),
        ]
    )


def call(mcp: Any, name: str, arguments: dict | None = None) -> dict:
    blocks = asyncio.run(mcp.call_tool(name, arguments or {}))
    text = next(block.text for block in blocks if getattr(block, "text", None))
    return json.loads(text)


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    from mcp.server.fastmcp import FastMCP

    bridge = build_bridge()
    monkeypatch.setattr(A, "_BRIDGE", bridge)
    monkeypatch.setattr(A, "_REGISTRY", None)
    monkeypatch.setenv("REAPER_MCP_PLUGIN_DISCOVERY", str(tmp_path / "discovery"))
    config = A.PluginConfig(provider_ids=(), cache_dir=str(tmp_path / "cache"), env={})
    mcp = FastMCP("probe")
    T.setup_plugin_tools(mcp, config=config)
    yield {"bridge": bridge, "mcp": mcp, "config": config, "directory": tmp_path / "discovery"}


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------


def test_plugin_prepare_is_registered_and_declared_read_only():
    assert "plugin_prepare" in T.TOOL_NAMES
    spec = T.SPEC["plugin_prepare"]
    assert spec["destructive"] is False
    assert spec["mutating_ops"] == ()
    assert spec["args"]["mode"]["default"] == "reuse"
    assert "dry_run" not in spec["args"], "a read-only tool takes no write switch"


def test_inventory_mode_reads_metadata_and_stores_an_artifact(prepared):
    envelope = call(prepared["mcp"], "plugin_prepare", {"instance": {}, "mode": "inventory"})
    assert envelope["ok"] is True
    data = envelope["data"]
    assert data["prepared"]["status"] == "measured"
    assert data["prepared"]["sha256_matches_disk"] is True
    assert data["counts"]["parameters"] == 4
    assert envelope["meta"]["host_calls"] > 0
    # inventory mode takes no display sample beyond the current-value channel
    assert prepared["bridge"].calls["format_params_normalized"] == 0
    assert data["classes"]


def test_prepare_mode_sweeps_the_display_and_builds_the_module_map(prepared):
    envelope = call(
        prepared["mcp"], "plugin_prepare", {"instance": {}, "mode": "prepare", "grid_points": 9}
    )
    data = envelope["data"]
    assert data["prepared"]["status"] == "measured"
    assert prepared["bridge"].calls["format_params_normalized"] >= 1
    assert len(prepared["bridge"].format_batches) == 1, "the sweep is batched per chunk"
    assert prepared["bridge"].format_batches[0] == 4 * 9
    modules = {row["id"]: row for row in data["modules"]}
    assert "alpha" in modules and "beta" in modules
    assert modules["alpha"]["n_controls"] == 3
    assert "controls" not in data, "the compact view must not carry the control list"


def test_scenario_16_reuse_serves_the_artifact_without_measuring_again(prepared):
    first = call(prepared["mcp"], "plugin_prepare", {"instance": {}, "mode": "prepare"})
    assert first["data"]["prepared"]["status"] == "measured"
    before_metadata = prepared["bridge"].calls["param_metadata"]
    before_format = prepared["bridge"].calls["format_params_normalized"]

    second = call(prepared["mcp"], "plugin_prepare", {"instance": {}, "mode": "reuse"})
    assert second["ok"] is True
    assert second["data"]["prepared"]["status"] == "served"
    assert "no host call" in second["data"]["prepared"]["note"]
    assert second["meta"]["host_calls"] == 0
    assert prepared["bridge"].calls["param_metadata"] == before_metadata
    assert prepared["bridge"].calls["format_params_normalized"] == before_format
    assert second["data"]["counts"] == first["data"]["counts"]
    assert second["data"]["modules"] == first["data"]["modules"]


def test_reuse_without_an_artifact_falls_back_to_the_metadata_only_pass(prepared):
    envelope = call(prepared["mcp"], "plugin_prepare", {"instance": {}, "mode": "reuse"})
    assert envelope["data"]["prepared"]["status"] == "measured"
    assert envelope["data"]["prepared"]["mode"] == "inventory"
    assert prepared["bridge"].calls["format_params_normalized"] == 0


def test_a_stored_inventory_artifact_does_not_answer_a_prepare_request(prepared):
    """P27: the modes are ranked (inventory < prepare < deep) and declared so.

    Measured live 2026-10-12: `mode="prepare"` served a stored `inventory`
    artifact with zero host calls and no display sweep - the answer contradicted
    the declared "prepare adds the display sweep". A request that outranks the
    stored artifact must re-measure, and the replaced artifact is reported.
    """
    first = call(prepared["mcp"], "plugin_prepare", {"instance": {}, "mode": "inventory"})
    assert first["data"]["prepared"]["mode"] == "inventory"
    before = prepared["bridge"].calls["format_params_normalized"]

    upgraded = call(prepared["mcp"], "plugin_prepare", {"instance": {}, "mode": "prepare"})
    assert upgraded["data"]["prepared"]["status"] == "measured"
    assert upgraded["data"]["prepared"]["mode"] == "prepare"
    assert prepared["bridge"].calls["format_params_normalized"] > before
    replaced = upgraded["data"]["prepared"]["previous_artifact"]
    assert replaced["rule"] == "mode_strength" and replaced["status"] == "not_used"
    # the artifact on disk is now the prepare-mode one, and a later reuse serves it
    served = call(prepared["mcp"], "plugin_prepare", {"instance": {}, "mode": "reuse"})
    assert served["data"]["prepared"]["status"] == "served"
    assert served["data"]["prepared"]["mode"] == "prepare"


def test_a_stored_prepare_artifact_answers_a_prepare_request(prepared):
    call(prepared["mcp"], "plugin_prepare", {"instance": {}, "mode": "prepare"})
    before = prepared["bridge"].calls["format_params_normalized"]
    again = call(prepared["mcp"], "plugin_prepare", {"instance": {}, "mode": "prepare"})
    assert again["data"]["prepared"]["status"] == "served"
    assert prepared["bridge"].calls["format_params_normalized"] == before


def test_a_limited_run_does_not_replace_a_broader_stored_artifact(prepared):
    """P27: `sample_indices`/`max_controls` cover part of the identity; a limited
    run must not overwrite a full measurement (the only copy of expensive host
    work). The measurement is still returned; only the store refuses."""
    first = call(prepared["mcp"], "plugin_prepare", {"instance": {}, "mode": "inventory"})
    store = PreparedStore(directory=prepared["directory"])
    stored_before = store.load(first["data"]["identity"])
    assert stored_before.ok

    limited = call(
        prepared["mcp"],
        "plugin_prepare",
        {"instance": {}, "mode": "prepare", "sample_indices": [0, 1], "grid_points": 5},
    )
    data = limited["data"]
    assert data["prepared"]["status"] == "measured"
    assert prepared["bridge"].calls["format_params_normalized"] > 0, "the limited run did measure"
    assert data["prepared"]["stored"] is False
    assert (
        data["prepared"]["store_refusal"]["rule"]
        == "limited_run_would_replace_a_broader_artifact"
    )
    stored_after = store.load(data["identity"])
    assert stored_after.ok
    assert stored_after.document.get("content_sha256") == stored_before.document.get(
        "content_sha256"
    ), "the broader stored artifact must be byte-identical after a limited run"


def test_force_measures_again_even_when_an_artifact_exists(prepared):
    call(prepared["mcp"], "plugin_prepare", {"instance": {}, "mode": "prepare"})
    before = prepared["bridge"].calls["format_params_normalized"]
    again = call(prepared["mcp"], "plugin_prepare", {"instance": {}, "mode": "prepare", "force": True})
    assert again["data"]["prepared"]["status"] == "measured"
    assert prepared["bridge"].calls["format_params_normalized"] > before


def test_deep_mode_refines_disputed_boundaries(prepared):
    envelope = call(
        prepared["mcp"],
        "plugin_prepare",
        {"instance": {}, "mode": "deep", "grid_points": 9, "refine_budget": 40},
    )
    assert envelope["ok"] is True
    assert envelope["data"]["prepared"]["mode"] == "deep"
    assert prepared["bridge"].calls["format_params_normalized"] > 4 * 9


def test_include_controls_returns_the_detail_on_request(prepared):
    envelope = call(
        prepared["mcp"],
        "plugin_prepare",
        {"instance": {}, "mode": "prepare", "include_controls": True},
    )
    controls = envelope["data"]["controls"]
    assert len(controls) == 4
    assert {"index", "classification", "evidence"} <= set(controls[0])


def test_a_limited_force_run_still_does_not_replace_a_broader_artifact(prepared):
    """F-C4 (P27 review): `force` must not bypass the limited-run guard.

    Evaluated by the P27 independent (GLM-5.3 Flash) review: with `force` the
    stored candidate was never loaded, so a `force` + `sample_indices` run
    silently destroyed the full artifact while reporting `stored: true`.
    """
    first = call(prepared["mcp"], "plugin_prepare", {"instance": {}, "mode": "inventory"})
    store = PreparedStore(directory=prepared["directory"])
    stored_before = store.load(first["data"]["identity"])
    assert stored_before.ok

    limited = call(
        prepared["mcp"],
        "plugin_prepare",
        {
            "instance": {},
            "mode": "prepare",
            "sample_indices": [0, 1],
            "grid_points": 5,
            "force": True,
        },
    )
    data = limited["data"]
    assert data["prepared"]["status"] == "measured"
    assert data["prepared"]["stored"] is False
    assert (
        data["prepared"]["store_refusal"]["rule"]
        == "limited_run_would_replace_a_broader_artifact"
    )
    stored_after = store.load(data["identity"])
    assert stored_after.ok
    assert stored_after.document.get("content_sha256") == stored_before.document.get(
        "content_sha256"
    )


def test_an_unknown_mode_is_refused_with_the_accepted_vocabulary(prepared):
    envelope = call(prepared["mcp"], "plugin_prepare", {"instance": {}, "mode": "everything"})
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "InvalidValue"
    assert envelope["error"]["detail"]["accepted"] == ["reuse", "inventory", "prepare", "deep"]


def test_prepare_never_touches_the_profile_cache_directory(prepared):
    """P27: prepared discovery data must not depend on the *disposable* profile
    cache. `plugin_prepare` reports ``cache: "not_used"`` and the configured
    profile-cache directory is never created, read or written - which is the
    structural half of "clearing the cache does not destroy prepared data"."""
    cache_dir = prepared["config"].cache_dir
    assert cache_dir is not None and not Path(cache_dir).exists()
    for mode in ("inventory", "prepare", "reuse"):
        envelope = call(prepared["mcp"], "plugin_prepare", {"instance": {}, "mode": mode})
        assert envelope["ok"] is True
        assert envelope["meta"]["cache"] == "not_used"
    assert not Path(cache_dir).exists(), "prepare must not create the disposable cache directory"


def test_scenario_19_a_corrupt_artifact_is_reported_and_not_served(prepared):
    call(prepared["mcp"], "plugin_prepare", {"instance": {}, "mode": "prepare"})
    store = PreparedStore(directory=prepared["directory"])
    path = next(iter(store.paths()))
    document = json.loads(path.read_text(encoding="utf-8"))
    document["payload"]["counts"]["parameters"] = 99
    path.write_text(json.dumps(document), encoding="utf-8", newline="\n")

    envelope = call(prepared["mcp"], "plugin_prepare", {"instance": {}, "mode": "reuse"})
    assert envelope["ok"] is True
    previous = envelope["data"]["prepared"]["previous_artifact"]
    assert previous["status"] == "corrupt" and previous["rule"] == "content_sha256"
    assert envelope["data"]["prepared"]["status"] == "measured"
    # the corrupt bytes were replaced by a fresh measurement, and the fresh artifact verifies
    assert PreparedStore(directory=prepared["directory"]).load(
        envelope["data"]["identity"]
    ).ok


def test_a_stale_engine_generation_is_not_served(prepared):
    call(prepared["mcp"], "plugin_prepare", {"instance": {}, "mode": "prepare"})
    store = PreparedStore(directory=prepared["directory"])
    path = next(iter(store.paths()))
    document = json.loads(path.read_text(encoding="utf-8"))
    document["engine_version"] = "discovery-v0"
    path.write_text(json.dumps(document), encoding="utf-8", newline="\n")
    envelope = call(prepared["mcp"], "plugin_prepare", {"instance": {}, "mode": "reuse"})
    # the engine version is part of the artifact key, so a stale file is not even addressed
    assert envelope["data"]["prepared"]["status"] == "measured"


def test_addressing_a_missing_slot_is_a_typed_refusal(prepared):
    envelope = call(
        prepared["mcp"], "plugin_prepare", {"instance": {"selector": {"track_index": 7}}}
    )
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "InstanceNotFound"


# ---------------------------------------------------------------------------
# The bridge's metadata channels (conformance with the measured field layout)
# ---------------------------------------------------------------------------


class FakeReapyApi:
    """The host API surface with the tuples the live probes measured."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self.values = {"0:48": 0.25}

    # -- the measured shapes ------------------------------------------------

    def TrackFX_GetParamName(self, track: Any, fx: int, index: int, name: str, capacity: int) -> list:
        return [1, track, fx, index, f"param {index}", capacity]

    def TrackFX_GetParamIdent(self, track: Any, fx: int, index: int, value: str, capacity: int) -> list:
        return [1, track, fx, index, f"0:{index}", capacity]

    def TrackFX_GetParamNormalized(self, track: Any, fx: int, index: int) -> float:
        return 0.5

    def TrackFX_GetParameterStepSizes(
        self, track: Any, fx: int, index: int, step: float, small: float, large: float, toggle: bool
    ) -> list:
        # slot 0 = reported, 4 = step, 5 = small, 6 = large, 7 = is_toggle (P22 probe)
        return [1, track, fx, index, 0.125, 0.25, 0.5, True]

    def TrackFX_GetFormattedParamValue(self, track: Any, fx: int, index: int, value: str, capacity: int) -> list:
        return [1, track, fx, index, "12dB", capacity]

    def TrackFX_GetNamedConfigParm(self, track: Any, fx: int, key: str, value: str, capacity: int) -> list:
        self.calls.append(("named", (key,)))
        if key.endswith("default_value"):
            return [1, track, fx, key, "0.5", capacity]
        return [0, track, fx, key, "", capacity]

    def TrackFX_FormatParamValueNormalized(
        self, track: Any, fx: int, index: int, value: float, text: str, capacity: int
    ) -> list:
        # slot 5 carries the display text (P22/P23 probe)
        return [1, track, fx, index, value, f"{value:.3f} dB", capacity]


class FakeTrack:
    def __init__(self) -> None:
        self.index = 0


def bridge_with(api: FakeReapyApi) -> ReaperHostBridge:
    bridge = ReaperHostBridge(timeout=5.0)
    bridge._api = lambda: api  # type: ignore[method-assign]
    bridge._track = lambda R, index: FakeTrack()  # type: ignore[method-assign]
    bridge.ensure = lambda: None  # type: ignore[method-assign]
    return bridge


def test_bridge_reads_the_measured_step_size_layout():
    api = FakeReapyApi()
    bridge = bridge_with(api)
    payload = bridge.param_metadata(REF, [3], include=("steps", "name", "ident"))
    row = payload["rows"][0]
    assert row["steps"] == {
        "status": "ok",
        "reported": True,
        "step": 0.125,
        "small": 0.25,
        "large": 0.5,
        "is_toggle": True,
        "note": None,
    }
    assert row["name"] == "param 3" and row["ident"] == "0:3"


def test_bridge_reads_the_formatter_text_from_the_measured_slot():
    api = FakeReapyApi()
    bridge = bridge_with(api)
    payload = bridge.format_params_normalized(REF, [(0, 0.0), (0, 0.5), (0, 1.0)])
    assert payload["texts"] == ["0.000 dB", "0.500 dB", "1.000 dB"]
    assert payload["calls"] == 3
    assert payload["call"] == "TrackFX_FormatParamValueNormalized"

    single = bridge.param_metadata(REF, [0], include=("formatted",))
    assert single["rows"][0]["formatted"] == "12dB"


def test_bridge_records_a_named_config_key_the_plugin_declines():
    api = FakeReapyApi()
    bridge = bridge_with(api)
    payload = bridge.param_metadata(REF, [0], include=("named",))
    named = payload["rows"][0]["named_config"]
    assert named["status"] == "ok"
    assert named["entries"] == {"default_value": "0.5"}
    # only the key that can carry a value is probed by default: every probe is a host
    # call, and the measured cost of an unheld call is what narrowed this (P26 live)
    assert named["probed"] == ["param.0.default_value"]
    full = bridge.param_metadata(REF, [0], include=("named_full",))["rows"][0]["named_config"]
    assert "param.0.min" in full["probed"]
    assert len(full["probed"]) == 4


def test_bridge_reports_an_absent_section_channel_instead_of_an_empty_map():
    api = FakeReapyApi()
    bridge = bridge_with(api)
    payload = bridge.param_sections(REF, [0, 1])
    assert payload["status"] == "unsupported"
    assert "no parameter-section read" in payload["reason"]
    assert payload["rows"] == []


def test_bridge_reads_sections_when_the_build_has_the_function():
    api = FakeReapyApi()

    def sections(track: Any, fx: int, index: int, value: str, capacity: int) -> list:
        return [1, track, fx, index, f"Unit {index}", capacity]

    api.TrackFX_GetParamSectionName = sections  # type: ignore[attr-defined]
    bridge = bridge_with(api)
    payload = bridge.param_sections(REF, [0, 1])
    assert payload["status"] == "ok"
    assert [row["section"] for row in payload["rows"]] == ["Unit 0", "Unit 1"]

    combined = bridge.param_metadata(REF, [0], include=("name", "sections"))
    assert combined["rows"][0]["section"] == "Unit 0"
    assert combined["channels"]["sections"] == "ok"


def test_bridge_metadata_absence_is_per_channel_not_one_empty_string():
    api = FakeReapyApi()

    def no_step(*args: Any, **kwargs: Any) -> list:
        return [0, args[0], args[1], args[2], 0.0, 0.0, 0.0, False]

    api.TrackFX_GetParameterStepSizes = no_step  # type: ignore[attr-defined]
    bridge = bridge_with(api)
    row = bridge.param_metadata(REF, [0], include=("steps",))["rows"][0]
    assert row["steps"]["status"] == "absent"
    assert row["steps"]["reported"] is False
    assert "no step" in row["steps"]["note"]
