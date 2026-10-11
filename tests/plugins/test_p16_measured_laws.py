"""P16 — laws derived from the control's own measured sweep (2026-10-11).

**The defect this closes (measured).** Every oscillator control in the registry
carried `"how": "rule"`, `"kind": "none"` and null native bounds, because the
correspondence generator never consulted the sweep that measures them. The
capability profile therefore declared `law: none`, `values.to_native` refused
every conversion, and `plugin_write` could not address **any** oscillator control
- 90 of them, including the oscillator switch, level, transpose, wave frame and
distortion amount a bass patch needs. Independent audit of this change (a GLM
subagent, 2026-10-11) reproduced the refusal and found four blockers in the first
plan; each has a test below:

* ``Oscillator N Stack Style`` has 13 labels of which two start with a digit
  (``"2x Octave"``), so a per-state numeric filter let it into the numeric path
  and produced a ``power`` law with ``exponent = 0.0`` -> ``ZeroDivisionError``;
  the derivation now requires **every** display to parse, or takes the enum path.
* the enum bands of ``Oscillator N Distortion Type`` are **not** uniform (switch
  points 0, 11/256, 32/256, 54/256 ... ), and the uniform ``index / (n - 1)``
  conversion landed 5 of 11 members in the neighbouring band **without any
  error**; the derivation stores each member's measured band midpoint instead.
* three controls display *descending* values (Chorus Delay 1, Phaser Frequency,
  Delay Frequency 2): the range/spine model in ``values.py`` cannot state that
  direction, so they stay refused rather than being written inverted.
* the derivation must also set `type`/`enum_values`/`discrete` (a derived enum on
  a `number` control is refused by `validate` before the law is consulted), and a
  new law kind must be declared in both `values.LAW_KINDS` and
  ``profile.LAW_KINDS``.

Everything here is offline; the real-registry tests skip when the measured
registry is absent, and the fixture tests run always.
"""

from __future__ import annotations

import copy
import json
import math
from pathlib import Path

import pytest

from reaper_reapy_mcp.plugins import cache as cache_module
from reaper_reapy_mcp.plugins import profile as profile_module
from reaper_reapy_mcp.plugins import values as V
from reaper_reapy_mcp.plugins.errors import InvalidValue, NoConversion, PluginError
from reaper_reapy_mcp.plugins.providers.vital import (
    DEFAULT_REGISTRY_DIR,
    VitalProvider,
    load_registry,
)
from reaper_reapy_mcp.plugins.providers.vital import depth as depth_module

FIXTURES = Path(__file__).resolve().parent / "fixtures"
REGISTRY_DIR = FIXTURES / "vital_registry"

IDENTITY = {
    "fx_name": "VST3i: Vital (Vital Audio)",
    "class_id": "56535456697461766974616C00000000",
    "module": "Vital.vst3",
    "param_count": 2858,
    "version": "1.5.5",
}


def profile_from(directory: str | Path) -> dict:
    """A **complete** profile: the Core fills the provenance the validator needs.

    ``build_profile`` alone leaves ``provider_revision``/``artifact_digest`` empty
    on purpose (AUD-008: a missing determinant refuses the cache write, never the
    profile), so a schema-valid profile is built the way production builds it.
    """
    registry = load_registry(directory=str(directory))
    provider = VitalProvider(registry=registry)
    return cache_module.build_capability_profile(provider, dict(IDENTITY)).profile


@pytest.fixture(scope="module")
def fixture_profile() -> dict:
    return profile_from(REGISTRY_DIR)


@pytest.fixture(scope="module")
def real_profile() -> dict:
    try:
        load_registry(directory=str(DEFAULT_REGISTRY_DIR))
    except Exception as exc:  # noqa: BLE001 - an absent registry is a skip
        pytest.skip(f"the measured registry is not present: {exc}")
    return profile_from(DEFAULT_REGISTRY_DIR)


def by_id(profile: dict, control_id: str) -> dict:
    for control in profile["controls"]:
        if control["id"] == control_id:
            return control
    raise AssertionError(f"control {control_id!r} is not in the profile")


# ---------------------------------------------------------------------------
# 1. The measured taper, pinned on the fixture registry (always runs)
# ---------------------------------------------------------------------------


def test_the_fixture_registry_derives_the_measured_power_taper(fixture_profile):
    """`unison detune` measures `display = 100 * normalized ** 2` - and says so."""
    control = by_id(fixture_profile, "osc_1_unison_detune")
    assert control["law"]["kind"] == "power", control["law"]
    params = control["law"]["params"]
    assert params["base"] == 100.0
    assert abs(params["exponent"] - 2.0) < 1e-4, params
    assert control["range"] == {"min": 0.0, "max": 100.0}
    assert control["type"] == "number"
    assert params["source"].startswith("vital_scales_full.json")
    # the measured conversion, not a linear one: 25 % sits at normalized 0.5
    assert V.to_normalized(control, 25.0) == pytest.approx(0.5, abs=2e-6)
    assert V.normalized_to_native(control, 0.5) == pytest.approx(25.0, abs=1e-4)
    # round-trip
    for value in (0.0, 1.0, 50.0, 100.0):
        assert V.from_native(control, V.normalized_to_native(control, V.to_normalized(control, value))) == pytest.approx(
            value, abs=1e-3
        )


def test_the_percent_trap_still_fires_after_derivation(fixture_profile):
    """A derived law must not disarm the `%` trap (audit requirement)."""
    control = by_id(fixture_profile, "osc_1_unison_detune")
    assert control["unit"] == "%"
    with pytest.raises(InvalidValue):
        V.to_native(control, 1.0)
    assert V.to_native(control, 0.0) == 0.0


def test_a_derived_enum_is_a_real_enum_control(real_profile):
    """`type`/`enum_values`/`discrete` must move with the law, or `validate`
    refuses every label before the law is consulted (audit blocker D4)."""
    for control_id in ("osc_1_distortion_type", "osc_1_stack_style", "osc_1_on"):
        control = by_id(real_profile, control_id)
        assert control["type"] == "enum", control_id
        assert control["discrete"] is True, control_id
        assert len(control["enum_values"]) >= 2, control_id
        assert control["law"]["kind"] == "enum_index", control_id
        validation = profile_module.validate_profile(real_profile)
        assert validation.ok, [problem.message for problem in validation.problems]


# ---------------------------------------------------------------------------
# 2. The derivation reads the measurement (and invents nothing)
# ---------------------------------------------------------------------------


def _copy_registry(tmp_path: Path) -> Path:
    target = tmp_path / "registry"
    target.mkdir()
    for source in REGISTRY_DIR.iterdir():
        if source.is_file():
            (target / source.name).write_bytes(source.read_bytes())
    return target


def test_mutating_the_sweep_changes_the_derived_law(tmp_path):
    """If the law were hard-coded or invented, this test could not pass."""
    target = _copy_registry(tmp_path)
    scales_path = target / "vital_scales_full.json"
    data = json.loads(scales_path.read_text(encoding="utf-8"))
    name = "Oscillator 1 Unison Detune"
    original = data["scales"][name]
    assert original["states"], "the fixture must measure this control"
    # replace the quadratic taper with a straight line 0 -> 100
    steps = len(original["states"])
    original["states"] = [
        [index / (steps - 1), f"{100.0 * index / (steps - 1):.6g}"] for index in range(steps)
    ]
    scales_path.write_text(json.dumps(data), encoding="utf-8")

    control = by_id(profile_from(target), "osc_1_unison_detune")
    assert control["law"]["kind"] == "affine", control["law"]
    assert control["law"]["params"]["measured_slope"] == pytest.approx(100.0, abs=0.5)
    assert V.to_normalized(control, 25.0) == pytest.approx(0.25, abs=1e-3)


def test_removing_the_measurement_leaves_the_control_refused(tmp_path):
    """No measurement, no law: the refusal is the honest answer, not a default."""
    target = _copy_registry(tmp_path)
    scales_path = target / "vital_scales_full.json"
    data = json.loads(scales_path.read_text(encoding="utf-8"))
    del data["scales"]["Oscillator 1 Unison Detune"]
    scales_path.write_text(json.dumps(data), encoding="utf-8")

    control = by_id(profile_from(target), "osc_1_unison_detune")
    assert control["law"]["kind"] == "none", control["law"]
    assert control["law"]["params"] == {}
    with pytest.raises(NoConversion):
        V.to_native(control, 25.0)


# ---------------------------------------------------------------------------
# 3. The enum bands are measured, not assumed uniform
# ---------------------------------------------------------------------------


def _bands(states: list[tuple[float, str]]) -> dict[str, tuple[float, float]]:
    """Each label's own normalized band, from the measured change points."""
    points: list[tuple[float, str]] = []
    for norm, display in sorted(states, key=lambda pair: pair[0]):
        label = str(display).strip()
        if points and points[-1][1] == label:
            continue
        points.append((float(norm), label))
    bands: dict[str, tuple[float, float]] = {}
    for index, (start, label) in enumerate(points):
        if index + 1 < len(points):
            end = points[index + 1][0]
        else:
            width = start - points[index - 1][0] if index else 1.0
            end = min(1.0, start + width)
        bands[label] = (start, end)
    return bands


def test_enum_members_and_positions_follow_the_measured_bands(real_profile):
    registry = load_registry(directory=str(DEFAULT_REGISTRY_DIR))
    scales = registry.scales()
    for control_id, name in (
        ("osc_1_distortion_type", "Oscillator 1 Distortion Type"),
        ("osc_1_stack_style", "Oscillator 1 Stack Style"),
    ):
        control = by_id(real_profile, control_id)
        members = [member["id"] for member in control["enum_values"]]
        positions = control["law"]["params"]["positions"]
        bands = _bands(depth_module.states_of(scales, name))
        assert len(members) == len(positions) == len(bands), control_id
        for member, position in zip(members, positions):
            low, high = bands[member]
            assert low <= position <= high, (control_id, member, position, (low, high))
        assert positions == sorted(positions), control_id


def test_the_distortion_type_members_are_the_measured_order(real_profile):
    """The uniform mapping put 5 of these 11 members in the *neighbouring* band."""
    control = by_id(real_profile, "osc_1_distortion_type")
    assert [member["id"] for member in control["enum_values"]] == [
        "None",
        "Sync",
        "Formant",
        "Quantize",
        "Bend",
        "Squeeze",
        "Pulse",
        "FM <- Osc",
        "FM <- Sample",
        "RM <- Osc",
        "RM <- Sample",
    ]
    positions = control["law"]["params"]["positions"]
    # the measured band of "Quantize" - the uniform law would have written 0.3,
    # which the sweep renders as "Bend"
    assert V.to_normalized(control, V.to_native(control, "Quantize")) == pytest.approx(
        positions[3], abs=1e-9
    )
    assert positions[3] < 0.3, positions


def test_a_label_with_a_numeric_prefix_stays_an_enum(real_profile):
    """`Stack Style` has two labels the leading-number parser reads as 2.0.

    A per-state filter sent this control into the numeric path (audit blocker D1);
    the derivation now requires every display to parse as a number.
    """
    control = by_id(real_profile, "osc_1_stack_style")
    assert control["law"]["kind"] == "enum_index", control["law"]
    assert control["type"] == "enum"
    members = [member["id"] for member in control["enum_values"]]
    assert "2x Octave" in members and "2x Power Chord" in members
    assert len(members) == 13
    # no ZeroDivisionError, no inf/nan, whatever the caller asks for
    for norm in (0.0, 0.25, 0.5, 0.75, 1.0):
        native = V.normalized_to_native(control, norm)
        assert math.isfinite(float(native))
        assert 0.0 <= V.to_normalized(control, native) <= 1.0


# ---------------------------------------------------------------------------
# 4. The whole derived set converts safely, and the honest refusals stay
# ---------------------------------------------------------------------------


def test_every_control_converts_inside_the_unit_interval(real_profile):
    """The sweep that would have caught the `exponent = 0` class of bug."""
    problems: list[str] = []
    for control in real_profile["controls"]:
        kind = control["law"]["kind"]
        if kind in ("none", "unmeasured"):
            assert control["law"]["params"] == {}, control["id"]
            bounds = control["range"] or {}
            probe = 0.0
            if bounds.get("min") is not None and bounds.get("max") is not None:
                probe = (float(bounds["min"]) + float(bounds["max"])) / 2.0
            with pytest.raises(PluginError) as refusal:
                V.to_native(control, probe)
            assert refusal.value.code in ("NoConversion", "InvalidValue"), control["id"]
            continue
        try:
            if control["type"] == "enum":
                for index in range(len(control["enum_values"])):
                    value = V.to_normalized(control, float(index))
                    if not (0.0 <= value <= 1.0) or not math.isfinite(value):
                        problems.append(f"{control['id']} enum index {index} -> {value}")
                for norm in (0.0, 0.5, 1.0):
                    native = V.normalized_to_native(control, norm)
                    if not math.isfinite(float(native)):
                        problems.append(f"{control['id']} back {norm} -> {native}")
                continue
            bounds = control["range"] or {}
            if bounds.get("min") is None or bounds.get("max") is None:
                problems.append(f"{control['id']} numeric law without a range")
                continue
            for norm in (0.0, 0.25, 0.5, 0.75, 1.0):
                native = V.normalized_to_native(control, norm)
                if not math.isfinite(float(native)):
                    problems.append(f"{control['id']} native {norm} -> {native}")
                    continue
                back = V.to_normalized(control, native)
                if not math.isfinite(back) or abs(back - norm) > 1e-6:
                    problems.append(f"{control['id']} round trip {norm} -> {back}")
        except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
            problems.append(f"{control['id']} raised {type(exc).__name__}: {exc}")
    assert problems == [], problems


def test_descending_displays_are_now_measured_laws_and_round_trip(real_profile):
    """4 controls fall as the parameter rises (audit blocker D2) - now **measured**.

    History: the range/spine model expressed one direction only, so a descending
    sweep was refused rather than inverted (writing an inverted range would
    produce a full-band error that the read-back, using the same law, would
    confirm as success). P22 answered that blocker the honest way: the law now
    declares its direction (`law.params.direction = "descending"`) and the fit
    runs in spine space, so the range keeps `min <= max` as numeric extremes.

    Three controls derive; one does not, and the reason is a measurement, not a
    gap: `random_N_frequency`'s exponential is anchored on printed endpoints
    (``0.00195312`` for 2**-9) and lands within 5.7e-6 *relative* of every state
    but outside each state's printed precision at the large end - so it stays
    refused with a `law_candidate` recording the miss.
    """
    controls = {control["id"]: control for control in real_profile["controls"]}
    derived = {
        cid: controls[cid]
        for cid in ("delay_frequency", "phaser_frequency", "delay_aux_frequency")
    }
    for control_id, control in derived.items():
        law = control["law"]
        params = law.get("params") or {}
        assert law["kind"] == "exponential", (control_id, law)
        assert params.get("direction") == "descending", (control_id, params)
        low, high = control["range"]["min"], control["range"]["max"]
        assert low < high, (control_id, control["range"])
        # the spine: normalized 0 is the maximum, normalized 1 the minimum, and the
        # inversion is exact at both ends and in the middle
        assert math.isclose(float(V.normalized_to_native(control, 0.0)), high, rel_tol=1e-9), control_id
        assert math.isclose(float(V.normalized_to_native(control, 1.0)), low, rel_tol=1e-9), control_id
        for norm in (0.0, 0.25, 0.5, 0.75, 1.0):
            native = V.normalized_to_native(control, norm)
            assert abs(V.to_normalized(control, native) - norm) < 1e-6, (control_id, norm)

    # the labelled case: `delay_aux_tempo`'s displays are fractions ("4/1"..."1/64"),
    # which the leading-number parser reads as 4.0/1.0/1.0 - not a numeric scale,
    # so the row takes the enum path with its measured member order
    tempo = controls["delay_aux_tempo"]
    assert tempo["type"] == "enum", tempo["law"]
    assert tempo["law"]["kind"] == "enum_index"
    members = [member["id"] for member in tempo["enum_values"]]
    assert members[:2] == ["4/1", "2/1"] and members[-1] == "1/64", members
    assert len(tempo["law"]["params"]["positions"]) == len(members)

    # the four that remain refused carry a named candidate instead of silence
    for cid in ("random_1_frequency", "random_2_frequency", "random_3_frequency", "random_4_frequency"):
        control = controls[cid]
        assert control["law"]["kind"] == "none", (cid, control["law"])
        candidate = control.get("law_candidate")
        assert candidate and candidate["kind"] == "exponential", (cid, candidate)
        assert candidate["direction"] == "descending", (cid, candidate)
        assert candidate["worst_state"]["ratio"] > 1.0, (cid, candidate)
        # and the candidate must NOT have been installed
        with pytest.raises(V.NoConversion):
            V.to_native(control, float(control["law_candidate"]["range"]["max"]))


# ---------------------------------------------------------------------------
# 5. The change is additive: declared laws are untouched
# ---------------------------------------------------------------------------


def test_declared_laws_are_not_re_derived(real_profile):
    """Rows that already had a *usable* law must keep it, and a bass pass must be exact.

    The one documented exception is a declared affine row whose own bounds cannot
    produce a range (``native_min == native_max``): 42 ``modulation_NN_power`` rows
    carry that data defect, convert as an identity today, and are answered from
    the sweep instead (measured -10..+10).

    Since P18 (2026-10-11) there is a second, narrower exception: a row whose
    declaration was **contradicted by a live host measurement** may be replaced
    by that measurement, and only such a row - the table is
    ``LIVE_MEASURED_CORRECTIONS`` (portamento time/slope, the envelope times,
    ``volume`` and - P19 - ``voice_transpose``, whose corpus range made the
    documented neutral write +10 semitones), each pinned in
    ``test_p18_live_corrections.py``. Everything else here is unchanged: the
    sweep may fill a missing law, never overrule a working one.

    Since P19 (2026-10-11) there is a third, narrower exception of the same kind:
    a declared ``enum_index`` law whose **measured** bands are not uniform gets the
    measured positions attached (``_declared_enum_positions``), and only such a
    row. It keeps the declared *kind* and the member list - only where each member
    sits in the host's 0..1 space changes - because the uniform arithmetic landed
    members in the *neighbouring* band while the layer's own read-back agreed.
    The derivation is re-computed from the registry's sweeps in
    ``test_p19_enum_positions.py`` (this test only requires that each such row
    carries its own measurement, and that no other row does).
    """
    from reaper_reapy_mcp.plugins.providers.vital.profile_build import LIVE_MEASURED_CORRECTIONS

    registry = load_registry(directory=str(DEFAULT_REGISTRY_DIR))
    derived_markers = {"measured_slope", "measured_intercept", "fit_residual", "positions_source"}
    re_derived: set[str] = set()
    corrected: set[str] = set()
    positions_corrected: set[str] = set()
    for row in registry.rows():
        control_id = row.get("state_key")
        if not control_id or row.get("kind") not in ("affine", "enum_index"):
            continue
        control = by_id(real_profile, str(control_id))
        if str(control_id) in LIVE_MEASURED_CORRECTIONS:
            assert control["law"]["kind"] != row["kind"] or control["range"] != _declared_range(row), (
                f"{control_id} is listed as a live-measured correction but is unchanged"
            )
            corrected.add(str(control_id))
            continue
        note = (control.get("evidence") or {}).get("positions_correction")
        if note:
            assert control["law"]["kind"] == row["kind"] == "enum_index", control_id
            assert int(note["members_the_declared_mapping_puts_outside_their_band"]) >= 1, control_id
            assert "vital_scales_full.json" in str(note["positions_source"]), control_id
            assert (control["law"]["params"] or {}).get("positions"), control_id
            positions_corrected.add(str(control_id))
            continue
        assert control["law"]["kind"] == row["kind"], control_id
        if derived_markers & set(control["law"]["params"]):
            assert control["range"] is not None, control_id
            re_derived.add(str(control_id))
        else:
            assert control["range"] == _declared_range(row), control_id
    assert corrected == set(LIVE_MEASURED_CORRECTIONS), sorted(corrected ^ set(LIVE_MEASURED_CORRECTIONS))
    assert positions_corrected == {"filter_1_style", "filter_2_style", "filter_fx_style"}, sorted(
        positions_corrected
    )
    expected_re_derived = {
        str(row["state_key"])
        for row in registry.rows()
        if row.get("state_key")
        and row.get("kind") == "affine"
        and _declared_range(row) is None
    }
    assert re_derived == expected_re_derived, sorted(re_derived ^ expected_re_derived)
    assert len(re_derived) > 0, "non-vacuity: the degenerate-bound rows really exist"
    cutoff = by_id(real_profile, "filter_1_cutoff")
    assert cutoff["law"]["kind"] == "affine"
    assert cutoff["law"]["params"]["native_min"] == 8.0
    assert cutoff["range"] == {"min": 8.0, "max": 136.0}
    style = by_id(real_profile, "filter_1_style")
    assert style["type"] == "enum" and style["discrete"] is True
    assert style["law"]["kind"] == "enum_index", style["law"]
    # the declared *kind* and member list survive; only the measured positions are
    # attached, and only because the declared uniform mapping was measurably wrong
    note = style["evidence"]["positions_correction"]
    assert note["declared_mapping"] == "uniform index / (n - 1)"
    assert note["members_the_declared_mapping_puts_outside_their_band"] == 4, note
    assert style["law"]["params"]["positions"] == [0.03125, 0.125, 0.25, 0.375, 0.5], style["law"]["params"]
    switch = by_id(real_profile, "filter_1_on")
    assert "positions" not in (switch["law"]["params"] or {}), "a switch keeps the uniform law"


def _declared_range(row: dict) -> dict | None:
    low, high = row.get("native_min"), row.get("native_max")
    if isinstance(low, (int, float)) and isinstance(high, (int, float)) and high > low:
        return {"min": float(low), "max": float(high)}
    return None


def test_transpose_endpoints_are_exact(real_profile):
    """The fit's slope must not leak into the conversion (audit blocker D6)."""
    control = by_id(real_profile, "osc_1_transpose")
    assert control["law"]["params"]["scale"] == 1.0
    assert control["law"]["params"]["offset"] == 0.0
    assert control["range"] == {"min": -48.0, "max": 48.0}
    assert V.to_normalized(control, -48.0) == 0.0
    assert V.to_normalized(control, 48.0) == 1.0
    assert V.to_normalized(control, 0.0) == pytest.approx(0.5, abs=2e-3)


# ---------------------------------------------------------------------------
# 6. The new law kind is declared everywhere the contract checks it
# ---------------------------------------------------------------------------


def test_power_is_declared_in_both_contract_surfaces(real_profile):
    assert "power" in V.LAW_KINDS
    assert "power" in profile_module.LAW_KINDS
    assert "power" in real_profile["values"]["kinds"]
    kinds = {control["law"]["kind"] for control in real_profile["controls"]}
    assert "power" in kinds, "the measured taper must actually be used"
    validation = profile_module.validate_profile(real_profile)
    assert validation.ok, [problem.message for problem in validation.problems]
    # non-vacuity: the validator really rejects an unknown kind
    broken = copy.deepcopy(real_profile)
    broken["controls"][0]["law"] = {"kind": "invented", "params": {}}
    assert not profile_module.validate_profile(broken).ok
