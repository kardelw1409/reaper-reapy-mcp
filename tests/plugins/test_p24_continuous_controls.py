"""P24 tests, workstream A: the declared enums that are continuous ramps.

P23 measured that 20 ``enum_index`` declarations have **no band structure** - the
host prints a value between adjacent members at every transition - and recorded the
finding as `no_band_structure` while keeping the 257-member lists. The AI therefore
still had to name one of 257 strings to set a continuous level, and `lfo_5_phase`
still carried no range at all. This round answers the declaration from the sweep:

* the sweep records a display at **every step it walked** (the file says so:
  ``steps: 256``), so a control whose display changed at all 257 positions is a ramp
  sampled on that grid, and one that changed at 49 of them is a banded member set
  (``pitch_bend_range`` - whose bands the artifact measured exactly);
* the fit is judged at the position the display was really produced at
  (``_exact_sampled_positions``), not at the six-decimal value the file stores, and
  the budget carries half a float32 ulp, because the plugin prints a 32-bit float;
* with both, the four ``random_N_frequency`` rows - 16 octaves of *period* across
  the range, the "ratio law" P23 recorded as refused - derive too.

What the round does **not** claim: that any of these controls is continuous between
the sampled positions. That is a question for the host's own formatter, and the
artifact's ``continuous_display`` measurement (when present) is the evidence that
answers it; the P24 probe measures the same thing live, at 64 points per control
including values no sweep sampled.
"""

from __future__ import annotations

import math
import struct

import pytest

from reaper_reapy_mcp.plugins import cache as cache_module
from reaper_reapy_mcp.plugins import profile as profile_module
from reaper_reapy_mcp.plugins import values as V
from reaper_reapy_mcp.plugins.errors import InvalidValue
from reaper_reapy_mcp.plugins.providers.vital import enum_boundaries as EB
from reaper_reapy_mcp.plugins.providers.vital import profile_build as PB
from reaper_reapy_mcp.plugins.providers.vital import source_facts as SF
from reaper_reapy_mcp.plugins.providers.vital.provider import VitalProvider

from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"
REGISTRY_DIR = FIXTURES / "vital_registry"
ARTIFACT_FIXTURE = FIXTURES / "vital_enum_boundaries" / "vital_enum_boundaries.json"

IDENTITY = {
    "fx_name": PB.VITAL_FX_NAME,
    "class_id": PB.VITAL_CLASS_ID,
    "module": PB.VITAL_MODULE,
    "param_count": 2858,
}
FIXTURE_IDENTITY = {**IDENTITY, "param_count": 8}

#: The measured set: every declared ``enum_index`` whose display changed at all 256
#: swept steps. Pinned, so a new conversion is a deliberate edit here.
RAMPS = (
    "distortion_filter_blend", "effect_chain_order", "filter_1_blend", "filter_1_drive",
    "filter_2_blend", "filter_2_drive", "filter_fx_blend", "filter_fx_drive",
    "lfo_1_phase", "lfo_2_phase", "lfo_3_phase", "lfo_4_phase", "lfo_5_phase",
    "lfo_6_phase", "lfo_7_phase", "lfo_8_phase", "mod_wheel", "reverb_delay",
    "reverb_pre_low_cutoff", "sample_transpose_quantize",
)

#: The ratio-law rows: the display is the *period* of an exponential frequency.
RATIOS = {
    "random_1_frequency": 16.0,
    "random_2_frequency": 16.0,
    "random_3_frequency": 16.0,
    "random_4_frequency": 16.0,
    "delay_frequency": 11.0,
    "delay_aux_frequency": 11.0,
    "phaser_frequency": 7.0,
}


def profile_from(directory) -> dict:
    """A complete profile, built the way production builds it (provenance filled)."""
    registry = PB.load_registry(directory=str(directory))
    provider = VitalProvider(registry=registry)
    return cache_module.build_capability_profile(provider, dict(IDENTITY)).profile


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


def sweep(states: list[tuple[float, str]], steps: int | None = 256):
    return PB._sampled_ramp(states, steps)


def ramp_states(count: int = 257) -> list[tuple[float, str]]:
    """A sweep that changed at every step: a linear ramp printed plainly."""
    return [(round(index / (count - 1), 6), "%.6g" % (2.0 * index / (count - 1))) for index in range(count)]


# ---------------------------------------------------------------------------
# 1. The predicate: a ramp changed at every step it walked
# ---------------------------------------------------------------------------


def test_a_display_that_changed_at_every_step_is_a_sampling_grid():
    evidence = sweep(ramp_states())
    assert evidence is not None
    assert evidence["sampled_positions"] == 257 and evidence["sweep_steps"] == 256
    assert evidence["direction"] == "increasing"
    assert evidence["distinct_values"] == 257
    assert evidence["smallest_measured_step"] > 0.0


def test_a_ladder_that_held_still_between_changes_is_a_member_list():
    """`pitch_bend_range`, measured: 49 integers over a 256-step sweep."""
    states = [(round(index / 48.0, 6), "%d semitones" % index) for index in range(49)]
    assert sweep(states) is None, "the display held still between changes: a member set"


def test_a_repeated_label_is_a_band_and_not_a_grid():
    states = [(0.0, "Off"), (0.25, "Off"), (0.5, "On"), (1.0, "On")]
    assert sweep(states) is None


def test_a_non_numeric_display_declines():
    states = [(index / 256, "mode %d" % index) for index in range(257)]
    assert sweep(states) is None


def test_a_swap_that_is_not_monotone_declines():
    states = [(0.0, "0"), (0.25, "2"), (0.5, "1"), (0.75, "3"), (1.0, "4")]
    assert sweep(states) is None


def test_an_unknown_sweep_length_declines():
    """Without the sampling plan the same numbers could be a 257-member enum."""
    assert sweep(ramp_states(), None) is None


def test_the_predicate_reports_what_the_sweep_sampled_when_the_print_is_integral():
    states = [(index / 256, "%d" % index) for index in range(257)]
    evidence = sweep(states)
    assert evidence is not None
    sampled = evidence["sampled_display"]
    assert sampled["printed_values_are_integers"] is True
    assert sampled["smallest_sampled_step"] == 1.0
    # and it does not claim a *display* quantum the sweep cannot see
    assert "display_step" not in evidence
    assert "cannot see below" in sampled["note"]


def test_a_descending_ramp_carries_its_direction():
    states = [(index / 256, "%.6g" % (2.0 - 2.0 * index / 256)) for index in range(257)]
    evidence = sweep(states)
    assert evidence is not None and evidence["direction"] == "decreasing"


def test_the_derived_law_for_a_ramp_is_numeric_and_carries_the_fit_budget():
    states = ramp_states()
    row = {"kind": "enum_index", "state_key": "x", "name": "x"}
    derived = PB.derive_measured_law(row, states)
    assert derived is not None and derived["kind"] == "affine"
    params = derived["params"]
    assert params["measured_slope"] == pytest.approx(2.0, abs=1e-6)
    assert params["fit_residual_ratio"] <= 1.0
    assert params["position_model"].startswith("the sweep's own uniform grid")
    # the digit-for-digit model is carried, and states it may finish below the count
    assert params["print_model"]["states_compared"] == 257
    assert "not exact to its last digit" in params["print_model"]["note"]


# ---------------------------------------------------------------------------
# 2. Where the display was really produced: exact positions + float32 storage
# ---------------------------------------------------------------------------


def test_a_uniform_grids_positions_are_recovered_exactly():
    entries = [(round(index / 256, 6), 1.0, 0.5) for index in range(257)]
    exact = PB._exact_sampled_positions(entries)
    assert exact is not None
    assert [position for position, _value, _p in exact] == [index / 256 for index in range(257)]


def test_change_points_that_are_not_a_grid_stay_as_recorded():
    """`pitch_bend_range`, measured: its change points are crossings, not a grid."""
    positions = [0.0, 0.011719, 0.03125, 0.054688, 0.074219, 0.09375, 0.117188, 0.136719]
    entries = [(position, float(index), 0.5) for index, position in enumerate(positions)]
    assert PB._exact_sampled_positions(entries) is None


def test_float32_ulp_is_the_real_spacing():
    assert PB._float32_ulp(1.0) == pytest.approx(2.0**-23, rel=1e-12)
    assert PB._float32_ulp(128.0) == pytest.approx(2.0**-16, rel=1e-12)
    # the term the budget carries is half of that
    assert PB._float32_storage_term([128.0]) == pytest.approx(0.5 * 2.0**-16, rel=1e-12)
    # and the float32 rounding is what the print sees
    assert PB._float32(0.1) != 0.1 and PB._g6(PB._float32(0.1)) == "0.1"


# ---------------------------------------------------------------------------
# 3. The twenty conversions, on the real registry
# ---------------------------------------------------------------------------


def test_exactly_the_twenty_ramps_became_numbers(real_profile):
    converted = {
        str(control["id"])
        for control in real_profile["controls"]
        if (control.get("evidence") or {}).get("enum_declaration_overridden")
    }
    assert converted == set(RAMPS), sorted(converted ^ set(RAMPS))
    for control_id in RAMPS:
        control = by_id(real_profile, control_id)
        assert control["type"] == "number", control_id
        assert control["enum_values"] is None, control_id
        # P25: two of the twenty are integer ladders (`ValueDetails::kIndexed`, whose
        # engine rounds every set): they are discrete and carry the storage grid the
        # core snaps both conversion directions onto. The other eighteen are ramps.
        if str(control_id) in SF.INTEGER_LADDERS:
            assert control["discrete"] is True, control_id
            assert (control["law"]["params"] or {}).get("quantization"), control_id
        else:
            assert control["discrete"] is False, control_id
        assert control["law"]["kind"] in ("affine", "power", "exponential"), control_id
        evidence = control["evidence"]["enum_declaration_overridden"]
        assert evidence["declared_members"] == 257, control_id
        assert evidence["measured_states"] == 257, control_id
        assert evidence["range_from_the_sweep"] == control["range"], control_id
        # what the round does not claim is spelled out where a reader sees the claim
        assert "continuous between" in evidence["what_this_does_not_decide"], control_id


def test_no_control_keeps_a_257_member_list(real_profile):
    huge = [
        control["id"]
        for control in real_profile["controls"]
        if control.get("enum_values") and len(control["enum_values"]) > 49
    ]
    assert huge == [], "a member list that long is a sampling grid, not a member set"


def test_the_two_other_numeric_declarations_keep_their_members(real_profile):
    """`oversampling` (4 states) and `pitch_bend_range` (49) changed at 49 of 256 steps."""
    for control_id, members in (("oversampling", 4), ("pitch_bend_range", 49)):
        control = by_id(real_profile, control_id)
        assert control["type"] == "enum", control_id
        assert control["law"]["kind"] == "enum_index", control_id
        assert len(control["enum_values"]) == members, control_id
        assert (control.get("evidence") or {}).get("enum_declaration_overridden") is None, control_id


def test_the_converted_controls_convert_both_ways_and_are_monotone(real_profile):
    checked = 0
    for control_id in RAMPS:
        control = by_id(real_profile, control_id)
        grid = (control["law"].get("params") or {}).get("quantization")
        step = float(grid["step"]) if isinstance(grid, dict) else 0.0
        origin = float(grid.get("origin", 0.0)) if isinstance(grid, dict) else 0.0
        previous = None
        for step_index in range(65):
            norm = step_index / 64
            native = V.normalized_to_native(control, norm)
            assert math.isfinite(float(native)), (control_id, norm)
            low, high = control["range"]["min"], control["range"]["max"]
            back = V.to_normalized(control, native)
            # a ladder cannot round trip to the identity (the engine holds a step);
            # every other converted ramp must, to within a millionth
            tolerance = 1e-6 + (0.5 * step / (high - low) if step > 0.0 else 0.0)
            assert abs(float(back) - norm) < tolerance, (control_id, norm, back)
            if step > 0.0:
                # P25: what comes back must be a value the engine can actually hold
                index = (float(native) - origin) / step
                assert abs(index - round(index)) < 1e-9, (control_id, norm, native)
            assert low - 1e-9 <= float(native) <= high + 1e-9, (control_id, norm, native)
            if previous is not None:
                assert float(native) >= previous - 1e-9, (control_id, norm)
            previous = float(native)
            checked += 1
    assert checked == 20 * 65


def test_the_converted_controls_hit_round_values_at_the_measured_points(real_profile):
    """Independent points, not the ones a fit would be tempted to use."""
    expected = {
        "filter_1_blend": (0.5, 1.0, 1e-4),
        "filter_1_drive": (0.5, 10.0, 1e-3),
        "reverb_delay": (0.5, 0.15, 1e-6),
        "reverb_pre_low_cutoff": (0.5, 64.0, 1e-3),
        "lfo_1_phase": (0.5, 0.5, 1e-6),
        "lfo_3_phase": (0.25, 0.25, 1e-6),
        "mod_wheel": (0.75, 0.75, 1e-6),
        "effect_chain_order": (0.5, 181440.0, 1.0),
        "sample_transpose_quantize": (0.5, 4096.0, 1.0),
        "distortion_filter_blend": (0.25, 0.5, 1e-4),
    }
    for control_id, (norm, value, tolerance) in expected.items():
        control = by_id(real_profile, control_id)
        got = float(V.normalized_to_native(control, norm))
        assert abs(got - value) <= tolerance, (control_id, norm, got, value)


def test_a_write_outside_the_measured_range_is_refused(real_profile):
    for control_id in RAMPS:
        control = by_id(real_profile, control_id)
        high = float(control["range"]["max"])
        with pytest.raises(InvalidValue):
            V.display_to_write_space(control, high * 1.001 + 1e-6)
        with pytest.raises(InvalidValue):
            V.display_to_write_space(control, float(control["range"]["min"]) - 1.0)
        # and the one conversion the write uses is inside the host's space
        plan = V.display_to_write_space(control, high)
        assert plan["normalized"] == pytest.approx(1.0, abs=1e-6), control_id


def test_the_frequency_rows_are_exponential_periods_of_whole_octaves(real_profile):
    for control_id, octaves in RATIOS.items():
        control = by_id(real_profile, control_id)
        assert control["law"]["kind"] == "exponential", control_id
        assert control["law"]["params"].get("direction") == "descending", control_id
        assert control.get("law_candidate") is None, control_id
        low = float(control["range"]["min"])
        high = float(control["range"]["max"])
        assert abs(math.log2(high / low) - octaves) <= 1e-4, (control_id, math.log2(high / low))
        # normalized 0 is the maximum and 1 the minimum, exactly at both ends
        assert float(V.normalized_to_native(control, 0.0)) == high, control_id
        assert float(V.normalized_to_native(control, 1.0)) == low, control_id
    # the 16-octave rows reach a round 0.5 s in the middle of the range
    for control_id in ("random_1_frequency", "random_2_frequency"):
        control = by_id(real_profile, control_id)
        assert float(V.normalized_to_native(control, 0.5)) == pytest.approx(0.5, rel=1e-5), control_id


def test_the_ratio_laws_are_additive_for_the_rows_that_already_derived(real_profile):
    """A row with a usable declared law keeps it (strictly additive).

    `lfo_1_frequency` and `chorus_frequency` declare an affine law with bounds, so
    the sweep is never consulted for them - the descending/exponential derivation of
    P22 did not touch them, and neither does this round. (`osc_1_tune` is a
    *derived* affine row: the registry declares no law for it, so the sweep decides.)
    """
    for control_id in ("lfo_1_frequency", "chorus_frequency"):
        control = by_id(real_profile, control_id)
        assert control["law"]["kind"] == "affine", control_id
        params = control["law"]["params"]
        assert params.get("native_min") is not None, control_id
        assert "measured_slope" not in params, control_id
        assert (control.get("evidence") or {}).get("enum_declaration_overridden") is None
    derived = by_id(real_profile, "osc_1_tune")
    assert "measured_slope" in derived["law"]["params"], "a declared-none row is answered from the sweep"


def test_every_derived_law_is_inside_its_own_instrument_budget(real_profile):
    checked = 0
    for control in real_profile["controls"]:
        params = (control.get("law") or {}).get("params") or {}
        if "fit_residual_ratio" not in params:
            continue
        assert float(params["fit_residual_ratio"]) <= 1.0, (control["id"], params["fit_residual_ratio"])
        assert float(params["fit_bound"]) > 0.0, control["id"]
        terms = params["fit_bound_terms"]
        assert set(terms) == {"from_the_display", "from_the_float32_storage"}, control["id"]
        checked += 1
    assert checked >= 150, checked


def test_integer_sampled_rows_record_the_sampled_step_and_others_do_not(real_profile):
    """A sweep that sampled integers records *that*, never a display quantum.

    The live probe (2026-10-12) falsified the stronger claim: `Oscillator 1 Wave
    Frame`'s samples are the integers 0..256 and the host prints 12.1905 between
    them. What the profile may state is what the sweep saw.
    """
    frame = by_id(real_profile, "osc_1_wave_frame")
    sampled = frame["law"]["params"]["sampled_display"]
    assert sampled["printed_values_are_integers"] is True
    assert sampled["smallest_sampled_step"] == 1.0
    assert frame["law"]["params"]["residual_in_sampled_steps"] < 1e-3
    blend = by_id(real_profile, "filter_1_blend")
    assert "sampled_display" not in blend["law"]["params"], "a six-digit display is not integral"
    for control_id in ("effect_chain_order", "sample_transpose_quantize"):
        control = by_id(real_profile, control_id)
        params = control["law"]["params"]
        # the two ladders: the residual sits at the staircase's own limit, and the
        # smallest *sampled* step is the wide one the sweep used
        assert params["residual_in_sampled_steps"] <= 0.5 + 1e-3, (control_id, params)
        assert params["sampled_display"]["smallest_sampled_step"] > 1.0, control_id


def test_the_print_model_reproduces_the_host_text_for_the_converted_ramps(real_profile):
    """The strongest form of the display model, measured per row.

    On a display that prints fractions, six significant digits of the law's own
    value are usually the host's string - the residual is the acceptance test, this
    count is the statement that the *digits* agree. On a display that quantises to
    1 it cannot be: a fractional prediction is never the printed integer, so those
    rows are judged by `residual_in_display_steps` instead.
    """
    integer_display = {"effect_chain_order", "sample_transpose_quantize"}
    rates = {}
    for control_id in RAMPS:
        params = by_id(real_profile, control_id)["law"]["params"]
        block = params["print_model"]
        assert block["states_compared"] == 257, control_id
        rate = block["states_reproduced"] / block["states_compared"]
        if control_id in integer_display:
            assert params["sampled_display"]["smallest_sampled_step"] > 1.0, control_id
            # at the staircase's own limit: half a *sampled* step, plus the sweep's
            # own position rounding at this slope (measured 0.50004 for the 362879
            # step ladder, 0.498 for the 8192 step one)
            assert params["residual_in_sampled_steps"] <= 0.5 + 1e-3, (
                control_id,
                params["residual_in_sampled_steps"],
            )
        else:
            rates[control_id] = rate
    assert len(rates) == 18
    assert min(rates.values()) >= 0.9, sorted(rates.items(), key=lambda kv: kv[1])[:3]
    assert rates["reverb_pre_low_cutoff"] == 1.0
    # the sixteen phase/mod-wheel rows and the cutoff land within one printed digit
    # of every state; the blends, drives and the reverb delay are a digit or two off
    # on a few states, which is what the residual ratio already says is allowed
    high = {control_id for control_id, rate in rates.items() if rate >= 0.98}
    assert high == {
        "reverb_pre_low_cutoff",
        "lfo_1_phase", "lfo_2_phase", "lfo_3_phase", "lfo_4_phase",
        "lfo_5_phase", "lfo_6_phase", "lfo_7_phase", "lfo_8_phase", "mod_wheel",
    }, sorted(high)


def test_an_unproven_unit_is_not_invented(real_profile):
    assert by_id(real_profile, "effect_chain_order")["unit"] is None
    assert by_id(real_profile, "sample_transpose_quantize")["unit"] is None
    assert by_id(real_profile, "filter_1_drive")["unit"] == "dB"
    assert by_id(real_profile, "reverb_pre_low_cutoff")["unit"] == "semitones"
    assert by_id(real_profile, "lfo_1_phase")["unit"] is None


def test_the_converted_rows_satisfy_the_profile_validator(real_profile):
    validation = profile_module.validate_profile(real_profile)
    assert validation.ok, [problem.message for problem in validation.problems]


# ---------------------------------------------------------------------------
# 4. The artifact cross-check: agreement confirms, a band claim refuses
# ---------------------------------------------------------------------------


def artifact(tmp_path: Path, controls: dict, *, identity: dict | None = None, name: str = "a.json") -> Path:
    import json

    payload = json.loads(ARTIFACT_FIXTURE.read_text(encoding="utf-8"))
    payload["controls"] = controls
    if identity is not None:
        payload["identity"] = identity
    path = tmp_path / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8", newline="\n")
    return path


def boundaries_for(path: Path):
    return EB.load_enum_boundaries(path, identity=dict(FIXTURE_IDENTITY))


def test_the_artifact_confirmation_is_attached_when_the_measurement_agrees(tmp_path):
    measured = VitalProvider(
        registry=PB.load_registry(directory=str(REGISTRY_DIR)),
        enum_boundaries_path=str(ARTIFACT_FIXTURE),
    ).build_profile(dict(FIXTURE_IDENTITY))
    blend = by_id(measured, "distortion_filter_blend")
    confirmation = blend["law"]["params"]["artifact_confirmation"]
    assert confirmation["state"] == "continuous_display"
    assert confirmation["transitions_measured"] == 256
    assert confirmation["declared_members"] == 257
    assert confirmation["profile_model"] == {"type": "number", "law": "affine"}
    assert len(confirmation["artifact"]["sha256"]) == 64
    block = measured["enum_boundaries"]
    assert block["counts"]["controls_not_enums_any_more"] == 1
    assert [row["control"] for row in block["not_enums_any_more"]] == ["distortion_filter_blend"]


def test_an_artifact_claiming_bands_for_a_numeric_control_is_refused_loudly(tmp_path):
    """Two measurements of one control disagreeing must not be resolved silently."""
    payload = __import__("json").loads(ARTIFACT_FIXTURE.read_text(encoding="utf-8"))
    record = payload["controls"]["distortion_filter_blend"]
    # one of the ramp's transitions now *claims* a measured band (the shape a real
    # `exact` transition has), so the artifact and the sweep disagree about the
    # control's structure
    first = record["transitions"][0]
    record["transitions"][0] = {
        "hi": first["hi"],
        "i": first["i"],
        "lo": first["lo"],
        "lower_member": first["lower_member"],
        "state": "exact",
        "upper_member": first["upper_member"],
        "width": 1e-17,
    }
    path = tmp_path / "conflict.json"
    path.write_text(__import__("json").dumps(payload, ensure_ascii=False), encoding="utf-8", newline="\n")
    profile = VitalProvider(
        registry=PB.load_registry(directory=str(REGISTRY_DIR)),
        enum_boundaries_path=str(path),
    ).build_profile(dict(FIXTURE_IDENTITY))
    block = profile["enum_boundaries"]
    refusals = {row["control"]: row for row in block["refused_controls"]}
    assert "distortion_filter_blend" in refusals, refusals
    assert refusals["distortion_filter_blend"]["reason"] == (
        "the_artifact_measured_band_structure_for_a_control_the_profile_models_numerically"
    )
    assert refusals["distortion_filter_blend"]["profile_model"] == {
        "type": "number",
        "law": "affine",
    }
    # fail-closed: the numeric law is untouched and no band geometry is attached
    blend = by_id(profile, "distortion_filter_blend")
    assert "boundary_evidence" not in blend["law"]["params"]
    assert blend["type"] == "number" and blend["law"]["kind"] == "affine"


def artifact_with_states(tmp_path: Path, control_id: str, states: dict[int, str], *, name: str) -> Path:
    """The fixture artifact with chosen transition states (an incomplete measurement)."""
    import json

    payload = json.loads(ARTIFACT_FIXTURE.read_text(encoding="utf-8"))
    record = payload["controls"][control_id]
    for index, state in states.items():
        record["transitions"][index] = {**record["transitions"][index], "state": state}
    path = tmp_path / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8", newline="\n")
    return path


def test_an_artifact_that_measured_nothing_cannot_confirm_the_model(tmp_path: Path):
    """`unresolved` is not agreement, and counting it silently would be a false pass.

    A wholly unmeasured record used to reach the confirmation branch (there were no
    *band* transitions to refuse on) and was then attached with
    ``transitions_measured: 256`` and the note "the host printed a value between
    adjacent members at every measured transition" - while the very same state makes
    an *enum* control a loud refusal (`not_every_transition_is_measured`). The model
    itself comes from the sweep, so it stands; the artifact's confirmation must be
    withheld and named instead.
    """
    record = __import__("json").loads(ARTIFACT_FIXTURE.read_text(encoding="utf-8"))["controls"][
        "distortion_filter_blend"
    ]
    every = {index: "unresolved" for index in range(len(record["transitions"]))}
    path = artifact_with_states(tmp_path, "distortion_filter_blend", every, name="unmeasured.json")
    profile = VitalProvider(
        registry=PB.load_registry(directory=str(REGISTRY_DIR)),
        enum_boundaries_path=str(path),
    ).build_profile(dict(FIXTURE_IDENTITY))

    blend = by_id(profile, "distortion_filter_blend")
    assert "artifact_confirmation" not in blend["law"]["params"]
    assert blend["type"] == "number" and blend["law"]["kind"] == "affine"
    block = profile["enum_boundaries"]
    assert block["counts"]["confirmations_withheld"] == 1
    assert block["counts"]["controls_refused"] == 0, "an unmeasured record is not a contradiction"
    withheld = {row["control"]: row for row in block["confirmations_withheld"]}
    assert withheld["distortion_filter_blend"]["reason"] == (
        "not_every_transition_measured_a_display_between_members"
    )
    assert withheld["distortion_filter_blend"]["measured_continuous"] == 0
    assert withheld["distortion_filter_blend"]["states"] == ["unresolved"]
    row = {entry["control"]: entry for entry in block["not_enums_any_more"]}
    assert row["distortion_filter_blend"]["confirmation"] == "withheld"

    # one transition the measurement could not resolve is enough: the confirmation
    # covers *every* transition or it is not a confirmation
    partial = artifact_with_states(
        tmp_path, "distortion_filter_blend", {0: "unresolved"}, name="partial.json"
    )
    partial_profile = VitalProvider(
        registry=PB.load_registry(directory=str(REGISTRY_DIR)),
        enum_boundaries_path=str(partial),
    ).build_profile(dict(FIXTURE_IDENTITY))
    assert "artifact_confirmation" not in by_id(
        partial_profile, "distortion_filter_blend"
    )["law"]["params"]
    assert partial_profile["enum_boundaries"]["counts"]["confirmations_withheld"] == 1
    assert partial_profile["enum_boundaries"]["counts"]["confirmations_withheld"] == len(
        partial_profile["enum_boundaries"]["confirmations_withheld"]
    )


def test_a_converted_control_without_a_range_is_refused(tmp_path: Path):
    """The row promises "a number with a numeric law and a real range" - so it must.

    `values.to_normalized` falls back to the identity space for a range-less control,
    so a converted control with `range: null` would plan a *display* value as the
    normalized parameter (`display_to_write_space(control, 4095.5)` plans
    `normalized: 4095.5`, measured). Production builds always carry the sweep's range;
    a self-consistent but corrupt entry is exactly what the validator exists for - and
    the kind matters, because the loader consults `rebuild_required`/`cacheable` and
    never `ok`: a plain validation error would leave the entry a cache **hit**.
    """
    import json

    provider = VitalProvider(
        registry=PB.load_registry(directory=str(REGISTRY_DIR)),
        enum_boundaries_path=str(ARTIFACT_FIXTURE),
    )
    profile = cache_module.build_capability_profile(provider, dict(FIXTURE_IDENTITY)).profile
    assert profile_module.validate_profile(profile).ok, "the built profile must validate"

    def corrupted(broken_range) -> dict:
        victim = json.loads(json.dumps(profile))
        by_id(victim, "distortion_filter_blend")["range"] = broken_range
        return victim

    for broken_range in (None, {"min": 1.0, "max": 1.0}, {"min": None, "max": 2.0}):
        validation = profile_module.validate_profile(corrupted(broken_range))
        assert "enum_declaration_overridden_valid" in validation.codes(), broken_range
        assert validation.ok is False, broken_range
        assert validation.rebuild_required is True, broken_range

    # the write path refuses it ...
    material = cache_module.key_material_of(provider, dict(FIXTURE_IDENTITY))
    key = cache_module.compute_cache_key(
        provider_id=str(provider.id),
        identity=dict(FIXTURE_IDENTITY),
        provider_revision=material.provider_revision,
        provider_artifact_digest=material.provider_artifact_digest,
        source_digest=material.source_digest,
    )
    directory = tmp_path / "cache"
    cache = cache_module.ProfileCache(provider_id=str(provider.id), directory=directory)
    assert cache.save(key, profile) and cache.path_for(key).exists(), "a good profile saves"
    written = cache.path_for(key).read_bytes()

    refused = cache.save(key, corrupted(None))
    assert [row.rule for row in refused.refusals], refused.as_dict()
    assert cache.path_for(key).read_bytes() == written, "a refused save must not touch the bytes"

    # ... and the load path quarantines the same corruption when it is already on disk,
    # *even though the entry hashes and counts itself consistently* (the reviewer's
    # scenario: nothing about it is internally inconsistent)
    entry = json.loads(written.decode("utf-8"))
    entry["profile"] = corrupted(None)
    entry["content_sha256"] = cache_module.profile_content_hash(entry["profile"])
    entry["counts"] = cache_module.profile_counts(entry["profile"])
    cache.path_for(key).write_text(json.dumps(entry), encoding="utf-8", newline="\n")

    loaded = cache.load(key, identity=dict(FIXTURE_IDENTITY))
    assert loaded.status == "refused" and loaded.profile is None, loaded.status
    assert loaded.refusal.rule == "profile_invalid", loaded.refusal.rule
    assert cache.status.quarantined, "the refused entry must be quarantined, not served"


# ---------------------------------------------------------------------------
# 5. The change must reach the cache: a warm entry may not outlive the semantics
# ---------------------------------------------------------------------------

#: The revision P23 shipped. Pinned, because the point of the test below is that the
#: revision *moved* when the 20 controls changed meaning.
P23_REVISION = "vital-provider-2026.10-r10"


def test_the_change_moves_the_provider_revision_and_therefore_the_cache_key(tmp_path: Path):
    """A semantics change must reach the cache, or the fix dies with a warm entry.

    ``ProfileCache`` validates a stored entry against *itself* (its own content hash
    and counts) and against the live identity - never against a fresh build - so an
    entry filed under an unchanged key keeps being served after the profile's meaning
    changes. The provider revision is a key determinant (09 §3), so it has to move
    with the semantics; this test pins the mechanism (the revision is in the key
    material and in the key) **and** the consequence (an entry filed under the
    revision P23 shipped is not served to this build - and is not destroyed either).
    """
    registry = PB.load_registry(directory=str(REGISTRY_DIR))
    provider = VitalProvider(registry=registry)
    material = cache_module.key_material_of(provider, dict(FIXTURE_IDENTITY))
    assert material is not None
    assert material.provider_revision == provider.implementation_revision
    assert material.provider_revision != P23_REVISION, (
        "P24 changed the meaning of 20 controls at an unchanged identity; the revision is "
        "the determinant that declares the semantics generation, so it must have moved "
        "(the source digest moves with any code edit, but nothing *states* the change)"
    )

    def key_for(revision: str) -> str:
        return cache_module.compute_cache_key(
            provider_id=provider.id,
            identity=dict(FIXTURE_IDENTITY),
            provider_revision=revision,
            provider_artifact_digest=material.provider_artifact_digest,
            source_digest=material.source_digest,
        )

    current, stale = key_for(material.provider_revision), key_for(P23_REVISION)
    assert current != stale, "the revision is a key determinant (09 §3)"

    directory = tmp_path / "cache"
    cache = cache_module.ProfileCache(provider_id=provider.id, directory=directory)
    assert cache.path_for(current) != cache.path_for(stale)
    stale_path = cache.path_for(stale)
    stale_path.parent.mkdir(parents=True, exist_ok=True)
    stale_path.write_text(
        '{"provider_id": "vital", "note": "an entry the previous revision filed"}\n',
        encoding="utf-8",
        newline="\n",
    )

    # the entry the previous revision filed is neither served nor touched: the current
    # key addresses a different file, so the call is a plain miss (a cold build)
    loaded = cache.load(current, identity=dict(FIXTURE_IDENTITY))
    assert loaded.status == "miss" and loaded.profile is None
    assert stale_path.read_text(encoding="utf-8").startswith('{"provider_id": "vital"')
    assert cache.status.quarantined == [], "invalidation must not move another key's file"


# ---------------------------------------------------------------------------
# 5. The validator: a converted row is a number, and a number carries no members
# ---------------------------------------------------------------------------


def _minimal_profile() -> dict:
    import copy

    profile = {
        "profile_schema_version": 2,
        "provider": "vital",
        "identity": {"plugin_id": "vital"},
        "controls": [],
        "values": {"kinds": list(V.LAW_KINDS)},
        "provenance": {"provider_revision": "1", "provider_artifact_digest": "0" * 64},
        "state": {"capture_scope": {"channels": ["track_state_chunk"]}},
    }
    return copy.deepcopy(profile)


def test_a_number_control_may_not_carry_a_member_list():
    profile = _minimal_profile()
    profile["controls"] = [
        {
            "id": "c",
            "label": "c",
            "type": "number",
            "unit": None,
            "range": {"min": 0.0, "max": 1.0},
            "discrete": False,
            "enum_values": [{"id": "a", "label": "a"}, {"id": "b", "label": "b"}],
            "aliases": [],
            "authority": {"read": "parameter", "write": "parameter"},
            "binding": {"index": 1, "ident": None, "state_key": "c"},
            "law": {"kind": "affine", "params": {"scale": 1.0, "offset": 0.0}},
            "evidence": {"strength": "strong"},
        }
    ]
    problems = [p.rule for p in profile_module.validate_profile(profile).problems]
    assert "number_control_shape_valid" in problems


def test_a_number_control_may_not_carry_enum_geometry():
    profile = _minimal_profile()
    profile["controls"] = [
        {
            "id": "c",
            "label": "c",
            "type": "number",
            "unit": None,
            "range": {"min": 0.0, "max": 1.0},
            "discrete": False,
            "enum_values": None,
            "aliases": [],
            "authority": {"read": "parameter", "write": "parameter"},
            "binding": {"index": 1, "ident": None, "state_key": "c"},
            "law": {"kind": "enum_index", "params": {"positions": [0.0, 1.0]}},
            "evidence": {"strength": "strong"},
        }
    ]
    problems = [p.rule for p in profile_module.validate_profile(profile).problems]
    assert "number_control_shape_valid" in problems


def test_a_malformed_conversion_block_is_a_problem():
    profile = _minimal_profile()
    profile["controls"] = [
        {
            "id": "c",
            "label": "c",
            "type": "number",
            "unit": None,
            "range": {"min": 0.0, "max": 1.0},
            "discrete": False,
            "enum_values": None,
            "aliases": [],
            "authority": {"read": "parameter", "write": "parameter"},
            "binding": {"index": 1, "ident": None, "state_key": "c"},
            "law": {"kind": "affine", "params": {"scale": 1.0, "offset": 0.0}},
            "evidence": {"strength": "strong", "enum_declaration_overridden": {"declared_kind": "affine"}},
        }
    ]
    problems = [p.rule for p in profile_module.validate_profile(profile).problems]
    assert "enum_declaration_overridden_valid" in problems
