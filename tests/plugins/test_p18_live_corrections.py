"""P18 — profile corrections measured against the live host (2026-10-11).

**The defect this closes (measured live).** The Vital registry's native ranges
come from the *observed preset corpus* (`phase2b_native_ranges.json`): every
range stops at the largest value any preset ever used. A live calibration run -
`set_fx_param` (the classic REAPER tool) wrote known host-normalized values,
REAPER's own `TrackFX_GetFormattedParamValue` reported the plugin's display -
measured the real mappings and found three audible disagreements:

* `Portamento Time` declared -10..-0.476 (0.72 s max), although the parameter
  runs to +4 (16 s). A glide-time request of the (then) declared value -3.3
  landed at **0.9005 s** instead of 0.1 s (the truncated affine law sent it to
  normalized 0.7035; the plugin display read "0.900531 secs"). The live curve is
  the plugin's own exponential base-2 family: `t = 2 ** (-10 + 14 * norm)`,
  measured 0.125 s at 0.5 and 6.06286 s at 0.9.
* `Portamento Slope` declared -4.5..+8; measured -4 at 0.25, +4 at 0.75, +8 at
  1.0 -> the true range is -8..+8.
* Envelope attack/decay/release declared linear 0..2.378 s; measured 0.125 s at
  0.25, 2.00002 s at 0.5, 10.1251 s at 0.75, 32.0003 s at 1.0 -> the plugin
  maps them as `seconds = 32.0003 * norm ** 4` (its kQuartic family). A declared
  "0.35 s decay" rendered **15 ms**.

The fix lives in the provider (`profile_build.LIVE_MEASURED_CORRECTIONS`), only
for rows the calibration measured; everything else keeps its declaration. The
tests below pin the corrected conversions on the real registry (skipped when the
measured registry is absent, like the P16 suite).

The registry file itself was **not** modified: the correction is a provider-side
projection with its evidence named, so the shared POC registry keeps serving the
old system unchanged.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from reaper_reapy_mcp.plugins import values as V
from reaper_reapy_mcp.plugins.errors import InvalidValue
from reaper_reapy_mcp.plugins.providers.vital import DEFAULT_REGISTRY_DIR, VitalProvider, load_registry
from reaper_reapy_mcp.plugins.providers.vital.profile_build import LIVE_MEASURED_CORRECTIONS
from reaper_reapy_mcp.plugins import cache as cache_module

IDENTITY = {
    "fx_name": "VST3i: Vital (Vital Audio)",
    "class_id": "56535456697461766974616C00000000",
    "module": "Vital.vst3",
    "param_count": 2858,
    "version": "1.5.5",
}


@pytest.fixture(scope="module")
def real_profile() -> dict:
    try:
        registry = load_registry(directory=str(DEFAULT_REGISTRY_DIR))
    except Exception as exc:  # noqa: BLE001 - an absent registry is a skip
        pytest.skip(f"the measured registry is not present: {exc}")
    provider = VitalProvider(registry=registry)
    return cache_module.build_capability_profile(provider, dict(IDENTITY)).profile


def by_id(profile: dict, control_id: str) -> dict:
    for control in profile["controls"]:
        if control["id"] == control_id:
            return control
    raise AssertionError(f"control {control_id!r} is not in the profile")


def test_voice_transpose_is_the_displayed_semitone_offset(real_profile):
    """The measured anchors: the corpus range made the neutral read +10 st."""
    control = by_id(real_profile, "voice_transpose")
    assert control["range"] == {"min": -48.0, "max": 48.0}, control["range"]
    assert control["law"]["kind"] == "affine", control["law"]
    # the measured points, in the *corrected* space: the value is the semitones
    assert V.to_normalized(control, 0.0) == pytest.approx(0.5, abs=1e-9)
    assert V.to_normalized(control, 48.0) == pytest.approx(1.0, abs=1e-9)
    assert V.to_normalized(control, -48.0) == pytest.approx(0.0, abs=1e-9)
    assert V.to_normalized(control, -6.0) == pytest.approx(0.4375, abs=1e-9)
    # every measured (written, displayed) pair from the probe's declared space:
    # declared value v displayed 1.6 * v + 9.6 semitones, so in the corrected
    # space the same normalized position must map back to that display
    for written, displayed in ((24.0, 48.0), (0.0, 10.0), (-24.0, -29.0), (12.0, 29.0), (-12.0, -10.0)):
        legacy_normalized = (written + 36.0) / 60.0
        corrected_value = legacy_normalized * 96.0 - 48.0
        assert corrected_value == pytest.approx(displayed, abs=0.5), (written, displayed, corrected_value)


def test_the_correction_table_covers_exactly_the_measured_rows():
    expected = {"portamento_time", "portamento_slope", "volume", "voice_transpose"}
    expected |= {f"env_{i}_{section}" for i in range(1, 7) for section in ("attack", "decay", "release")}
    assert set(LIVE_MEASURED_CORRECTIONS) == expected


def test_portamento_time_is_seconds_on_the_measured_exponential(real_profile):
    control = by_id(real_profile, "portamento_time")
    assert control["law"]["kind"] == "exponential", control["law"]
    assert control["range"] == {"min": 2.0**-10, "max": 2.0**4}, control["range"]
    assert control["unit"] == "secs"
    # the measured anchors: 0.125 s at normalized 0.5; 6.06286 s at 0.9;
    # and the measured endpoints 0.000976562 s at 0.0 / 16 s at 1.0
    assert V.to_normalized(control, 0.125) == pytest.approx(0.5, abs=1e-9)
    assert V.to_normalized(control, 6.06286) == pytest.approx(0.9, abs=1e-4)
    assert V.to_normalized(control, 0.000976562) == pytest.approx(0.0, abs=1e-6)
    assert V.to_normalized(control, 2.0**-10) == pytest.approx(0.0, abs=1e-12)
    assert V.to_normalized(control, 16.0) == pytest.approx(1.0, abs=1e-9)
    # the measured glide request (0.1 s) sits at normalized 0.477, not 0.703
    assert V.to_normalized(control, 0.1) == pytest.approx(0.47702, abs=1e-4)
    # the old convention (a negative log2-seconds value) is a typed refusal now
    with pytest.raises(InvalidValue):
        V.to_native(control, -3.3)
    for value in (2.0**-10, 0.001, 0.1, 1.0, 6.06286, 16.0):
        native = V.normalized_to_native(control, V.to_normalized(control, value))
        assert V.from_native(control, native) == pytest.approx(value, rel=1e-6)


def test_portamento_slope_uses_the_measured_range(real_profile):
    control = by_id(real_profile, "portamento_slope")
    assert control["law"]["kind"] == "affine", control["law"]
    assert control["range"] == {"min": -8.0, "max": 8.0}, control["range"]
    # measured: -4 at 0.25, +4 at 0.75, +8 at 1.0 -> 0 sits at 0.5
    assert V.to_normalized(control, -8.0) == pytest.approx(0.0)
    assert V.to_normalized(control, -4.0) == pytest.approx(0.25)
    assert V.to_normalized(control, 0.0) == pytest.approx(0.5)
    assert V.to_normalized(control, 8.0) == pytest.approx(1.0)


def test_envelope_times_are_seconds_on_the_measured_quartic(real_profile):
    for index in range(1, 7):
        for section in ("attack", "decay", "release"):
            control = by_id(real_profile, f"env_{index}_{section}")
            law = control["law"]
            assert law["kind"] == "power", (index, section, law)
            assert law["params"]["base"] == pytest.approx(32.0003)
            assert law["params"]["exponent"] == pytest.approx(4.0)
            assert control["range"] == {"min": 0.0, "max": 32.0003}, control["range"]
            # the measured anchors, worst-case tolerance 0.05 % of range
            assert V.to_normalized(control, 0.125) == pytest.approx(0.25, abs=1e-4)
            assert V.to_normalized(control, 2.00002) == pytest.approx(0.5, abs=1e-5)
            assert V.to_normalized(control, 10.1251) == pytest.approx(0.75, abs=1e-4)
            assert V.to_normalized(control, 32.0003) == pytest.approx(1.0)
            for seconds in (0.001, 0.05, 0.35, 2.0, 10.1251, 32.0):
                native = V.normalized_to_native(control, V.to_normalized(control, seconds))
                assert V.from_native(control, native) == pytest.approx(seconds, rel=1e-5)


def test_volume_follows_the_measured_square_root(real_profile):
    control = by_id(real_profile, "volume")
    law = control["law"]
    assert law["kind"] == "power", law
    assert law["params"]["base"] == pytest.approx(86.02)
    assert law["params"]["exponent"] == pytest.approx(0.5)
    assert law["params"]["offset"] == pytest.approx(-80.0)
    assert control["range"] == {"min": -80.0, "max": 6.02}, control["range"]
    # the measured anchors: -36.99 dB at 0.25, -19.1747 dB at 0.5, +6.02 at 1.0
    assert V.to_normalized(control, -36.99) == pytest.approx(0.25, abs=1e-4)
    assert V.to_normalized(control, -19.1747) == pytest.approx(0.5, abs=1e-4)
    assert V.to_normalized(control, -5.50449) == pytest.approx(0.75, abs=1e-4)
    assert V.to_normalized(control, 6.02) == pytest.approx(1.0, abs=1e-9)
    assert V.to_normalized(control, -80.0) == pytest.approx(0.0)
    for db in (-80.0, -36.99, -19.1747, -6.02, 0.0, 6.02):
        native = V.normalized_to_native(control, V.to_normalized(control, db))
        assert V.from_native(control, native) == pytest.approx(db, abs=1e-6)


def test_corrections_do_not_leak_into_unmeasured_rows(real_profile):
    """The correction table is measured-row-only: neighbours stay untouched."""
    voice = by_id(real_profile, "voice_priority")
    assert voice["law"]["kind"] == "enum_index"
    assert [row["id"] for row in voice["enum_values"]] == [
        "Newest", "Oldest", "Highest", "Lowest", "Round Robin",
    ]
    # Newest is the determinism pin the old system measured; its normalized 0.0
    assert V.to_normalized(voice, V.to_native(voice, "Newest")) == pytest.approx(0.0)
    # a P16-derived row keeps its measured power law (base 1, exponent 2)
    level = by_id(real_profile, "osc_1_level")
    assert level["law"]["kind"] == "power"
    assert math.isclose(level["law"]["params"]["exponent"], 2.0, rel_tol=1e-4)
