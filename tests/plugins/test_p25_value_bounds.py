"""P25 tests: range integrity, the two integer ladders, and unnamed enum states.

Three defects, three sources of proof, and one rule the round keeps: *widen only*.

**Task A - the declared range was a corpus sample.** ``_range_from_row`` took
``native_min``/``native_max`` from the registry, which are the extremes a 900-preset
corpus reached (``phase2b_native_ranges.json``). The P24 census compared those
numbers against the host's printed text and reported 103 disagreements; P25 read the
plugin's own parameter table, verified it against the live host formatter on 7,650
samples (``source_facts.CORRESPONDENCE``) and split the 103 in two: 51 were the
*same range in two different spaces* (a sample of 1 in a percent control, or a
state value against ``value - 60``), and 52 were genuinely truncated. Across the
whole profile 70 rows were truncated - each one with the registry's own ``fit``
already carrying the right bounds - and each is widened to the plugin's bounds with
the before/after, the fit corroboration and the display transform in
``evidence.value_bounds``.

**Task B - two controls the display could not settle.** ``effect_chain_order`` and
``sample_transpose_quantize`` never printed a fraction at 70 arbitrary points. The
source says why and settles the model: both are ``ValueDetails::kIndexed`` with a
span of 10,000s, so ``ValueBridge::isDiscrete()`` is false (the host sees a
continuous parameter and reports *no* step - measured), while
``convertToEngineValue`` rounds *every* value the host write channel stores. A
fractional request is therefore stored rounded and the display rounds too, which is
storage quantisation, not a formatting accident. The profile now says so
(``law.params.quantization``), and the core snaps both conversion directions onto
that grid, clamped to the range.

**Task C - names with a source, and names without one.** ``bypass`` prints ``0``/``60``
because its own table multiplies the value by 60 and carries **no** label table, so
the words ``normal``/``bypassed`` are the registry's, not the plugin's - recorded as
such, and the transition stays unresolved *by construction*. ``filter_N_style`` has a
five-entry name table for a parameter declared over 0.0..9.0, so the sixth band has
no name to print (the release prints an empty string); the parameter's text does
**not** depend on ``filter_N_model`` (that naming lives in the editor), so the enum
needs no contextual dependency - only the note, now proven instead of deferred.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reaper_reapy_mcp.plugins import cache as cache_module
from reaper_reapy_mcp.plugins import profile as profile_module
from reaper_reapy_mcp.plugins import values as V
from reaper_reapy_mcp.plugins.errors import InvalidValue
from reaper_reapy_mcp.plugins.providers.vital import profile_build as PB
from reaper_reapy_mcp.plugins.providers.vital import source_facts as SF
from reaper_reapy_mcp.plugins.providers.vital.provider import VitalProvider

FIXTURES = Path(__file__).resolve().parent / "fixtures"
REGISTRY_DIR = FIXTURES / "vital_registry"
IDENTITY = {
    "fx_name": PB.VITAL_FX_NAME,
    "class_id": PB.VITAL_CLASS_ID,
    "module": PB.VITAL_MODULE,
    "param_count": 2858,
}
FIXTURE_IDENTITY = {**IDENTITY, "param_count": 8}

#: The revision P24 shipped: pinned, because the point of the test below is that the
#: revision moved when ranges (part of what a profile means) changed.
P24_REVISION = "vital-provider-2026.10-r11"


def profile_from(directory) -> dict:
    registry = PB.load_registry(directory=str(directory))
    provider = VitalProvider(registry=registry)
    return cache_module.build_capability_profile(provider, dict(IDENTITY)).profile


@pytest.fixture(scope="module")
def fixture_profile() -> dict:
    return profile_from(REGISTRY_DIR)


@pytest.fixture(scope="module")
def real_profile() -> dict:
    try:
        PB.load_registry(directory=PB.DEFAULT_REGISTRY_DIR)
    except Exception as exc:  # noqa: BLE001 - an absent registry is a skip
        pytest.skip(f"the measured registry is not present: {exc}")
    return profile_from(PB.DEFAULT_REGISTRY_DIR)


def by_id(profile: dict, control_id: str) -> dict:
    for control in profile["controls"]:
        if control["id"] == control_id:
            return control
    raise AssertionError(f"control {control_id!r} is not in the profile")


def ladder_control(**overrides) -> dict:
    """A minimal numeric control carrying the storage grid the ladders declare."""
    control = {
        "id": "ladder",
        "label": "ladder",
        "type": "number",
        "unit": None,
        "range": {"min": 0.0, "max": 8191.0},
        "discrete": True,
        "enum_values": None,
        "aliases": [],
        "authority": {"read": "parameter", "write": "parameter"},
        "binding": {"index": 1, "ident": None, "state_key": "ladder"},
        "law": {
            "kind": "affine",
            "params": {
                "scale": 1.0,
                "offset": 0.0,
                "quantization": {"step": 1.0, "origin": 0.0, "rounding": "half_away_from_zero"},
            },
        },
        "evidence": {"strength": "strong"},
    }
    control.update(overrides)
    return control


def minimal_profile(controls: list[dict] | None = None) -> dict:
    return {
        "profile_schema_version": 2,
        "provider": "vital",
        "identity": {"plugin_id": "vital", "fx_name": "x", "class_id": "y", "module": "z"},
        "controls": controls or [],
        "values": {"kinds": list(V.LAW_KINDS)},
        "provenance": {"provider_revision": "1", "provider_artifact_digest": "0" * 64},
        "state": {"capture_scope": {"channels": ["track_state_chunk"]}},
    }


# ---------------------------------------------------------------------------
# A1. The facts table and its own verification
# ---------------------------------------------------------------------------


def test_the_facts_table_carries_the_seventy_widened_controls_and_the_two_ladders():
    assert len(SF.SOURCE_BOUNDS) == 70, "the measured truncation set is pinned"
    assert set(SF.SOURCE_BOUNDS) == set(SF.DISPLAY_FACTS), (
        "every widened control must carry its display transform, or the evidence for "
        "why the numbers differ would be missing"
    )
    assert set(SF.INTEGER_LADDERS) == {"effect_chain_order", "sample_transpose_quantize"}
    for bounds in SF.SOURCE_BOUNDS.values():
        assert bounds["min"] < bounds["max"]


def test_every_display_fact_reproduces_the_live_host_text():
    """The correspondence claim, checked where a reader can check it offline.

    ``text_at_*`` is what the plugin's own model computes at the two extremes;
    ``live_text_at_*`` is what the *live host formatter* printed there. They agree
    everywhere except one control whose *unit token* differs between the public
    source and the release (``cents`` vs ``c``) - which is named, so a new drift
    fails this test instead of hiding inside a total.
    """
    drifted = []
    for control_id, facts in sorted(SF.DISPLAY_FACTS.items()):
        if facts["text_at_0"] != facts["live_text_at_0"] or facts["text_at_1"] != facts["live_text_at_1"]:
            drifted.append(control_id)
    assert drifted == ["voice_tune"], drifted
    assert SF.CORRESPONDENCE["numbers_differ"] == 0
    assert SF.CORRESPONDENCE["samples_compared"] == 7650


# ---------------------------------------------------------------------------
# A2. The rule: widen only
# ---------------------------------------------------------------------------


def test_only_a_strict_subset_is_widened():
    source = SF.SOURCE_BOUNDS["sample_pan"]
    declared = {"min": -1.0, "max": 0.059999942779541016}
    record = SF.widen_to_source_bounds("sample_pan", declared)
    assert record is not None
    assert record["min"] == source["min"] and record["max"] == source["max"]
    assert record["declared_before"] == declared
    assert record["widened"] == {"min": None, "max": source["max"]}

    # a range that already equals the source is not a correction ...
    assert SF.widen_to_source_bounds("sample_pan", dict(source)) is None
    # ... neither is one that is *wider* (it would be a narrowing) ...
    assert SF.widen_to_source_bounds("sample_pan", {"min": -2.0, "max": 2.0}) is None
    # ... nor one that is shifted (its min is *inside* the source's, its max outside) ...
    assert SF.widen_to_source_bounds("sample_pan", {"min": -0.5, "max": 3.0}) is None
    # ... nor a range-less row, nor a control the table does not list
    assert SF.widen_to_source_bounds("sample_pan", None) is None
    assert SF.widen_to_source_bounds("filter_1_cutoff", {"min": 1.0, "max": 2.0}) is None


def test_a_widened_range_is_a_corrected_conversion(fixture_profile):
    """The point of the widening: the mapping changes with it.

    The fixture registry declares ``lfo_1_frequency`` over the corpus range
    ``-5.32 .. 9`` (log2 seconds). That spine puts the plugin's own minimum
    (``-7`` -> 128 s) at normalized ``-0.34``, i.e. *outside* the parameter - a value
    the profile could neither accept nor write. The widened range puts it at 0.
    """
    control = by_id(fixture_profile, "lfo_1_frequency")
    source = SF.SOURCE_BOUNDS["lfo_1_frequency"]
    assert control["range"] == {"min": source["min"], "max": source["max"]}
    assert V.to_normalized(control, -7.0) == 0.0
    assert V.to_normalized(control, 9.0) == 1.0

    before = {**control, "range": dict(control["evidence"]["value_bounds"]["declared_before"])}
    assert V.to_normalized(before, -7.0) < 0.0, (
        "under the corpus range the plugin's own minimum mapped outside the parameter"
    )
    assert not (before["range"]["min"] <= -7.0 <= before["range"]["max"])


def test_the_fixture_profile_records_exactly_what_it_widened(fixture_profile):
    block = fixture_profile["value_bounds"]
    widened = [
        str(control["id"])
        for control in fixture_profile["controls"]
        if "value_bounds" in (control.get("evidence") or {})
    ]
    assert block["state"] == ("applied" if widened else "none")
    assert block["controls"] == widened
    assert block["counts"] == {"controls_widened": len(widened)}
    assert widened, "the fixture registry declares at least one truncated range"
    for control_id in widened:
        assert control_id in SF.SOURCE_BOUNDS
        record = by_id(fixture_profile, control_id)["evidence"]["value_bounds"]
        assert record["declared_after"] == {
            "min": SF.SOURCE_BOUNDS[control_id]["min"],
            "max": SF.SOURCE_BOUNDS[control_id]["max"],
        }
        assert record["source"] == SF.CORRESPONDENCE["source"]
        assert "state-space" in record["evidence_level"]


def test_a_row_that_already_equals_the_source_is_untouched(fixture_profile):
    # chorus_feedback: the corpus max is float32(0.95), exactly the source's bound
    for control_id in ("chorus_feedback", "filter_1_cutoff"):
        assert "value_bounds" not in by_id(fixture_profile, control_id)["evidence"], control_id


def test_a_row_whose_law_was_measured_never_receives_source_bounds(fixture_profile):
    """The space guard: a fitted or live-measured row lives in the display's space.

    ``distortion_filter_blend`` is a converted ramp (fitted to the sweep) and
    ``volume`` was corrected from a live calibration; both are in the display's own
    units, so the plugin's state-space bounds say nothing about them. The same guard
    keeps the 224 display-space rows of the real profile out of the correction.
    """
    for control_id in ("distortion_filter_blend", "volume"):
        control = by_id(fixture_profile, control_id)
        assert "value_bounds" not in control["evidence"], control_id
        assert "measured sweep fit" in str(control["evidence"].get("source")) or (
            "live REAPER formatted-display calibration" in str(control["evidence"].get("source"))
        ), control_id


def test_the_real_profile_widens_exactly_the_measured_seventy(real_profile):
    widened = {
        str(control["id"])
        for control in real_profile["controls"]
        if "value_bounds" in (control.get("evidence") or {})
    }
    assert widened == set(SF.SOURCE_BOUNDS), (
        "the widened set must be exactly the fact table: a new truncation needs a new "
        "row (and the revision to move), a stale one needs the row removed"
    )
    assert real_profile["value_bounds"]["state"] == "applied"
    assert len(real_profile["value_bounds"]["controls"]) == 70
    # every widened control's range is now a range the plugin itself declares
    for control_id in widened:
        control = by_id(real_profile, control_id)
        assert control["range"] == {
            "min": SF.SOURCE_BOUNDS[control_id]["min"],
            "max": SF.SOURCE_BOUNDS[control_id]["max"],
        }
        display = control["evidence"]["value_bounds"]["value_space"]["display"]
        if control_id in SF.CORRESPONDENCE["unit_label_drift"]:
            # the one named drift (the release's own unit token differs from the public
            # branch's): the *number* still agrees, which is what the bound rests on
            assert display["text_at_0"].replace(" cents", " c") == display["live_text_at_0"]
            assert display["text_at_1"].replace(" cents", " c") == display["live_text_at_1"]
        else:
            assert display["text_at_0"] == display["live_text_at_0"]
            assert display["text_at_1"] == display["live_text_at_1"]


def test_the_registry_fit_is_recorded_as_corroboration(real_profile):
    """Two independent authorities: the plugin's table and the registry's own fit.

    Where the fit's numbers differ they are in the *display* space (a pan fit of
    -100..100 against a state range of -1..1 with ``display_multiply: 100``), and the
    record says so rather than leaving a bare disagreement.
    """
    agreeing = disagreeing = 0
    for control in real_profile["controls"]:
        record = (control.get("evidence") or {}).get("value_bounds")
        if not record:
            continue
        fit = record["registry_fit"]
        if fit["agrees_with_the_source"]:
            agreeing += 1
            assert abs(float(fit["min"]) - record["declared_after"]["min"]) <= 1e-6
        else:
            disagreeing += 1
            assert fit.get("agrees_after_the_display_transform") is True, control["id"]
            assert fit.get("fit_units") == "display space"
    assert agreeing > disagreeing > 0, (agreeing, disagreeing)


# ---------------------------------------------------------------------------
# B. The two integer ladders: the step is in the engine
# ---------------------------------------------------------------------------


def test_the_ladders_keep_the_sweep_range_and_declare_the_step(real_profile):
    for control_id, ladder in SF.INTEGER_LADDERS.items():
        control = by_id(real_profile, control_id)
        assert control["type"] == "number"
        assert control["discrete"] is True, "the engine value is an integer, so the control is discrete"
        assert control["range"] == {"min": ladder["min"], "max": ladder["max"]}
        quantization = control["law"]["params"]["quantization"]
        assert quantization["step"] == ladder["step"] == 1.0
        assert quantization["origin"] == ladder["min"]
        assert quantization["rounding"] == "half_away_from_zero"
        assert "kIndexed" in control["evidence"]["enum_declaration_overridden"]["integer_ladder"]["plugin_rounding"]
        # the sweep's range is not a corpus range, so the bounds table must not touch it
        assert control_id not in SF.SOURCE_BOUNDS


def test_a_fractional_request_lands_on_the_grid_the_plugin_stores():
    control = ladder_control()
    control["range"] = {"min": 0.0, "max": 362879.0}
    assert V.to_native(control, 181440.4) == 181440.0
    assert V.to_native(control, 0.5) == 1.0, "std::round takes a halfway case away from zero"
    assert V.to_native(control, 362879.0) == 362879.0
    plan = V.display_to_write_space(control, 181440.4)
    assert plan["native"] == 181440.0
    assert plan["normalized"] == pytest.approx(181440.0 / 362879.0)

    quantize = ladder_control()
    assert V.to_native(quantize, 8191.0) == 8191.0
    # out of range is *refused*, not clamped (the range gate comes first, unchanged)
    with pytest.raises(InvalidValue):
        V.to_native(quantize, -5.2)
    with pytest.raises(InvalidValue):
        V.to_native(quantize, 8191.9)

    # a range whose end is *not* on the grid clamps to the last step inside it
    off_grid = ladder_control()
    off_grid["range"] = {"min": 0.0, "max": 9.5}
    assert V.to_native(off_grid, 9.5) == 9.0


def test_the_read_side_reports_a_value_the_plugin_can_hold():
    control = ladder_control()
    native = V.normalized_to_native(control, 0.5)
    assert native == float(round(native)), native
    # and the two directions agree exactly after the snap
    assert V.to_normalized(control, native) == pytest.approx((native - 0.0) / 8191.0)


def test_a_law_without_a_step_converts_exactly_as_before():
    plain = ladder_control()
    del plain["law"]["params"]["quantization"]
    assert V.to_native(plain, 4095.5) == pytest.approx(4095.5)
    assert V.normalized_to_native(plain, 0.5) == pytest.approx(4095.5)


@pytest.mark.parametrize(
    "grid",
    [
        {"step": 0.0, "origin": 0.0},
        {"step": -1.0, "origin": 0.0},
        {"step": float("nan"), "origin": 0.0},
        {"step": "1", "origin": 0.0},
        {"origin": 0.0},
        "not-a-dict",
    ],
)
def test_a_malformed_grid_is_ignored_not_approximated(grid):
    control = ladder_control()
    control["law"]["params"]["quantization"] = grid
    assert V._storage_step(control) is None
    assert V.to_native(control, 4095.5) == pytest.approx(4095.5)
    assert V.normalized_to_native(control, 0.5) == pytest.approx(4095.5)


def test_the_validator_refuses_an_unusable_grid():
    broken = ladder_control()
    broken["law"]["params"]["quantization"] = {"step": 0.0, "origin": 0.0}
    validation = profile_module.validate_profile(minimal_profile([broken]))
    assert "law_quantization_valid" in validation.codes()
    assert validation.rebuild_required is True, (
        "the loader consults rebuild_required/cacheable and never ok - a plain "
        "validation error would leave a stored entry served (the P24 lesson)"
    )

    good = ladder_control()
    assert "law_quantization_valid" not in profile_module.validate_profile(
        minimal_profile([good])
    ).codes()


def test_a_grid_the_range_does_not_sit_on_is_refused():
    """A grid is only usable where the range can walk on it (the round's review, F4).

    The snap rounds in the control's own space with the declared origin, while a plugin
    that rounds its value does not clamp: for an origin the range does not sit on, the
    two would disagree about the first step. No current row trips it (both ladders are
    0-aligned with integer bounds), and the validator makes sure none ever does.
    """
    misaligned = ladder_control()
    misaligned["law"]["params"]["quantization"] = {"step": 1.0, "origin": 0.3}
    validation = profile_module.validate_profile(minimal_profile([misaligned]))
    assert "law_quantization_valid" in validation.codes()
    assert validation.rebuild_required is True

    aligned = ladder_control()
    aligned["law"]["params"]["quantization"] = {"step": 0.5, "origin": 0.0}
    assert "law_quantization_valid" not in profile_module.validate_profile(
        minimal_profile([aligned])
    ).codes()


def test_the_loader_refuses_a_stored_profile_with_a_bad_grid(tmp_path: Path):
    registry = PB.load_registry(directory=str(REGISTRY_DIR))
    provider = VitalProvider(registry=registry)
    profile = cache_module.build_capability_profile(provider, dict(FIXTURE_IDENTITY)).profile
    material = cache_module.key_material_of(provider, dict(FIXTURE_IDENTITY))
    key = cache_module.compute_cache_key(
        provider_id=provider.id,
        identity=dict(FIXTURE_IDENTITY),
        provider_revision=material.provider_revision,
        provider_artifact_digest=material.provider_artifact_digest,
        source_digest=material.source_digest,
    )
    cache = cache_module.ProfileCache(provider_id=provider.id, directory=tmp_path / "cache")
    assert cache.save(key, profile) and cache.path_for(key).exists()
    assert cache.load(key, identity=dict(FIXTURE_IDENTITY)).status == "hit"

    # corrupt the entry the way a hand edit could, keeping it self-consistent
    entry = json.loads(cache.path_for(key).read_text(encoding="utf-8"))
    victim = by_id(entry["profile"], "lfo_1_frequency")
    victim["law"]["params"]["quantization"] = {"step": -1.0, "origin": 0.0}
    entry["content_sha256"] = cache_module.profile_content_hash(entry["profile"])
    entry["counts"] = cache_module.profile_counts(entry["profile"])
    cache.path_for(key).write_text(json.dumps(entry), encoding="utf-8", newline="\n")

    loaded = cache.load(key, identity=dict(FIXTURE_IDENTITY))
    assert loaded.status == "refused" and loaded.profile is None
    assert loaded.refusal.rule == "profile_invalid", loaded.refusal.rule
    assert cache.status.quarantined, "a profile the validator refuses must not be served"


# ---------------------------------------------------------------------------
# C. Names with a source, and names without one
# ---------------------------------------------------------------------------


def test_bypass_states_carry_their_provenance(real_profile):
    control = by_id(real_profile, "bypass")
    evidence = control["law"]["params"]["boundary_evidence"]
    assert evidence["state"] == "undetermined"
    assert evidence["states"] == ["unresolved"]
    assert "0" in evidence["note"] and "60" in evidence["note"]
    assert "no state label to measure" in evidence["note"]
    labels = evidence["state_labels"]
    assert labels["display_multiply"] == 60.0
    assert labels["string_lookup"] is None
    assert labels["member_ids"] == ["normal", "bypassed"]
    assert "not the plugin's display function" in labels["member_ids_source"]
    assert labels["replay_source"] if "replay_source" in labels else True
    # the member list itself is unchanged: the ids are the profile's identifiers
    assert [member["id"] for member in control["enum_values"]] == ["normal", "bypassed"]


def test_the_style_region_names_the_source_of_its_unnamed_band(real_profile):
    regions = [
        region
        for region in real_profile["enum_boundaries"]["unlabelled_regions"]
        if region["control"].startswith("filter_")
    ]
    assert regions, "the filter styles carry an unnamed band"
    for region in regions:
        assert region["labels"] == SF.FILTER_STYLE_FACTS["labels"]
        assert region["named_indices"] == [0, 1, 2, 3, 4]
        assert region["unnamed_indices"] == [5, 6, 7, 8]
        assert region["parameter_bounds"] == {"min": 0.0, "max": 8.0}
        assert region["display_depends_on_the_filter_model"] is False
        assert "value_bridge.h" in region["source"] or "synth_strings.h" in region["source"]
        assert region["ui_labels_by_model"]["Diode"] == ["Low Shelf", "Low Cut"]
        assert "has five entries" in region["why"]


def test_the_unnamed_band_is_the_one_past_the_five_name_table():
    """The cause, stated as arithmetic: index 5 reads past a five-entry table."""
    facts = SF.FILTER_STYLE_FACTS
    assert len(facts["labels"]) == 5
    assert facts["value_scale"] == "indexed"
    assert max(facts["named_indices"]) == len(facts["labels"]) - 1
    assert min(facts["unnamed_indices"]) == len(facts["labels"])


def test_the_style_enum_geometry_is_unchanged(real_profile):
    """No regression: P25 added provenance, not geometry (pinned to the P23 values)."""
    for control_id in ("filter_1_style", "filter_2_style", "filter_fx_style"):
        control = by_id(real_profile, control_id)
        assert [member["id"] for member in control["enum_values"]] == list(SF.FILTER_STYLE_FACTS["labels"])
        params = control["law"]["params"]
        assert params["positions"] == [0.03125, 0.125, 0.25, 0.375, 0.5], control_id
        # the bisected boundaries are carried at full precision; the last three are the
        # float32 neighbours of the round numbers (P23's measurement, unchanged by P25)
        assert params["switch_points"] == pytest.approx(
            [0.0, 0.0625, 0.1875, 0.3125, 0.4375], abs=1e-6
        ), control_id


def test_the_style_span_is_the_measured_eight_not_the_public_nine():
    """The public branch and the release disagree here, and the measurement decides.

    ``ValueBridge::span_`` is ``round(max - min)`` for kIndexed, and the label changes
    the host actually shows sit at ``(k + 0.5)/8`` - so the release's ``style`` is
    declared over 0..8 while the public table says 0..9. The profile keeps the five
    named bands and the unnamed tail; only the *reason* for the tail changes (an index
    past a five-entry table), and that is what the facts record.
    """
    facts = SF.FILTER_STYLE_FACTS
    assert facts["parameter_bounds"] == {"min": 0.0, "max": 8.0}
    assert facts["public_source_bounds"] == {"min": 0.0, "max": 9.0}
    assert facts["unnamed_indices"] == [5, 6, 7, 8]
    # the measured boundary of the region: (5 - 0.5)/8 = 0.5625
    region_start = (min(facts["unnamed_indices"]) - 0.5) / facts["parameter_bounds"]["max"]
    assert region_start == 0.5625


# ---------------------------------------------------------------------------
# D. The percent trap: a fraction space is not a percent space
# ---------------------------------------------------------------------------


def test_the_percent_trap_keeps_firing_on_a_percent_space():
    percent = {"id": "k", "type": "number", "unit": "%", "range": {"min": -100.0, "max": 100.0}}
    with pytest.raises(InvalidValue):
        V.unit_trap(percent, 1.0)
    V.unit_trap(percent, 2.0)
    V.unit_trap(percent, 100.0)


def test_the_percent_trap_does_not_fire_on_a_fraction_space():
    """``1.0`` in a 0..1 space is the top of the range, not one percent."""
    for bounds in ({"min": 0.0, "max": 1.0}, {"min": -1.0, "max": 1.0}):
        fraction = {"id": "m", "type": "number", "unit": "%", "range": bounds}
        V.unit_trap(fraction, 1.0)
        V.unit_trap(fraction, 0.5)
        plan = V.display_to_write_space(fraction, 1.0)
        assert plan["normalized"] == pytest.approx(1.0 if bounds["min"] == 0.0 else 1.0)


def test_the_trap_still_fires_when_no_range_can_prove_the_space():
    unknown = {"id": "u", "type": "number", "unit": "%", "range": None}
    with pytest.raises(InvalidValue):
        V.unit_trap(unknown, 1.0)
    other_unit = {"id": "s", "type": "number", "unit": "secs", "range": {"min": 0.0, "max": 10.0}}
    V.unit_trap(other_unit, 1.0)


# ---------------------------------------------------------------------------
# E. The change must reach the cache
# ---------------------------------------------------------------------------


def test_the_change_moves_the_provider_revision_and_therefore_the_cache_key(tmp_path: Path):
    registry = PB.load_registry(directory=str(REGISTRY_DIR))
    provider = VitalProvider(registry=registry)
    material = cache_module.key_material_of(provider, dict(FIXTURE_IDENTITY))
    assert material.provider_revision == provider.implementation_revision
    assert material.provider_revision != P24_REVISION, (
        "P25 changed what 70 ranges mean; the revision is the determinant that declares "
        "the semantics generation (07 §3.4)"
    )

    def key_for(revision: str) -> str:
        return cache_module.compute_cache_key(
            provider_id=provider.id,
            identity=dict(FIXTURE_IDENTITY),
            provider_revision=revision,
            provider_artifact_digest=material.provider_artifact_digest,
            source_digest=material.source_digest,
        )

    current, stale = key_for(material.provider_revision), key_for(P24_REVISION)
    assert current != stale
    cache = cache_module.ProfileCache(provider_id=provider.id, directory=tmp_path / "cache")
    stale_path = cache.path_for(stale)
    stale_path.parent.mkdir(parents=True, exist_ok=True)
    stale_path.write_text('{"provider_id": "vital", "note": "r11"}\n', encoding="utf-8", newline="\n")
    loaded = cache.load(current, identity=dict(FIXTURE_IDENTITY))
    assert loaded.status == "miss"
    assert stale_path.read_text(encoding="utf-8").startswith('{"provider_id": "vital"')
    assert cache.status.quarantined == []


def test_a_widening_record_that_its_range_contradicts_is_refused(fixture_profile, tmp_path: Path):
    """The review's F1: two fail-open branches, both now `refuse_rebuild`.

    A self-consistent, hand-edited entry whose `evidence.value_bounds` is unreadable
    validated with a plain error (which the loader ignores) and whose `range` was
    tampered away from `declared_after` validated **ok** - both were served as cache
    hits, so a corrupted spine would silently run every write. Both now stop the
    entry, and the loader quarantines it.
    """
    registry = PB.load_registry(directory=str(REGISTRY_DIR))
    provider = VitalProvider(registry=registry)
    profile = cache_module.build_capability_profile(provider, dict(FIXTURE_IDENTITY)).profile
    material = cache_module.key_material_of(provider, dict(FIXTURE_IDENTITY))
    key = cache_module.compute_cache_key(
        provider_id=provider.id,
        identity=dict(FIXTURE_IDENTITY),
        provider_revision=material.provider_revision,
        provider_artifact_digest=material.provider_artifact_digest,
        source_digest=material.source_digest,
    )
    target = next(
        control for control in profile["controls"] if "value_bounds" in (control.get("evidence") or {})
    )
    assert target["range"] == target["evidence"]["value_bounds"]["declared_after"]

    def corrupted(mutate) -> dict:
        victim = json.loads(json.dumps(profile))
        control = next(
            row for row in victim["controls"] if row["id"] == target["id"]
        )
        mutate(control)
        return victim

    def tampered_range(control: dict) -> None:
        control["range"] = {"min": -999.0, "max": 999.0}

    def unreadable_record(control: dict) -> None:
        control["evidence"]["value_bounds"]["declared_before"] = {"min": "x", "max": "y"}

    for mutate in (tampered_range, unreadable_record):
        validation = profile_module.validate_profile(corrupted(mutate))
        assert "value_bounds_block_valid" in validation.codes(), mutate.__name__
        assert validation.rebuild_required is True, mutate.__name__

    directory = tmp_path / "cache"
    cache = cache_module.ProfileCache(provider_id=provider.id, directory=directory)
    assert cache.save(key, profile) and cache.path_for(key).exists()

    entry = json.loads(cache.path_for(key).read_text(encoding="utf-8"))
    entry["profile"] = corrupted(tampered_range)
    entry["content_sha256"] = cache_module.profile_content_hash(entry["profile"])
    entry["counts"] = cache_module.profile_counts(entry["profile"])
    cache.path_for(key).write_text(json.dumps(entry), encoding="utf-8", newline="\n")

    loaded = cache.load(key, identity=dict(FIXTURE_IDENTITY))
    assert loaded.status == "refused" and loaded.profile is None
    assert loaded.refusal.rule == "profile_invalid", loaded.refusal.rule
    assert cache.status.quarantined


def test_the_revision_is_r14():
    # r13 -> r14 (P38): the preset library (`preset.py`, `catalog.py`) and the codec's
    # measured write laws (growth-budgeted batching, writable labels, the sample
    # block's reload-delay verification). The revision is a pin on purpose: the
    # semantics changed, so the key must move with them (07 §3.4).
    assert VitalProvider.implementation_revision == "vital-provider-2026.10-r14"
