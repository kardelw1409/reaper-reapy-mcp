"""P19 — `route_into_off_filter` is evaluated, not guarded (2026-10-11).

**The defect this closes (L-10).** The rule was declared but not evaluated, and
route changes were refused wholesale rather than being judged. Its two deciding
inputs were named as missing: "the registry carries no destination families" and
"the filter on/off flag lives in the live document". Both were measured to exist:

* every one of the vocabulary's destinations is a correspondence row's
  ``state_key``, so the join ``destination -> module`` is **total** on the
  shipping registry (316/316), and each module's on/off switch is the row the
  registry itself calls a ``Switch`` (e.g. ``Filter 1`` -> ``filter_1_on``);
* the decoded live document is read before the plan is built, and Core's
  ``plan.project_world`` now carries it, overlaid with the plan's own writes.

The measured rule is the POC's (`adapter/dependencies.py`,
`route_to_an_off_filter`): it fires only when a route into ``filter_{1,2}_*``
exists **and** that filter is OFF. So the evaluator now *decides* it:

* destination's module not in the measured scope  -> decided, no finding;
* module is Filter 1/2 and its projected flag is ON -> decided, no finding;
* module is Filter 1/2 and its projected flag is OFF -> hard violation;
* any input missing (unmapped destination, no Switch row, no flag in the
  document, a table-shaped write, a count without specs, a control bound into
  the route table) -> refused **with the missing input named**.

The engine-level cases run on the **shipping registry** (skipped when absent,
like the P16/P18 suites) because the pinned fixture registry carries no Switch
rows - which is itself pinned below.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

import fixtures as F  # noqa: F401 - the src bootstrap lives in the conftest
import mock_host as MH
from reaper_reapy_mcp.plugins import model as M
from reaper_reapy_mcp.plugins import providers as providers_module
from reaper_reapy_mcp.plugins.errors import DependencyViolation
from reaper_reapy_mcp.plugins.providers.vital import (
    DEFAULT_REGISTRY_DIR,
    VitalProvider,
    dependencies_rules as RULES,
    load_registry,
)
from reaper_reapy_mcp.plugins.transaction import Transaction
from reaper_reapy_mcp.plugins.resolve import TargetTokenRegistry
from test_p7_vital import IDENTITY, build_block, document, provider_on_fixtures

REGISTRY_DIR = Path(__file__).resolve().parent / "fixtures" / "vital_registry"


# ---------------------------------------------------------------------------
# The evaluator, on an explicit world (no host, no registry)
# ---------------------------------------------------------------------------


def map_profile(*, switches: dict[str, str] | None = None) -> dict:
    """A profile with the two derived maps the rule needs, as the provider builds them."""
    registry = load_registry(directory=str(REGISTRY_DIR))
    provider = VitalProvider(registry=registry)
    profile = provider.build_profile(dict(IDENTITY))
    modulation = dict(profile.get("modulation") or {})
    modulation["destination_modules"] = {
        "filter_1_cutoff": "Filter 1",
        "filter_2_cutoff": "Filter 2",
        "filter_fx_cutoff": "Filter FX",
        "osc_1_transpose": "Oscillator 1",
    }
    modulation["module_switches"] = dict(
        switches if switches is not None else {"Filter 1": "filter_1_on", "Filter 2": "filter_2_on", "Filter FX": "filter_fx_on"}
    )
    profile["modulation"] = modulation
    return profile


def world_for(pair: dict, *, flags: dict | None = None, **extra) -> dict:
    """A projected-world shape matching `plan.project_world` for one created pair."""
    world = {
        "routes_create": 1,
        "routes_create_specs": [pair],
        "routes_update_specs": [],
        "state_paths_changed": {},
        "controls_changed": {},
        "live_document": {"settings": dict(flags or {})},
    }
    world.update(extra)
    return world


def findings(world: dict, profile: dict) -> list:
    ruleset = RULES.dependencies(profile)
    return [f for f in ruleset.evaluate(world, ruleset.rules) if f.rule == "route_into_off_filter"]


def test_a_pair_into_an_on_filter_is_decided_and_passes():
    profile = map_profile()
    world = world_for({"source": "env_2", "destination": "filter_1_cutoff"}, flags={"filter_1_on": 1.0})
    assert findings(world, profile) == []


def test_a_pair_into_an_off_filter_is_a_decided_violation():
    profile = map_profile()
    world = world_for({"source": "env_2", "destination": "filter_1_cutoff"}, flags={"filter_1_on": 0.0})
    found = findings(world, profile)
    assert len(found) == 1
    row = found[0]
    assert row.detail["evaluated"] is True
    assert row.detail["reason"] == "route_into_off_filter"
    assert row.detail["module"] == "Filter 1"
    assert row.detail["switch"] == "filter_1_on"
    assert row.informational is False
    assert "OFF" in row.message


def test_a_pair_into_a_non_filter_destination_is_decided_without_a_flag():
    profile = map_profile()
    world = world_for({"source": "env_2", "destination": "osc_1_transpose"}, flags={})
    assert findings(world, profile) == []


def test_filter_fx_is_outside_the_measured_scope_and_says_so():
    profile = map_profile()
    world = world_for({"source": "env_2", "destination": "filter_fx_cutoff"}, flags={"filter_fx_on": 0.0})
    assert findings(world, profile) == []
    assert "Filter FX" in " ".join(RULES.evaluation_for("route_into_off_filter")["not_covered"])


def test_the_plan_turning_the_filter_on_is_what_the_rule_is_judged_against():
    """The projected world: a plan that switches the filter on in the same
    transaction must not be judged against the state it starts from."""
    profile = map_profile()
    world = world_for(
        {"source": "env_2", "destination": "filter_1_cutoff"},
        flags={"filter_1_on": 0.0},
        state_paths_changed={"settings.filter_1_on": 1.0},
    )
    assert findings(world, profile) == []


@pytest.mark.parametrize(
    "world, switches, expected_missing",
    [
        # a destination the join does not cover
        (
            world_for({"source": "env_2", "destination": "who_knows"}, flags={"filter_1_on": 1.0}),
            {"Filter 1": "filter_1_on", "Filter 2": "filter_2_on"},
            "destination->module join",
        ),
        # a filter module with no measured Switch row
        (
            world_for({"source": "env_2", "destination": "filter_1_cutoff"}, flags={"filter_1_on": 1.0}),
            {"Filter 2": "filter_2_on"},
            "on/off flag for module 'Filter 1'",
        ),
        # the flag itself is absent from the document
        (
            world_for({"source": "env_2", "destination": "filter_1_cutoff"}, flags={}),
            {"Filter 1": "filter_1_on", "Filter 2": "filter_2_on"},
            "in the live document or the plan",
        ),
        # the document is not in the world at all
        (
            {"routes_create": 1, "routes_create_specs": [{"source": "env_2", "destination": "filter_1_cutoff"}]},
            {"Filter 1": "filter_1_on", "Filter 2": "filter_2_on"},
            "in the live document or the plan",
        ),
    ],
)
def test_a_missing_input_refuses_with_the_input_named(world, switches, expected_missing):
    profile = map_profile(switches=switches)
    found = findings(world, profile)
    assert len(found) == 1, found
    assert found[0].detail["evaluated"] is False
    assert found[0].detail["reason"] == "rule_not_evaluated"
    assert expected_missing in found[0].detail["missing_input"], found[0].detail


def test_a_table_shaped_write_and_a_count_without_specs_stay_fail_closed():
    profile = map_profile()
    table_write = {
        "state_paths_changed": {"settings.modulations.3": {"source": "env_2"}},
        "live_document": {"settings": {"filter_1_on": 1.0}},
    }
    found = findings(table_write, profile)
    assert found and all(f.detail["evaluated"] is False for f in found)

    short = {"routes_create": 2, "routes_create_specs": [], "live_document": {"settings": {"filter_1_on": 1.0}}}
    found = findings(short, profile)
    assert found and "without carrying the specs" in found[0].detail["observed"]


def test_a_source_only_update_is_decided_from_the_slots_live_destination():
    profile = map_profile()
    base = {
        "routes_update": 1,
        "routes_update_specs": [{"slot": 2, "source": "env_2"}],
        "live_document": {
            "settings": {
                "filter_1_on": 0.0,
                "modulations": [{"source": "", "destination": ""}, {"source": "lfo_1", "destination": "filter_1_cutoff"}],
            }
        },
    }
    found = findings(base, profile)
    assert len(found) == 1 and found[0].detail["evaluated"] is True, found

    empty = copy.deepcopy(base)
    empty["live_document"]["settings"]["modulations"][1] = {"source": "", "destination": ""}
    found = findings(empty, profile)
    assert found and found[0].detail["evaluated"] is False
    assert "live destination of slot 2" in found[0].detail["missing_input"]


def test_operations_that_create_no_pair_stay_silent():
    profile = map_profile()
    quiet = [
        {"live_document": {"settings": {"filter_1_on": 0.0}}},
        {"routes_remove": 1, "live_document": {"settings": {"filter_1_on": 0.0}}},
        {"routes_update_specs": [{"slot": 2, "amount": 0.25}], "live_document": {"settings": {"filter_1_on": 0.0}}},
        {"state_paths_changed": {"settings.modulations.3.destination": ""}, "live_document": {"settings": {"filter_1_on": 0.0}}},
        {"state_paths_changed": {"settings.modulation_3_amount": 0.0}, "live_document": {"settings": {"filter_1_on": 0.0}}},
    ]
    for world in quiet:
        assert findings(world, profile) == [], world


# ---------------------------------------------------------------------------
# The derivation, against the shipping registry (skip when absent)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def real_profile() -> dict:
    try:
        registry = load_registry(directory=str(DEFAULT_REGISTRY_DIR))
    except Exception as exc:  # noqa: BLE001 - an absent registry is a skip
        pytest.skip(f"the measured registry is not present: {exc}")
    provider = VitalProvider(registry=registry)
    return provider.build_profile(dict(IDENTITY))


def test_the_destination_module_join_is_total_on_the_shipping_registry(real_profile):
    modulation = real_profile["modulation"]
    coverage = modulation["destination_module_coverage"]
    assert coverage["mapped"] == coverage["total"], coverage
    assert coverage["mapped"] >= 300, coverage
    assert modulation["destination_modules"]["filter_1_cutoff"] == "Filter 1"
    assert modulation["destination_modules"]["osc_1_transpose"] == "Oscillator 1"


def test_the_module_switches_come_from_the_registry_rows_themselves(real_profile):
    switches = real_profile["modulation"]["module_switches"]
    assert switches["Filter 1"] == "filter_1_on"
    assert switches["Filter 2"] == "filter_2_on"
    assert switches["Filter FX"] == "filter_fx_on"
    assert switches["Oscillator 1"] == "osc_1_on"
    # derived, not typed: every switch is a control the profile itself declares
    declared = {control["id"] for control in real_profile["controls"]}
    for module, key in switches.items():
        assert key in declared, (module, key)


def test_the_shipping_profile_declares_the_route_rule_evaluated(real_profile):
    rows = {row["id"]: row for row in real_profile["dependencies"]}
    assert rows["route_into_off_filter"]["evaluated"] is True
    assert rows["fm_needs_modulator"]["evaluated"] is False
    assert rows["wave_frame_needs_multiframe_wavetable"]["evaluated"] is False
    ops = {row["id"]: row for row in real_profile["state"]["structural_ops"]}
    assert ops["route_create"]["available"] is True
    assert ops["route_create"]["available_reason"] is None


def test_the_route_rule_is_not_in_the_unevaluated_report(real_profile):
    # the report is provider-level, not profile-level; both must agree
    assert {row["rule"] for row in RULES.rules_not_evaluated()} == {
        "fm_needs_modulator",
        "wave_frame_needs_multiframe_wavetable",
    }
    assert all(real_profile["dependencies"])


# ---------------------------------------------------------------------------
# The engine, on the shipping registry with a Vital-shaped document (mock host)
# ---------------------------------------------------------------------------


def engine_host(*, filter_1_on: float | None) -> MH.MockHostBridge:
    doc = copy.deepcopy(document())
    if filter_1_on is not None:
        doc["settings"]["filter_1_on"] = float(filter_1_on)
    slot = MH.MockFxSlot(
        name=IDENTITY["fx_name"],
        module=IDENTITY["module"],
        class_id=IDENTITY["class_id"],
        block=build_block(doc),
        params={"p0": 0.5, "p1": 0.5, "p2": 0.5, "p3": 0.5, "p4": 0.5, "p5": 0.5, "p6": 0.5, "p7": 0.5},
        param_names={0: "Beats Per Minute", 1: "Chorus Filter Cutoff"},
        version="1.5.5",
    )
    return MH.MockHostBridge(tracks=[MH.MockTrack(0, "Track 1", [slot])])


def run_engine(host: MH.MockHostBridge, provider: VitalProvider, request: dict, *, dry_run: bool):
    profile = provider.build_profile(dict(IDENTITY))
    registry = TargetTokenRegistry()
    token = registry.issue(facts=host.fx_facts(0, 0))
    ref = M.PluginInstanceRef(
        host="reaper", location={"track_index": 0, "fx_index": 0}, provider="vital", identity=IDENTITY
    )
    return Transaction(
        provider=provider,
        codec=provider.codec(),
        bridge=host,
        profile=profile,
        ref=ref,
        request=dict(request),
        token=token,
        registry=registry,
        dry_run=dry_run,
    ).run()


@pytest.fixture(scope="module")
def shipping_provider() -> VitalProvider:
    try:
        registry = load_registry(directory=str(DEFAULT_REGISTRY_DIR))
    except Exception as exc:  # noqa: BLE001 - an absent registry is a skip
        pytest.skip(f"the measured registry is not present: {exc}")
    return VitalProvider(registry=registry)


@pytest.mark.parametrize("dry_run", [True, False])
def test_engine_creating_a_pair_into_an_off_filter_is_a_decided_violation(shipping_provider, dry_run):
    host = engine_host(filter_1_on=0.0)
    result = run_engine(
        host,
        shipping_provider,
        {"routes_create": [{"source": "env_2", "destination": "filter_1_cutoff", "amount": 0.05}]},
        dry_run=dry_run,
    )
    assert result.ok is False, result.data
    assert isinstance(result.error, DependencyViolation), result.error
    assert result.error.detail["unevaluable"] == [], result.error.detail
    row = result.error.detail["findings"][0]
    assert row["evaluated"] is True
    assert row["detail"]["module"] == "Filter 1"
    assert result.outcome.host.write_calls_attempted == 0
    assert [entry for entry in host.log if entry[0].startswith("write")] == []


@pytest.mark.parametrize("dry_run", [True, False])
def test_engine_creating_a_pair_into_an_on_filter_is_applied(shipping_provider, dry_run):
    host = engine_host(filter_1_on=1.0)
    result = run_engine(
        host,
        shipping_provider,
        {"routes_create": [{"source": "env_2", "destination": "filter_1_cutoff", "amount": 0.05}]},
        dry_run=dry_run,
    )
    assert result.ok is True, result.error
    writes = [entry for entry in host.log if entry[0] == "write_state_slice"]
    if dry_run:
        assert writes == []
        assert result.data["would_write"] is True
        assert result.data["dependency_findings"] == []
    else:
        assert len(writes) == 1, writes
        assert result.outcome.verdict is M.Verdict.ALL_VERIFIED
        slots = json.loads(json.dumps(result.data["plan_summary"]))
        assert slots


def test_engine_creating_a_pair_into_a_pitch_destination_needs_no_filter_flag(shipping_provider, tmp_path):
    """The 808 recipe's own shape: env -> oscillator pitch, no filter involved."""
    host = engine_host(filter_1_on=0.0)  # even with Filter 1 OFF
    result = run_engine(
        host,
        shipping_provider,
        {"routes_create": [{"source": "env_2", "destination": "osc_1_transpose", "amount": 0.05}]},
        dry_run=False,
    )
    assert result.ok is True, result.error
    assert result.outcome.verdict is M.Verdict.ALL_VERIFIED
    assert [entry for entry in host.log if entry[0] == "write_state_slice"]
    assert result.data["dependency_findings"] == []
    # the written document really holds the pair, decoded through the provider's
    # own codec (the block is a base64 blob - a substring check would prove nothing)
    ref = M.PluginInstanceRef(
        host="reaper", location={"track_index": 0, "fx_index": 0}, provider="vital", identity=IDENTITY
    )
    facts = host.fx_facts(0, 0)
    chunk = host.read_state_chunk(ref)
    decoded = shipping_provider.codec().decode(host.locate_state_slice(ref, facts, chunk))
    active = [row for row in decoded["routes"] if row.get("destination")]
    # the fixture document carries its own active route (lfo_1 -> chorus_delay_1);
    # the created pair must exist **in addition**, with the codec's own defaults
    created = [row for row in active if row["destination"] == "osc_1_transpose"]
    assert len(created) == 1, active
    assert created[0]["source"] == "env_2"
    assert float(created[0]["amount"]) == pytest.approx(0.05, abs=1e-6)
    # the corpus's own neutral (16868/17036 routes store 0.0; an empty live slot
    # stores 0.0 while the plugin's "Modulation N Power" displays "0")
    assert float(created[0]["power"]) == pytest.approx(0.0, abs=1e-9)
    assert float(created[0]["bypass"]) == pytest.approx(0.0, abs=1e-9)
    assert float(created[0]["bipolar"]) == pytest.approx(0.0, abs=1e-9)
