"""P28: the providerless path - describe, search, read and batch-write any FX.

The round's brief (§6, §7) requires that an FX **no provider claims** is served
from its prepared discovery artifact instead of being swept: a compact
description, a multi-term search that returns a handful of relevant controls, an
address-verified read, and a **batched** write that reads its results back.

The file is organised along the path itself:

- `TestIndex` - the artifact index: lookups, ranked multi-term search, bounded
  output, one build per artifact;
- `TestMaterialization` - artifact control -> profile control: the laws are
  *verified* against the artifact's own samples, and the three models (numeric,
  band map, integer grid) are each pinned by the case that decides them;
- `TestTools` - the four tools on the provider path's own request shape;
- `TestWrite` - the batch write: dry run, apply, read-back, restore, and the
  refusals (state/routes/unknown key/unverified law/moved index);
- `TestProviderPathUnchanged` - the same helpers on a provider profile, so the
  shared change cannot have altered the provider path's answers.

Everything here is offline. The live audit over a real 28 MB artifact is
`tests/plugins/live/probe_p28_materialization_audit.py`.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from reaper_reapy_mcp.plugins import api as A
from reaper_reapy_mcp.plugins import host_scope as HS
from reaper_reapy_mcp.plugins import tools as T
from reaper_reapy_mcp.plugins import values as V
from reaper_reapy_mcp.plugins.discovery.index import ControlIndex, index_census, load_index
from reaper_reapy_mcp.plugins.errors import (
    DiscoveryArtifactMissing,
    NoConversion,
    TargetChanged,
    UnsupportedProvider,
)

import discovery_double as D

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

INSTANCE = {"track_index": 0, "fx_index": 0}


def call(mcp: Any, name: str, arguments: dict | None = None) -> dict:
    blocks = asyncio.run(mcp.call_tool(name, arguments or {}))
    text = next(block.text for block in blocks if getattr(block, "text", None))
    return json.loads(text)


def _ladder(index: int, normalized: float) -> str:
    """A clean integer ladder: ``-3 + 20x`` rounded **half away from zero**.

    The rounding convention is not a detail: the value layer declares the grid it
    measured on a real plugin (``std::round``: half away from zero), and a display
    that rounds differently at the midpoints is a display no single grid can
    reproduce - which is exactly what makes the model *verified* rather than
    assumed. ``discovery_double.stepper_display`` uses Python's banker's rounding,
    so it is deliberately not used here.
    """
    import math

    value = -3.0 + 20.0 * float(normalized)
    snapped = math.floor(value + 0.5) if value >= 0.0 else math.ceil(value - 0.5)
    return f"{snapped:g}"


def build_bridge(*, refuse_writes: bool = False) -> D.FakeDiscoveryBridge:
    """A four-control synth: two numeric displays, a label enum and an integer ladder."""
    bridge = D.FakeDiscoveryBridge(
        [
            D.FakeControl(0, "Amp EG Attack", display=D.power_display(scale=4.0, exponent=2.0, unit=" s")),
            D.FakeControl(1, "Filter 1 Cutoff", display=D.linear_display(20.0, 20000.0, " Hz")),
            D.FakeControl(2, "Filter 1 Type", display=D.labels_display(["Off", "LP 24 dB", "HP 12 dB"])),
            D.FakeControl(3, "Octave", display=_ladder),
            D.FakeControl(4, "M1: -", display=lambda _i, _n: "0.000000"),
        ]
    )
    bridge.refuse_writes = refuse_writes
    return bridge


def prepared_env(tmp_path: Path, monkeypatch, *, bridge: D.FakeDiscoveryBridge | None = None):
    """A discovery directory holding a real artifact produced by the engine."""
    from reaper_reapy_mcp.plugins.discovery.pipeline import DiscoveryEngine, EngineConfig
    from reaper_reapy_mcp.plugins.discovery.store import PreparedStore
    from reaper_reapy_mcp.plugins.model import PluginInstanceRef
    from reaper_reapy_mcp.plugins.resolve import facts_location

    active = bridge or build_bridge()
    monkeypatch.setattr(A, "_BRIDGE", active)
    monkeypatch.setattr(A, "_REGISTRY", None)
    directory = tmp_path / "discovery"
    monkeypatch.setenv("REAPER_MCP_PLUGIN_DISCOVERY", str(directory))
    facts = active.fx_facts(0, 0)
    ref = PluginInstanceRef(
        host="reaper",
        location=facts_location(facts),
        provider="host-scope",
        identity=HS.HostScope().identify(facts),
        display="fake",
    )
    result = DiscoveryEngine(bridge=active).run(ref, facts, config=EngineConfig(mode="prepare", grid_points=17).resolved())
    PreparedStore(directory=directory).save(result, identity=ref.identity)
    return active, directory


@pytest.fixture
def providerless(tmp_path, monkeypatch):
    from mcp.server.fastmcp import FastMCP

    bridge, directory = prepared_env(tmp_path, monkeypatch)
    config = A.PluginConfig(provider_ids=("vital",), cache_dir=str(tmp_path / "cache"), env={})
    mcp = FastMCP("probe")
    T.setup_plugin_tools(mcp, config=config)
    return {"bridge": bridge, "mcp": mcp, "config": config, "directory": directory, "tmp": tmp_path}


# ---------------------------------------------------------------------------
# The index
# ---------------------------------------------------------------------------


def _payload() -> dict:
    return {
        "identity": {"fx_name": "Fake", "class_id": "x", "module": "m", "param_count": 4},
        "mode": "prepare",
        "engine_version": "discovery-v1",
        "counts": {"controls_analyzed": 4},
        "cost": {"total_calls": 0},
        "modules": {
            "nodes": [
                {"id": "osc", "label": "OSC", "kind": "section", "status": "confirmed", "confidence": "strong", "controls": [0, 1]},
                {"id": "filter", "label": "FILTER", "kind": "section", "status": "confirmed", "confidence": "strong", "controls": [2]},
            ],
            "signals_used": ["name"],
        },
        "controls": [
            {
                "index": 0,
                "name": "Osc 1 Pitch",
                "ident": "0:11",
                "module": "osc",
                "classification": {"kind": "ramp_like", "confidence": "medium", "reasons": ["ramp"]},
                "range": {"chosen": [-24.0, 24.0], "space": "display"},
                "mapping": {"chosen": {"kind": "affine", "accepted": True, "params": {"scale": 48.0, "offset": -24.0}}},
                "discrete": {"family": "unknown", "members": []},
                "steps": {"reported": False},
            },
            {
                "index": 1,
                "name": "Filter 1 Cutoff",
                "ident": "0:12",
                "module": "filter",
                "classification": {"kind": "ramp_like", "confidence": "medium", "reasons": ["ramp"]},
                "range": {"chosen": [20.0, 20000.0], "space": "display"},
                "mapping": {"chosen": {"kind": "affine", "accepted": True, "params": {"scale": 19980.0, "offset": 20.0}}},
                "discrete": {"family": "unknown", "members": []},
                "steps": {"reported": False},
            },
            {
                "index": 2,
                "name": "Filter 1 Type",
                "ident": "0:13",
                "module": "filter",
                "classification": {"kind": "enum", "confidence": "medium", "reasons": ["labels"]},
                "range": {"reason": "api_does_not_expose"},
                "mapping": {"fits": []},
                "discrete": {
                    "family": "enum",
                    "state": "sampled_upper_bounds",
                    "members": [
                        {"ordinal": 0, "label": "Off", "band": [0.0, 0.5]},
                        {"ordinal": 1, "label": "LP 24 dB", "band": [0.5, 1.0]},
                    ],
                },
                "steps": {"reported": False},
            },
            {
                "index": 3,
                "name": "M1: -",
                "ident": "0:14",
                "module": "osc",
                "classification": {"kind": "unknown", "confidence": "none", "reasons": ["constant"]},
                "mapping": {"fits": [], "reason": "formatter_constant"},
                "discrete": {"family": "unknown", "members": []},
                "steps": {"reported": False},
            },
        ],
    }


def test_index_builds_lookups_from_one_pass():
    index = ControlIndex(_payload())
    assert [control.index for control in index.controls] == [0, 1, 2, 3]
    assert index.control("#2").index == 2
    assert index.control("2").index == 2
    assert index.control("Osc 1 Pitch").index == 0
    assert index.control("0:12").index == 1
    assert index.census()["controls_indexed"] == 4
    assert index.census()["modules"] == 2


def test_index_search_is_multi_term_and_explains_every_match():
    index = ControlIndex(_payload())
    found = index.find(["oscillator", "pitch", "cutoff"], limit=8)
    ids = [row["id"] for row in found["matches"]]
    assert ids[0] == "osc_1_pitch", "two terms beat one"
    assert "filter_1_cutoff" in ids
    assert found["n_matches"] >= 2
    first = found["matches"][0]
    assert set(first["matched_terms"]) == {"oscillator", "pitch"}
    assert any("role" in reason for reason in first["why"]), "the synonym hit is labelled as one"
    assert found["unmatched_terms"] == []


def test_index_search_reports_an_unmatched_term_instead_of_dropping_it():
    index = ControlIndex(_payload())
    found = index.find(["cutoff", "zzzz"], limit=4)
    assert found["unmatched_terms"] == ["zzzz"]
    assert found["matches"], "the term that did match still answers"
    assert found["suggestions"]["zzzz"] == [] or isinstance(found["suggestions"]["zzzz"], list)


def test_index_search_is_bounded_and_says_so():
    index = ControlIndex(_payload())
    found = index.find(["filter"], limit=1)
    assert len(found["matches"]) == 1
    assert found["truncated"] is True
    assert found["n_matches"] >= 1


def test_index_serves_module_membership_and_unknown_module_refuses():
    index = ControlIndex(_payload())
    module = index.module("osc")
    assert [control.index for control in module["controls"]] == [0, 3]
    assert index.module("nope") is None


def test_the_same_artifact_is_indexed_once():
    payload = _payload()
    before = index_census()["indexes_built"]
    first = load_index(payload, cache_key=("memory:test-key", "digest-1"))
    second = load_index(payload, cache_key=("memory:test-key", "digest-1"))
    assert first is second
    assert index_census()["indexes_built"] == before + 1


def test_a_changed_digest_is_a_different_index():
    payload = _payload()
    first = load_index(payload, cache_key=("memory:test-key", "digest-a"))
    second = load_index(payload, cache_key=("memory:test-key", "digest-b"))
    assert first is not second, "a re-measured artifact must never be served a stale view"


# ---------------------------------------------------------------------------
# Materialization
# ---------------------------------------------------------------------------


def _materialize(bridge: D.FakeDiscoveryBridge) -> tuple[dict, dict]:
    import hashlib

    from reaper_reapy_mcp.plugins.discovery.pipeline import DiscoveryEngine, EngineConfig
    from reaper_reapy_mcp.plugins.discovery.store import PreparedStore
    from reaper_reapy_mcp.plugins.model import PluginInstanceRef

    facts = bridge.fx_facts(0, 0)
    ref = PluginInstanceRef(
        host="reaper",
        location={"track_index": 0, "fx_index": 0},
        provider="host-scope",
        identity=HS.HostScope().identify(facts),
        display="fake",
    )
    result = DiscoveryEngine(bridge=bridge).run(
        ref, facts, config=EngineConfig(mode="prepare", grid_points=17).resolved()
    )
    payload = result.to_dict(with_observations=False, with_inventory=True)
    observations = [item.to_dict() for item in result.observations]
    # A REAL content digest: the materialization is cached per (path, digest), so a
    # placeholder would let one fixture's profile answer for the next one. The key
    # is what makes "the view matches the artifact it names" true.
    digest = hashlib.sha256(
        json.dumps({"payload": payload, "observations": observations}, sort_keys=True).encode("utf-8")
    ).hexdigest()
    store = PreparedStore(directory=Path(bridge.__dict__.get("_dir", ".")) / "unused")
    artifact = store._validate(  # noqa: SLF001 - the validated document, without a file
        {
            "kind": "fx-discovery",
            "schema_version": 1,
            "engine_version": result.engine_version,
            "mode": result.mode,
            "identity": dict(ref.identity),
            "payload": payload,
            "observations": observations,
            "content_sha256": digest,
        },
        identity=ref.identity,
        path=Path("memory"),
        engine_version=result.engine_version,
    )
    artifact.document["content_sha256"] = artifact.document.get("content_sha256")
    profile = HS.materialize_profile(artifact)
    controls = {control["id"]: control for control in profile["controls"]}
    return profile, controls


def test_numeric_laws_are_verified_against_the_artifacts_own_samples():
    bridge = build_bridge()
    profile, controls = _materialize(bridge)
    attack = controls["amp_eg_attack"]
    assert attack["type"] == "number"
    assert attack["evidence"]["model"] == "numeric_law"
    assert attack["law"]["kind"] == "power"
    evidence = attack["evidence"]["law_evidence"]
    assert evidence["ok"] is True and evidence["tolerance"] > 0
    assert evidence["worst_read_error"] <= evidence["tolerance"]
    assert attack["evidence"]["native_space"] == "display"


def test_a_labelled_control_becomes_a_band_map_with_unique_member_ids():
    bridge = build_bridge()
    _profile, controls = _materialize(bridge)
    kind = controls["filter_1_type"]
    assert kind["type"] == "enum"
    assert kind["evidence"]["model"] == "band_map"
    assert [member["id"] for member in kind["enum_values"]] == ["Off", "LP 24 dB", "HP 12 dB"]
    law = kind["law"]
    assert law["kind"] == "enum_index"
    assert law["params"]["switch_points"][1] > 0.25, "the measured band edge, not 1/3 assumed"
    assert law["params"]["band_source"] == "sampled_grid"
    assert V.normalized_to_native(kind, 0.9) == 2.0
    assert V.from_native(kind, V.normalized_to_native(kind, 0.9)) == "HP 12 dB"


def test_an_integer_display_gets_the_measured_grid_not_a_continuum():
    bridge = build_bridge()
    _profile, controls = _materialize(bridge)
    octave = controls["octave"]
    assert octave["evidence"]["model"] == "quantised_numeric"
    grid = octave["law"]["params"]["quantization"]
    assert grid["step"] == 1.0 and grid["rounding"] == "half_away_from_zero"
    assert "grid_source" in octave["evidence"]["law_evidence"]
    # the grid snaps both directions, which is what makes every printed state reachable
    assert V.normalized_to_native(octave, 0.0) == -3.0
    assert V.normalized_to_native(octave, 0.0625) == -2.0
    assert V.to_native(octave, 1.0) == 1.0
    assert V.to_native(octave, 1.4) == 1.0


def test_a_display_with_no_closed_form_keeps_the_measured_table():
    """The measured real case: REAPER's own log frequency mapping (20 Hz..24 kHz)
    fits no closed form the law vocabulary can express - the discovery engine
    itself chose a `table` - so the samples ARE the law, and both directions
    interpolate. Pinned here on a *shifted* exponential, which no affine/power/
    exponential law reproduces: an unshifted exponential law would look plausible
    and be wrong by a factor of ~1.7 at the midpoint."""
    import math

    def shifted_exponential(index: int, normalized: float) -> str:
        value = 59.95032740450714 * math.exp(5.993955874104003 * float(normalized)) - 39.947790136928695
        return f"{value:.2f} Hz"

    bridge = D.FakeDiscoveryBridge([D.FakeControl(0, "Freq-Low Shelf", display=shifted_exponential)])
    profile, controls = _materialize(bridge)
    freq = controls["freq_low_shelf"]
    assert freq["evidence"]["model"] == "table_law", freq["evidence"]
    assert freq["law"]["kind"] == "table"
    assert len(freq["law"]["params"]["points"]) == 17
    assert "table_note" in freq["evidence"]["law_evidence"]
    # read: the endpoints are the plugin's own printed extremes
    assert V.normalized_to_native(freq, 0.0) == pytest.approx(20.0, abs=0.01)
    assert V.normalized_to_native(freq, 1.0) == pytest.approx(24000.0, abs=1.0)
    # write: a musical target inverts to a position that reads back as itself
    position = V.to_normalized(freq, 1000.0)
    assert 0.0 < position < 1.0
    assert V.from_native(freq, V.normalized_to_native(freq, position)) == pytest.approx(1000.0, abs=0.5)
    # and the range is the value space the write is validated against
    assert freq["range"]["min"] <= 20.0 and freq["range"]["max"] >= 24000.0


def test_a_table_law_that_is_not_invertible_is_refused():
    """A malformed table is ignored (like a malformed grid), never approximated."""
    broken = {
        "id": "x",
        "type": "number",
        "range": {"min": 0.0, "max": 1.0},
        "law": {"kind": "table", "params": {"points": [[0.0, 0.0], [0.5, 1.0], [1.0, 0.5]]}},
    }
    with pytest.raises(NoConversion):
        V.normalized_to_native(broken, 0.5)
    with pytest.raises(NoConversion):
        V.to_normalized(broken, 0.5)
    too_short = {
        "id": "x",
        "type": "number",
        "range": {"min": 0.0, "max": 1.0},
        "law": {"kind": "table", "params": {"points": [[0.0, 0.0], [1.0, 1.0]]}},
    }
    with pytest.raises(NoConversion):
        V.normalized_to_native(too_short, 0.5)


def test_a_control_the_host_prints_a_constant_for_has_no_law_and_refuses_conversion():
    bridge = build_bridge()
    _profile, controls = _materialize(bridge)
    constant = controls["m1"]
    assert constant["law"]["kind"] == "none"
    assert constant["evidence"]["law_reason"] == "formatter_constant"
    assert constant["evidence"]["strength"] == "weak"
    with pytest.raises(NoConversion):
        V.to_native(constant, 0.0)


def test_a_display_with_no_closed_form_keeps_the_measured_table():
    """The measured real case: REAPER's own log frequency mapping (20 Hz..24 kHz)
    fits no closed form the law vocabulary can express - the discovery engine
    itself chose a `table` - so the samples ARE the law, and both directions
    interpolate. Pinned here on a *shifted* exponential, which no affine/power/
    exponential law reproduces: an unshifted exponential law looks plausible and
    is wrong by a factor of ~1.7 at the midpoint."""
    import math

    def shifted_exponential(index: int, normalized: float) -> str:
        value = 59.95032740450714 * math.exp(5.993955874104003 * float(normalized)) - 39.947790136928695
        return f"{value:.2f} Hz"

    bridge = D.FakeDiscoveryBridge([D.FakeControl(0, "Freq-Low Shelf", display=shifted_exponential)])
    profile, controls = _materialize(bridge)
    freq = controls["freq_low_shelf"]
    assert freq["evidence"]["model"] == "table_law", freq["evidence"]
    assert freq["law"]["kind"] == "table"
    assert len(freq["law"]["params"]["points"]) == 17
    assert "table_note" in freq["evidence"]["law_evidence"]
    # read: the endpoints are the plugin's own printed extremes
    assert V.normalized_to_native(freq, 0.0) == pytest.approx(20.0, abs=0.01)
    assert V.normalized_to_native(freq, 1.0) == pytest.approx(24000.0, abs=1.0)
    # write: a musical target inverts to a position that reads back as itself
    position = V.to_normalized(freq, 1000.0)
    assert 0.0 < position < 1.0
    assert V.from_native(freq, V.normalized_to_native(freq, position)) == pytest.approx(1000.0, abs=0.5)
    assert freq["range"]["min"] <= 20.0 and freq["range"]["max"] >= 24000.0


def test_a_table_law_that_is_not_invertible_is_refused():
    """A malformed table is ignored (like a malformed grid), never approximated."""
    broken = {
        "id": "x",
        "type": "number",
        "range": {"min": 0.0, "max": 1.0},
        "law": {"kind": "table", "params": {"points": [[0.0, 0.0], [0.5, 1.0], [1.0, 0.5]]}},
    }
    with pytest.raises(NoConversion):
        V.normalized_to_native(broken, 0.5)
    with pytest.raises(NoConversion):
        V.to_normalized(broken, 0.5)
    too_short = {
        "id": "x",
        "type": "number",
        "range": {"min": 0.0, "max": 1.0},
        "law": {"kind": "table", "params": {"points": [[0.0, 0.0], [1.0, 1.0]]}},
    }
    with pytest.raises(NoConversion):
        V.normalized_to_native(too_short, 0.5)


def test_every_materialized_control_declares_its_address_and_provenance():
    bridge = build_bridge()
    profile, controls = _materialize(bridge)
    assert len(controls) == 5
    for control in controls.values():
        assert isinstance(control["binding"]["index"], int)
        assert control["binding"]["live_name"]
        assert control["authority"] == {"read": "parameter", "write": "parameter"}
        assert control["evidence"]["source"].startswith("discovery-artifact:")
    assert profile["state"]["codec"] is None
    assert profile["modulation"]["supported"] is False
    assert profile["limits"]


def test_two_controls_with_the_same_name_stay_addressable():
    """A plugin can name two controls the same - ReaComp has two `Attack`s
    (measured live, P28 stage G). An id that names two targets is a wrong-write
    hazard, and the first version of this layer turned the collision into a bare
    `assert` inside the tool, i.e. an `InternalError` with an empty message while
    one of the two controls was silently unaddressable."""
    from reaper_reapy_mcp.plugins.discovery.store import PreparedArtifact

    def _control(index: int) -> dict:
        return {
            "index": index,
            "name": "Attack",
            "ident": f"0:{index}",
            "module": "attack",
            "classification": {"kind": "ramp_like", "confidence": "medium", "reasons": ["ramp"]},
            "range": {"chosen": [0.0, 1.0], "space": "display"},
            "mapping": {"chosen": {"kind": "affine", "accepted": True, "params": {"scale": 1.0, "offset": 0.0}}},
            "discrete": {"family": "unknown", "members": []},
            "steps": {"reported": False},
        }

    payload = {
        "identity": {"fx_name": "Fake", "class_id": "x", "module": "m", "param_count": 10},
        "mode": "prepare",
        "counts": {"controls_analyzed": 2},
        "cost": {"total_calls": 0},
        "modules": {"nodes": [], "signals_used": ["name"]},
        "controls": [_control(4), _control(9)],
    }
    index = load_index(payload)
    index_ids = [control.control_id for control in index.controls]
    assert index_ids == ["attack #4", "attack #9"], index_ids
    assert index.census()["disambiguated_ids"] == ["attack"]
    assert index.control("attack #4").index == 4
    assert index.control("attack #9").index == 9

    artifact = PreparedArtifact(
        document={
            "kind": "fx-discovery",
            "schema_version": 1,
            "engine_version": "discovery-v1",
            "mode": "prepare",
            "identity": payload["identity"],
            "payload": payload,
            "observations": [],
            "content_sha256": "dup0123",
        },
        status="ok",
        path="memory:duplicate-names",
    )
    profile = HS.materialize_profile(artifact)
    ids = [str(control["id"]) for control in profile["controls"]]
    assert ids == ["attack #4", "attack #9"], ids
    assert ids == index_ids, "a search row's id and the write path's id are the same id"
    assert profile["counts"]["disambiguated_ids"] == ["attack"]
    # the write path resolves a request key through this lookup, so the two must
    # not be interchangeable
    assert HS._control_by_name(profile["controls"], "attack #9")["binding"]["index"] == 9
    assert HS._control_by_name(profile["controls"], "attack #4")["binding"]["index"] == 4


def test_the_index_search_row_carries_the_address_its_write_uses(providerless):
    """Measured live (P28 stage G): the index path emitted the address ONLY inside
    `binding`, so a caller reading `match.index` - the key `plugin_write`'s preview
    and verification rows use - had no address to write with."""
    envelope = call(
        providerless["mcp"], "plugin_find", {"instance": dict(INSTANCE), "query": "octave", "limit": 5}
    )
    assert envelope["ok"] is True, envelope
    rows = envelope["data"]["matches"]
    assert rows, envelope
    for row in rows:
        assert isinstance(row["index"], int)
        assert row["index"] == row["binding"]["index"]
        assert row["name"]


def test_materialization_is_cached_per_artifact():
    from reaper_reapy_mcp.plugins.discovery.store import PreparedArtifact

    document = {
        "kind": "fx-discovery",
        "schema_version": 1,
        "engine_version": "discovery-v1",
        "mode": "prepare",
        "identity": {"fx_name": "Fake", "class_id": "x", "module": "m", "param_count": 1},
        "payload": {"controls": [{"index": 0, "name": "a", "ident": "0:1", "classification": {"kind": "unknown"}}]},
        "observations": [],
        "content_sha256": "deadbeef",
    }
    artifact = PreparedArtifact(document=document, status="ok", path="memory:stable")
    first = HS.materialize_profile(artifact)
    before = HS.materialization_stats()["profiles_built"]
    second = HS.materialize_profile(artifact)
    assert first is second
    assert HS.materialization_stats()["profiles_built"] == before


# ---------------------------------------------------------------------------
# The tools, on the providerless path
# ---------------------------------------------------------------------------


def test_describe_is_compact_and_names_the_artifact_it_came_from(providerless):
    envelope = call(providerless["mcp"], "plugin_describe", {"instance": dict(INSTANCE)})
    assert envelope["ok"] is True, envelope
    data = envelope["data"]
    assert envelope["meta"]["profile_source"] == "discovery"
    assert data["profile"]["source"] == "discovery"
    assert data["counts"]["controls"] == 5
    assert data["counts"]["writable"] == 4
    assert {row["id"] for row in data["modules"]} >= {"amp", "filter 1", "octave"}
    assert "controls" not in data, "the default description must stay compact"
    assert data["discovery"]["mode"] == "prepare"
    assert data["discovery"]["writable_controls"] == 4
    assert data["state"]["codec"] is None
    assert data["routes"]["supported"] is False
    assert data["limits"]


def test_find_answers_a_multi_function_query_in_one_call(providerless):
    envelope = call(
        providerless["mcp"],
        "plugin_find",
        {"instance": dict(INSTANCE), "query": "oscillator pitch filter cutoff", "limit": 4},
    )
    data = envelope["data"]
    assert data["n_matches"] >= 2
    assert data["returned"] <= 4 and data["truncated"] in (True, False)
    ids = [row["id"] for row in data["matches"]]
    assert "filter_1_cutoff" in ids
    top = data["matches"][0]
    assert top["why"] and top["matched_terms"]
    assert envelope["meta"]["profile_source"] == "discovery"


def test_find_by_module_and_by_index(providerless):
    by_module = call(
        providerless["mcp"], "plugin_find", {"instance": dict(INSTANCE), "group": "octave"}
    )
    assert [row["id"] for row in by_module["data"]["matches"]] == ["octave"]

    unknown = call(
        providerless["mcp"], "plugin_find", {"instance": dict(INSTANCE), "group": "nope"}
    )
    assert unknown["ok"] is False and unknown["error"]["code"] == "InvalidValue"
    assert "filter 1" in unknown["error"]["detail"]["groups"]

    by_index = call(
        providerless["mcp"], "plugin_find", {"instance": dict(INSTANCE), "query": "#1"}
    )
    assert [row["id"] for row in by_index["data"]["matches"]] == ["filter_1_cutoff"]


def test_read_returns_the_hosts_own_text_beside_the_law_conversion(providerless):
    before = dict(providerless["bridge"].calls)
    envelope = call(
        providerless["mcp"],
        "plugin_read",
        {"instance": dict(INSTANCE), "controls": ["filter_1_cutoff", "octave"]},
    )
    assert envelope["ok"] is True, envelope
    values = envelope["data"]["values"]
    assert values["filter_1_cutoff"]["normalized"] == 0.0
    assert values["filter_1_cutoff"]["host_display"] == "20.00 Hz"
    assert values["filter_1_cutoff"]["display_source"] == "host_formatter"
    assert values["octave"]["host_display"] == "-3"
    assert values["octave"]["native"] == -3.0, "the grid lands on the printed state"
    calls = providerless["bridge"].calls
    assert calls["param_names"] == before["param_names"] + 1, "the batch is one hop"
    assert calls["read_params"] == before["read_params"] + 1
    assert calls["format_params_normalized"] == before["format_params_normalized"] + 1


def test_read_refuses_an_unknown_name_with_suggestions(providerless):
    envelope = call(
        providerless["mcp"], "plugin_read", {"instance": dict(INSTANCE), "controls": ["cutof"]}
    )
    problems = envelope["data"]["problems"]
    assert problems and problems[0]["code"] == "UnsupportedControl"
    assert problems[0]["detail"]["suggestions"]


def test_a_slot_with_no_artifact_refuses_and_never_measures(tmp_path, monkeypatch):
    from mcp.server.fastmcp import FastMCP

    bridge = build_bridge()
    monkeypatch.setattr(A, "_BRIDGE", bridge)
    monkeypatch.setattr(A, "_REGISTRY", None)
    monkeypatch.setenv("REAPER_MCP_PLUGIN_DISCOVERY", str(tmp_path / "empty"))
    mcp = FastMCP("probe")
    T.setup_plugin_tools(mcp, config=A.PluginConfig(provider_ids=("vital",), env={}))
    for tool, arguments in (
        ("plugin_describe", {"instance": dict(INSTANCE)}),
        ("plugin_find", {"instance": dict(INSTANCE), "query": "cutoff"}),
        ("plugin_read", {"instance": dict(INSTANCE), "controls": ["filter_1_cutoff"]}),
        (
            "plugin_write",
            {"instance": dict(INSTANCE), "request": {"controls": {"filter_1_cutoff": 100.0}}},
        ),
    ):
        envelope = call(mcp, tool, arguments)
        assert envelope["ok"] is False, tool
        assert envelope["error"]["code"] == "DiscoveryArtifactMissing", (tool, envelope)
        assert envelope["error"]["detail"]["remedy"].startswith("call plugin_prepare")
    assert bridge.calls["param_metadata"] == 0
    assert bridge.index_write_rows == []
    assert not list((tmp_path / "empty").glob("discovery_*.json"))


def test_state_and_modulation_channels_are_refused_with_the_provider_reason(providerless):
    state = call(providerless["mcp"], "plugin_state", {"instance": dict(INSTANCE)})
    assert state["ok"] is False
    assert state["error"]["code"] == "UnsupportedProvider"
    assert state["error"]["detail"]["channel"] == "state/document"

    modulation = call(
        providerless["mcp"], "plugin_modulation", {"instance": dict(INSTANCE), "op": "list"}
    )
    assert modulation["ok"] is False
    assert modulation["error"]["detail"]["channel"] == "routes/modulation"


def test_a_named_provider_keeps_its_own_refusal(providerless):
    envelope = call(
        providerless["mcp"],
        "plugin_describe",
        {"instance": {**INSTANCE, "provider": "vital"}},
    )
    assert envelope["ok"] is False
    assert envelope["error"]["code"] in ("UnsupportedProvider", "InstanceNotFound")


def test_instances_reports_a_providerless_slot_separately(providerless):
    envelope = call(providerless["mcp"], "plugin_instances", {"selector": dict(INSTANCE)})
    assert envelope["ok"] is True, envelope
    assert envelope["data"]["instances"] == [], "no configured provider claims this slot"
    rows = envelope["data"]["host_scope"]
    assert len(rows) == 1
    row = rows[0]
    assert row["provider"] == "host-scope" and row["ready"] is True
    assert row["artifact"]["mode"] == "prepare"
    assert row["identity"]["fx_name"] == "Fake Synth"


# ---------------------------------------------------------------------------
# The batch write
# ---------------------------------------------------------------------------


def test_write_dry_run_plans_the_batch_and_writes_nothing(providerless):
    envelope = call(
        providerless["mcp"],
        "plugin_write",
        {
            "instance": dict(INSTANCE),
            "request": {"controls": {"filter_1_cutoff": 100.0, "octave": 2}},
        },
    )
    assert envelope["ok"] is True, envelope
    data = envelope["data"]
    assert data["dry_run"] is True and data["would_write"] is True
    assert data["plan"]["n_items"] == 2
    assert data["plan"]["write_order"] == ["parameters", "verify"]
    assert data["preview"]["parameter_address_space"] == "index"
    assert [row["index"] for row in data["preview"]["parameters"]] == [1, 3]
    assert envelope["outcome"]["host"]["write_calls_attempted"] == 0
    assert providerless["bridge"].index_write_rows == []


def test_write_applies_one_batch_and_reads_every_control_back(providerless):
    envelope = call(
        providerless["mcp"],
        "plugin_write",
        {
            "instance": dict(INSTANCE),
            "request": {"controls": {"filter_1_cutoff": 100.0, "octave": 2}},
            "dry_run": False,
        },
    )
    assert envelope["ok"] is True, envelope
    assert providerless["bridge"].calls["write_params_by_index"] == 1, "ONE batched write"
    written = {index: value for index, _name, value in providerless["bridge"].index_write_rows}
    assert [name for _i, name, _v in providerless["bridge"].index_write_rows] == [
        "Filter 1 Cutoff",
        "Octave",
    ]
    assert written[1] == pytest.approx((100.0 - 20.0) / (20000.0 - 20.0), abs=1e-9)
    assert written[3] == pytest.approx(0.25, abs=1e-9)

    channels = envelope["outcome"]["channels"]
    assert set(channels) == {"parameters"}, "the state channel is out of scope and not claimed"
    rows = channels["parameters"]["rows"]
    assert [row["address"]["index"] for row in rows] == [1, 3]
    assert {row["space"] for row in rows} == {"normalized"}
    assert all(row["agrees"] for row in rows)
    assert envelope["outcome"]["verdict"] == "ALL_VERIFIED"
    assert envelope["outcome"]["host"]["write_calls_attempted"] == 1
    assert envelope["outcome"]["host"]["write_effects_verified"] == 1
    gap = next(
        row for row in envelope["outcome"]["named_gaps"] if row["reason"] == "channel_not_in_scope"
    )
    assert gap["channel"] == "state"

    checks = envelope["data"]["display_checks"]
    assert checks["n_rows"] == 2
    assert all(row["agrees"] is not False for row in checks["rows"])
    assert envelope["data"]["preflight"]["binding"]["method"] == "index_name_guarded_in_write_hop"
    assert envelope["data"]["preflight"]["channels_in_scope"] == ["parameters"]


def test_write_verifies_in_the_space_it_wrote_and_reports_the_display(providerless):
    envelope = call(
        providerless["mcp"],
        "plugin_write",
        {
            "instance": dict(INSTANCE),
            "request": {"controls": {"filter_1_type": "LP 24 dB"}},
            "dry_run": False,
        },
    )
    assert envelope["ok"] is True, envelope
    row = envelope["outcome"]["channels"]["parameters"]["rows"][0]
    assert row["space"] == "display"
    assert row["display_expected"] == "LP 24 dB"
    assert row["display_read_back"] == "LP 24 dB"


def test_a_write_that_does_not_land_is_restored_from_the_capture(tmp_path, monkeypatch):
    from mcp.server.fastmcp import FastMCP

    bridge, _directory = prepared_env(tmp_path, monkeypatch, bridge=build_bridge())
    bridge.values[1] = 0.25  # the value the capture must restore
    bridge.refuse_writes = True
    config = A.PluginConfig(provider_ids=("vital",), cache_dir=str(tmp_path / "cache"), env={})
    mcp = FastMCP("probe")
    T.setup_plugin_tools(mcp, config=config)

    envelope = call(
        mcp,
        "plugin_write",
        {
            "instance": dict(INSTANCE),
            "request": {"controls": {"filter_1_cutoff": 100.0}},
            "dry_run": False,
        },
    )
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "ReadbackMismatch"
    rollback = envelope["outcome"]["rollback"]
    assert rollback["channels"] == {"parameters": True}
    assert rollback["verified"] is True
    assert set(envelope["outcome"]["channels"]) == {"parameters"}
    assert bridge.values[1] == 0.25, "the captured value is what the rollback wrote"


def test_a_moved_index_refuses_before_the_setter_runs(providerless):
    providerless["bridge"].name_shift[1] = "Filter 1 Resonance"
    envelope = call(
        providerless["mcp"],
        "plugin_write",
        {
            "instance": dict(INSTANCE),
            "request": {"controls": {"filter_1_cutoff": 100.0}},
            "dry_run": False,
        },
    )
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "TargetChanged"
    assert envelope["error"]["detail"]["observed_name"] == "Filter 1 Resonance"
    assert envelope["error"]["detail"]["writes_issued"] == 0
    # The verb certified that no setter ran, so the accounting says exactly that:
    # the refusal happened before any write, and no restore is attempted.
    assert envelope["outcome"]["verdict"] == "REFUSED_BEFORE_ANY_WRITE"
    assert envelope["outcome"]["host"]["write_calls_attempted"] == 0
    assert envelope["outcome"]["host"]["write_effects_unknown"] == 0
    assert envelope["outcome"]["rollback"]["attempted"] is False
    assert providerless["bridge"].values[1] == 0.0, "nothing was written to the moved index"


def test_state_routes_and_unknown_request_keys_are_refused_before_any_write(providerless):
    for request, channel in (
        ({"controls": {"octave": 1}, "state": {"a": 1}}, "state"),
        ({"controls": {"octave": 1}, "routes_create": [{"source": "x", "destination": "y"}]}, "routes_create"),
        ({"controls": {"octave": 1}, "bogus": 1}, "bogus"),
    ):
        envelope = call(
            providerless["mcp"],
            "plugin_write",
            {"instance": dict(INSTANCE), "request": request, "dry_run": False},
        )
        assert envelope["ok"] is False, request
        assert envelope["outcome"]["host"]["write_calls_attempted"] == 0
        assert providerless["bridge"].index_write_rows == []


def test_a_control_without_a_verified_law_is_refused_for_writing(providerless):
    envelope = call(
        providerless["mcp"],
        "plugin_write",
        {"instance": dict(INSTANCE), "request": {"controls": {"m1": 0.0}}, "dry_run": False},
    )
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "NoConversion"
    assert providerless["bridge"].index_write_rows == []


def test_write_refuses_a_token_that_claims_instance_continuity(providerless):
    described = call(providerless["mcp"], "plugin_describe", {"instance": dict(INSTANCE)})
    token = described["meta"]["target_token"]
    envelope = call(
        providerless["mcp"],
        "plugin_write",
        {
            "instance": {**INSTANCE, "target_token": token},
            "request": {"controls": {"octave": 1}},
            "dry_run": False,
        },
    )
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "IdentityContinuityUnprovable"
    assert providerless["bridge"].index_write_rows == []


def test_write_refuses_an_instance_whose_identity_claim_disagrees(providerless):
    envelope = call(
        providerless["mcp"],
        "plugin_write",
        {
            "instance": {**INSTANCE, "identity": {"class_id": "not-the-live-one"}},
            "request": {"controls": {"octave": 1}},
            "dry_run": False,
        },
    )
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "TargetChanged"
    assert providerless["bridge"].index_write_rows == []


def test_one_refused_control_refuses_the_whole_batch_before_any_write(providerless):
    """GLM review Q2: a batch that names an unusable control must not write the
    others first. `normalize_request` refuses the request as a whole, and the
    refusal names every problem - so a caller never has to reason about a
    half-applied batch."""
    envelope = call(
        providerless["mcp"],
        "plugin_write",
        {
            "instance": dict(INSTANCE),
            "request": {
                "controls": {"filter_1_cutoff": 100.0, "octave": 2, "m1": 0.0},
            },
            "dry_run": False,
        },
    )
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "NoConversion"
    problems = envelope["error"]["detail"]["problems"]
    assert [row["control"] for row in problems] == ["m1"]
    assert envelope["outcome"]["host"]["write_calls_attempted"] == 0
    assert providerless["bridge"].index_write_rows == [], "not one setter ran"
    assert providerless["bridge"].values[1] == 0.0 and providerless["bridge"].values[3] == 0.0


def test_a_snapped_target_is_visible_in_the_preview(providerless):
    """GLM review Q2 (clamping): a grid law snaps 3.4 to 3, and the preview says
    so instead of reporting the requested number as if it were stored."""
    envelope = call(
        providerless["mcp"],
        "plugin_write",
        {"instance": dict(INSTANCE), "request": {"controls": {"octave": 3.4}}},
    )
    assert envelope["ok"] is True, envelope
    row = envelope["data"]["preview"]["parameters"][0]
    assert row["index"] == 3
    assert row["stored_display"] == 3.0, "the snapped value is what the plugin will hold"
    assert envelope["data"]["plan"]["items"][0]["to"] == 3.4, "the request itself is reported too"


def test_the_second_search_call_rebuilds_nothing(providerless):
    before = providerless["bridge"].calls["param_metadata"]
    first = call(providerless["mcp"], "plugin_find", {"instance": dict(INSTANCE), "query": "cutoff"})
    built = first["meta"]["materialization"]["profiles_built"]
    second = call(providerless["mcp"], "plugin_find", {"instance": dict(INSTANCE), "query": "cutoff"})
    assert second["meta"]["materialization"]["profiles_built"] == built
    assert second["meta"]["materialization"]["profile_hits"] >= 1
    assert providerless["bridge"].calls["param_metadata"] == before, "search never sweeps"


def test_the_answers_stay_small(providerless):
    describe = call(providerless["mcp"], "plugin_describe", {"instance": dict(INSTANCE)})
    found = call(
        providerless["mcp"],
        "plugin_find",
        {"instance": dict(INSTANCE), "query": "oscillator pitch filter cutoff", "limit": 8},
    )
    assert len(json.dumps(describe)) < 6000
    assert len(json.dumps(found)) < 6000
    assert len(found["data"]["matches"]) <= 8


# ---------------------------------------------------------------------------
# The provider path is untouched
# ---------------------------------------------------------------------------


def test_single_term_search_keeps_its_historical_semantics(providerless, monkeypatch):
    """`_score_control` is the provider path's multi-term ranking; one term must
    still mean "substring of a declared name", in profile order."""
    from reaper_reapy_mcp.plugins import api as api_module

    profile = {
        "controls": [
            {"id": "a_cutoff", "label": "Cutoff"},
            {"id": "b_cutoff_2", "label": "Cutoff 2"},
            {"id": "c_level", "label": "Level"},
        ]
    }
    count, matched = api_module._score_control(profile["controls"][0], ["cutoff"])
    assert (count, matched) == (1, ["cutoff"])
    assert api_module._term_hits(profile["controls"][1], "cutoff") is True
    assert api_module._term_hits(profile["controls"][2], "cutoff") is False
    assert api_module._term_hits(profile["controls"][2], "level") is True


def test_a_user_who_asks_for_continuity_gets_the_provider_contract(providerless):
    """The discovery path's write gate is the provider path's gate: the same
    typed refusal for a token, and the same 'address by location' remedy."""
    described = call(providerless["mcp"], "plugin_describe", {"instance": dict(INSTANCE)})
    basis = described["meta"]["identity_basis"]
    assert basis["not_claimed"] == ["instance_continuity"]
    assert basis["measured_by"], "the disclosure cites the measurement that produced it"
    refused = call(
        providerless["mcp"],
        "plugin_write",
        {
            "instance": {**INSTANCE, "target_token": described["meta"]["target_token"]},
            "request": {"controls": {"octave": 1}},
            "dry_run": False,
        },
    )
    assert refused["error"]["detail"]["remedy"].startswith("address the instance by location")
