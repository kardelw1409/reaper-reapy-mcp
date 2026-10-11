"""P19 — the filter-cutoff value space, pinned as a measurement (offline).

**Why this exists.** The registry's `Filter N Cutoff` rows declare `unit:
"semitones"` (the measured display strings end in " semitones"), and the
*parameter's* display really is semitones relative to MIDI 60 (261.63 Hz) - but
the **value space the profile accepts is the plugin's own document value**, which
is that display **+ 60** (`vital_calibration` 2026-10-11: the host-normalized
0.5 read back 72.0 = 8 + 0.5 x 128, and the plugin displayed -10 semitones for
50.0). A caller who reads "semitones" and passes a semitone value therefore gets
an `InvalidValue` (outside 8..136) rather than a filter two octaves high - but a
caller who passes a *Hz-looking* number (50, 86, 96, as the previous recipe did)
gets a filter two octaves too high and no warning at all: 50 is 146.8 Hz, not
50 Hz.

The tests below pin the relation the recipe must use, from the registry's own
measured sweep: `document_value = display_semitones + 60`, and
`Hz = 261.63 * 2 ** (display / 12)`.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from reaper_reapy_mcp.plugins.providers.vital import (
    DEFAULT_REGISTRY_DIR,
    VitalProvider,
    load_registry,
)

FIXTURE_REGISTRY = Path(__file__).resolve().parent / "fixtures" / "vital_registry"
IDENTITY = {
    "fx_name": "VST3i: Vital (Vital Audio)",
    "class_id": "56535456697461766974616C00000000",
    "module": "Vital.vst3",
    "param_count": 2858,
    "version": "1.5.5",
}

#: The measured offset between the document value and the displayed semitones.
DISPLAY_OFFSET = 60.0

#: The pitch the display is relative to (Vital's own docs, confirmed by the
#: calibration: displayed 0 = 262 Hz).
MIDI60_HZ = 261.6255653005986


def profile_from(directory: Path) -> dict:
    try:
        registry = load_registry(directory=str(directory))
    except Exception as exc:  # noqa: BLE001 - an absent registry is a skip
        pytest.skip(f"the registry is not present: {exc}")
    return VitalProvider(registry=registry).build_profile(dict(IDENTITY))


def sweep(directory, name: str) -> dict:
    """The registry's own recorded sweep for one control (``directory`` may be a
    :class:`~pathlib.Path` - the fixture - or the shipping registry's ``str``)."""
    import json

    data = json.loads((Path(directory) / "vital_scales_full.json").read_text(encoding="utf-8"))
    return data["scales"][name]


#: The three cutoffs whose display is semitones relative to MIDI 60. Deliberately
#: explicit: other `*_cutoff` controls (the chorus/delay/reverb filters) display
#: raw document values, so selecting by suffix would sweep a control whose space
#: is not this one and make the check say the wrong thing.
FILTER_SEMITONE_CUTOFFS = ("filter_1_cutoff", "filter_2_cutoff", "filter_fx_cutoff")


def assert_range_is_display_plus_offset(profile: dict, directory, control_id: str) -> None:
    control = next((row for row in profile["controls"] if row["id"] == control_id), None)
    assert control is not None, f"{control_id} is not in the profile"
    states = sweep(directory, control["label"])["states"]
    lowest, highest = float(states[0][1].split()[0]), float(states[-1][1].split()[0])
    assert control["range"]["min"] == pytest.approx(lowest + DISPLAY_OFFSET, abs=0.5), (lowest, control["range"])
    assert control["range"]["max"] == pytest.approx(highest + DISPLAY_OFFSET, abs=0.5), (highest, control["range"])
    assert control["unit"] == "semitones"


def test_the_declared_range_is_the_measured_display_plus_the_offset():
    """The range and the display are one relation apart - a probe on the
    registry's own sweep, not a re-typed constant.

    The fixture registry is deliberately narrow, so this exercises the relation
    on the filter cutoffs it carries and the shipping-registry test below covers
    all three. The fixture's set is asserted to be **non-empty** (a fixture that
    lost the row must not turn this into a vacuous pass).
    """
    profile = profile_from(FIXTURE_REGISTRY)
    ids = [row["id"] for row in profile["controls"] if row["id"] in FILTER_SEMITONE_CUTOFFS]
    assert ids, f"the fixture registry carries none of {FILTER_SEMITONE_CUTOFFS} - this check would be vacuous"
    for control_id in ids:
        assert_range_is_display_plus_offset(profile, FIXTURE_REGISTRY, control_id)


def test_the_recipe_arithmetic_from_hz_to_the_document_value():
    """What the recipe must compute, pinned so a wrong unit cannot look right.

    60 Hz is the sub's filter target; the arithmetic is
    `document = 60 + 12 * log2(Hz / 261.625565)` (the inverse of the measured
    display law). The numbers the previous recipe used (50/86/96) are shown to be
    two or more octaves high in the same arithmetic - 50 is 146.8 Hz, not 50 Hz.
    """
    def document_for(hz: float) -> float:
        return DISPLAY_OFFSET + 12.0 * math.log2(hz / MIDI60_HZ)

    def hz_for(document: float) -> float:
        return MIDI60_HZ * 2 ** ((document - DISPLAY_OFFSET) / 12.0)

    assert document_for(60.0) == pytest.approx(34.5, abs=0.1)
    assert hz_for(document_for(60.0)) == pytest.approx(60.0, abs=1e-6)
    # the previous recipe's values, read in the same arithmetic: the required
    # document value is more than an octave below them, and 96 (three octaves
    # above MIDI 60) is presence, not a sub
    assert 60.0 * 2 ** 1.2 < hz_for(50.0) < 60.0 * 2 ** 1.6, hz_for(50.0)
    assert 60.0 * 2 ** 4.2 < hz_for(86.0) < 60.0 * 2 ** 4.8, hz_for(86.0)
    assert hz_for(96.0) == pytest.approx(8.0 * MIDI60_HZ, rel=1e-9), hz_for(96.0)
    assert document_for(60.0) + 15.0 < 50.0


def test_the_relation_holds_on_the_shipping_registry_for_all_three_filters():
    profile = profile_from(DEFAULT_REGISTRY_DIR)
    for control_id in FILTER_SEMITONE_CUTOFFS:
        assert_range_is_display_plus_offset(profile, DEFAULT_REGISTRY_DIR, control_id)
