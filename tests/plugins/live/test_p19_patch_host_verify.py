"""P19 — the accepted patch, verified through REAPER's own formatter (read-only).

**Why.** An independent review (GLM-5.3 Flash, 2026-10-11) made the sharpest point
of the round: the layer's `ALL_VERIFIED` compares the value it wrote against a
value it read back **through the same conversion**, so a self-consistent wrong
mapping verifies green - which is exactly how `filter_1_style = "24dB"` landed in
"Notch Blend" and still passed. The acceptance artifact had independent legs only
for the enums and the cutoff.

This probe closes that: it reads the *accepted* patch's **numeric** controls through
`TrackFX_GetFormattedParamValue` and checks the parsed number against the intent -
the plugin's own text, not the layer's arithmetic. It writes nothing at all; the
parse rules below are explicit about the unit they expect, because a parser that
accepts anything verifies nothing.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

import pytest

import live_harness as H
import probe_p19_common as C

pytestmark = pytest.mark.live_write

MIDI60_HZ = 261.6255653005986

#: control -> (expected value, unit conversion, tolerance) where the *number in the
#: host's own text* must match the intent. `unit` multiplies the parsed number.
NUMERIC_INTENT: dict[str, tuple[float, float, float]] = {
    "filter_1_cutoff": (58.0, 1.0, 0.02),  # parsed as semitones, converted to Hz below
    "filter_1_resonance": (4.0, 1.0, 0.25),  # percent
    "filter_1_keytrack": (0.0, 1.0, 0.5),
    "osc_1_level": (1.0, 1.0, 0.01),  # the plugin's own text is the fraction ("1"), not a percent
    "osc_2_level": (0.0, 1.0, 0.5),
    "env_2_attack": (0.0005, 1.0, 1e-4),  # the plugin's text is already seconds
    "env_2_decay": (0.06, 1.0, 1e-3),
    "env_2_release": (0.06, 1.0, 1e-3),
    "env_2_sustain": (0.0, 1.0, 0.01),
    "voice_transpose": (0.0, 1.0, 0.1),
    "polyphony": (1.0, 1.0, 0.01),
}
ENUM_INTENT = {
    "osc_2_on": "Off",
    "filter_1_on": "On",
    "filter_1_model": "Analog",
    "filter_1_style": "24dB",
    "filter_1_drive": "0dB",
    "filter_2_on": "Off",
    "filter_2_style": "24dB",
    "osc_1_spectral_morph_type": "None",
    "legato": "Off",
    "portamento_force": "On",
    "voice_priority": "Newest",
    "pitch_bend_range": "0 semitones",
    "distortion_on": "Off",
}
NUMBER = re.compile(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")


def parse_host_number(text: str) -> float | None:
    match = NUMBER.search(str(text))
    return None if match is None else float(match.group(0))


def test_the_accepted_patch_reads_back_in_the_plugins_own_words():
    try:
        import reapy

        project = reapy.Project()
        project.id  # noqa: B018 - reachability probe
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"REAPER is not reachable, so the probe did not run: {exc}")

    os.environ.setdefault("REAPER_MCP_PLUGIN_PROVIDERS", "vital")
    from reapy import reascript_api as RPR
    from reaper_reapy_mcp.server import create_server

    server = create_server(debug=False)
    track_index = C.track_by_name(server)
    instance = {"track_index": track_index, "fx_index": 0}
    track = RPR.GetTrack(0, track_index)

    listing = C.call(server, "plugin_find", {"instance": instance, "kind": "control", "limit": 4000})
    index_by_id = {
        str(row.get("id")): (row.get("binding") or {}).get("index")
        for row in ((listing.get("data") or {}).get("matches") or [])
    }
    report: dict[str, Any] = {
        "probe": "p19_patch_host_verification",
        "utc": H.utc_now(),
        "live_write": False,
        "why": "the layer's own verification runs the same conversion on both legs; this reads the plugin's formatted text instead",
        "numeric": {},
        "enums": {},
    }

    for control_id, (expected, unit, tolerance) in NUMERIC_INTENT.items():
        index = index_by_id.get(control_id)
        assert isinstance(index, int), (control_id, index)
        text = C.host_display(track, index)
        parsed = parse_host_number(text)
        report["numeric"][control_id] = {
            "index": index,
            "host_text": text,
            "parsed": parsed,
            "unit_multiplier": unit,
            "expected_in_that_unit": expected,
            "tolerance": tolerance,
        }
    for control_id, expected in ENUM_INTENT.items():
        index = index_by_id.get(control_id)
        assert isinstance(index, int), (control_id, index)
        report["enums"][control_id] = {"index": index, "host_text": C.host_display(track, index), "expected": expected}

    path = H.write_run_json("p19_patch_host_verification", report, H.EVIDENCE_DIR)
    print(f"evidence: {path}")
    for control_id, row in report["numeric"].items():
        print(f"  {control_id:22s} host {row['host_text']!r:>18} parsed {row['parsed']} (x{row['unit_multiplier']}) vs {row['expected_in_that_unit']}")
    for control_id, row in report["enums"].items():
        print(f"  {control_id:22s} host {row['host_text']!r}")

    # --- assertions: the plugin's own text against the intent ---------------
    for control_id, (expected, unit, tolerance) in NUMERIC_INTENT.items():
        row = report["numeric"][control_id]
        parsed = row["parsed"]
        assert parsed is not None, (control_id, row["host_text"])
        value = parsed * unit
        if control_id == "filter_1_cutoff":
            hz = MIDI60_HZ * 2 ** (value / 12.0)
            assert hz == pytest.approx(58.0, rel=0.02), (control_id, row["host_text"], hz)
            report["numeric"][control_id]["converted"] = {"hz": round(hz, 2), "st": value}
            continue
        assert value == pytest.approx(expected, abs=tolerance), (control_id, row["host_text"], value, expected)
    for control_id, expected in ENUM_INTENT.items():
        assert report["enums"][control_id]["host_text"] == expected, (control_id, report["enums"][control_id])
    assert project.time_selection is not None  # the project object is reachable; nothing was written
