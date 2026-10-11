"""P19 acceptance — the 808 patch: one batch, verified, rendered, measured.

**What is applied.** The shape the P19 arms chose by measurement
(`p19_808_shaping__*.json`, re-derived with the corrected pitch instruments in
`p19_808_shaping_reanalysis__*.json`):

- one oscillator (`osc_2` off), its wavetable morph declared `None` (the morph was
  measured inert on the single-frame Init table, so the recipe must not *rely* on
  it), straight into **Filter 1**;
- Filter 1 = Analog 24 dB at the document value for **58 Hz** (1.4x the phrase's
  lowest note, E1), resonance 4 %, no key track, no drive. Measured on the arms:
  h2 -22 dB and 99.9 % of the energy below 120 Hz, with the two neighbouring
  cutoffs bracketing it (50 Hz: h2 -23.8, 70 Hz: h2 -17.6);
- no distortion: the effects distortion at drive +6 dB / mix 0.35 moved the level
  (+1.6 dB) and *removed* h3, and the filter drive (7.97 dB) traded h2 for h3
  (grit, not body) - neither is the clean sub this instrument is;
- the 808 drop: `env_2 -> osc_1_transpose` with a semantic depth of **12 st**
  (the provider's own measured conversion, 0.124733943), reached through
  `env_2` = instant attack, 60 ms decay, sustain 0, so every note starts high and
  falls to pitch;
- `legato` off with `portamento_force` on: each note retriggers its own drop while
  the glide between overlapping notes stays (the track's whole point);
- the corrected enum mapping is exercised here: `filter_1_style` "24dB" writes
  0.125, the point inside the host's own `[0.0625, 0.1875)` band
  (`p19_enum_bands_through_host__*.json`).

**How it is verified.** Dry run first (a plan that writes nothing), then one apply;
every written control is read back through the layer *and* - for the enums and the
cutoff - through REAPER's own formatter, which is the channel that found the enum
defect. The route is read from the document. Then the phrase window is rendered and
measured.

The patch is left applied: it is the sound the user is asked to judge, and the
pre-patch snapshot is recorded in the artifact so a restore is a recorded
operation rather than a guess.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any

import pytest

import live_harness as H
import probe_p19_common as C

pytestmark = pytest.mark.live_write

MIDI60_HZ = 261.6255653005986
DISPLAY_OFFSET = 60.0
E1_HZ = C.NOTES_HZ[28]
TARGET_CUTOFF_HZ = 58.0
DROP_ST = 12.0
POWER = 0.0

#: The patch: one batch, exactly the controls the shape needs.
PATCH: dict[str, Any] = {
    "osc_1_level": 1.0,
    "osc_1_spectral_morph_type": "None",
    "osc_1_spectral_morph_amount": 0.0,
    "osc_2_on": "Off",
    "osc_2_level": 0.0,
    "filter_1_on": "On",
    "filter_1_model": "Analog",
    "filter_1_style": "24dB",
    "filter_1_cutoff": DISPLAY_OFFSET + 12.0 * math.log2(TARGET_CUTOFF_HZ / MIDI60_HZ),
    "filter_1_resonance": 0.04,
    "filter_1_keytrack": 0.0,
    "filter_1_drive": "0dB",
    "filter_2_on": "Off",
    "filter_2_style": "24dB",
    "legato": "Off",
    "portamento_force": "On",
    "polyphony": 1.0,
    "voice_priority": "Newest",
    "pitch_bend_range": "0 semitones",
    "voice_transpose": 0.0,
    "distortion_on": "Off",
    "env_2_attack": 0.0005,
    "env_2_decay": 0.06,
    "env_2_sustain": 0.0,
    "env_2_release": 0.06,
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

#: The measured P18 A baseline, for the "damped harmonics" comparison.
BASELINE_H2_DB = -6.77
BASELINE_SUB = 0.9562


def test_p19_acceptance_808_patch():
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
    window_start, item = C.paragraph_window(server, track_index)
    instance = {"track_index": track_index, "fx_index": 0}
    track = RPR.GetTrack(0, track_index)

    listing = C.call(server, "plugin_find", {"instance": instance, "kind": "control", "limit": 4000})
    host_index = {
        str(row.get("id")): (row.get("binding") or {}).get("index")
        for row in ((listing.get("data") or {}).get("matches") or [])
    }
    before = C.call(server, "plugin_read", {"instance": instance, "controls": sorted(PATCH), "include_display": True})
    before_values = {
        key: {"native": value.get("native"), "display": value.get("display")}
        for key, value in (before.get("data") or {}).get("values", {}).items()
    }
    report: dict[str, Any] = {
        "probe": "p19_acceptance_808_patch",
        "utc": H.utc_now(),
        "live_write": True,
        "track": {"index": track_index, "name": C.TRACK_NAME, "fx_index": 0, "item": str(item), "window_start": window_start},
        "intent": {"cutoff_hz": TARGET_CUTOFF_HZ, "cutoff_document": PATCH["filter_1_cutoff"], "drop_st": DROP_ST, "route_power": POWER},
        "patch_controls": PATCH,
        "enum_intent": ENUM_INTENT,
        "before_controls": before_values,
        "before_host_enums": {
            control_id: C.host_display(track, host_index[control_id])
            for control_id in ENUM_INTENT
            if isinstance(host_index.get(control_id), int)
        },
        "before_host_cutoff": C.host_display(track, host_index["filter_1_cutoff"]),
        "before_routes": [
            row
            for row in (C.call(server, "plugin_modulation", {"instance": instance, "op": "list"})["data"].get("routes") or [])
            if row.get("destination")
        ],
    }

    # --- dry run, then one apply ---------------------------------------------
    request = {
        "controls": PATCH,
        "routes_create": [{"source": "env_2", "destination": "osc_1_transpose", "depth": DROP_ST}],
    }
    preview = C.call(server, "plugin_write", {"instance": instance, "request": request, "dry_run": True})
    preview_data = preview.get("data") or {}
    report["preview"] = {
        "ok": preview.get("ok"),
        "would_write": preview_data.get("would_write"),
        "committed": preview_data.get("committed"),
        "plan_summary": preview_data.get("plan_summary"),
    }
    assert report["preview"]["would_write"] is True, report["preview"]
    assert not report["preview"]["committed"], report["preview"]

    applied = C.call(server, "plugin_write", {"instance": instance, "request": request, "dry_run": False})
    applied_data = applied.get("data") or {}
    report["applied"] = {
        "ok": applied.get("ok"),
        "committed": applied_data.get("committed"),
        "outcome": applied_data.get("outcome"),
        "error": applied.get("error"),
    }
    assert applied.get("ok") is True and applied_data.get("committed") is True, report["applied"]
    assert ((applied_data.get("outcome") or {}).get("verdict")) == "ALL_VERIFIED", report["applied"]

    # --- verification: the layer, and the host's own formatter ---------------
    after = C.call(server, "plugin_read", {"instance": instance, "controls": sorted(PATCH), "include_display": True})
    after_values = {
        key: {"native": value.get("native"), "display": value.get("display")}
        for key, value in (after.get("data") or {}).get("values", {}).items()
    }
    host_after = {
        control_id: C.host_display(track, host_index[control_id])
        for control_id in sorted(PATCH)
        if isinstance(host_index.get(control_id), int)
    }
    routes = [
        row
        for row in (C.call(server, "plugin_modulation", {"instance": instance, "op": "list"})["data"].get("routes") or [])
        if row.get("destination")
    ]
    cutoff_display = host_after.get("filter_1_cutoff")
    cutoff_semitones = None
    if isinstance(cutoff_display, str):
        try:
            cutoff_semitones = float(cutoff_display.split()[0])
        except (ValueError, IndexError):
            cutoff_semitones = None
    report["verification"] = {
        "read_back": after_values,
        "host_enums": {control_id: host_after.get(control_id) for control_id in ENUM_INTENT},
        "host_cutoff": cutoff_display,
        "host_cutoff_hz": (
            None if cutoff_semitones is None else round(MIDI60_HZ * 2 ** (cutoff_semitones / 12.0), 2)
        ),
        "routes": routes,
    }

    # --- the render and its measurement -------------------------------------
    out = H.EVIDENCE_DIR / f"p19_808_patch__{H.run_stamp()}.wav"
    render = C.render_window(server, project, out, window_start)
    analysis = C.analyse(out, with_pitch=True)
    report["render"] = {"file": str(out), **render}
    report["analysis"] = analysis
    path = H.write_run_json("p19_acceptance_808_patch", report, H.EVIDENCE_DIR)
    print(f"evidence: {path}")
    print(f"render:   {out}")
    print(f"preview:  would_write={report['preview']['would_write']} committed={report['preview']['committed']} plan={json.dumps(report['preview']['plan_summary'])}")
    print(f"applied:  committed={report['applied']['committed']} verdict={(report['applied']['outcome'] or {}).get('verdict')}")
    print(f"host enums after: {json.dumps(report['verification']['host_enums'])}")
    print(f"host cutoff after: {cutoff_display!r} -> {report['verification']['host_cutoff_hz']} Hz (target {TARGET_CUTOFF_HZ})")
    print(f"routes: {json.dumps(routes)}")
    print(
        f"measured: h2={analysis['h2_db']} h3={analysis['h3_db']} sub={analysis['sub_below_120']} peak={analysis['peak_dbfs']} "
        f"clip={analysis['n_clipped_samples']} offs={analysis['steady_offset_st']}"
    )
    print(
        f"drop:     flight peak {analysis['attack_pitch_hz']} Hz = {analysis['attack_pitch_st_offset']} st "
        f"(instrument {analysis['attack_pitch_instrument']}), settle {analysis['settle_to_within_half_semitone_s']} s"
    )

    # --- assertions: the patch, in the layer and in the host -----------------
    for control_id, intent in ENUM_INTENT.items():
        assert host_after.get(control_id) == intent, (control_id, host_after.get(control_id), intent)
        assert after_values[control_id]["display"] == intent, (control_id, after_values[control_id])
    for control_id, value in PATCH.items():
        if control_id in ENUM_INTENT:
            continue
        assert after_values[control_id]["native"] == pytest.approx(value, rel=0.01, abs=0.001), (
            control_id,
            after_values[control_id],
            value,
        )
    assert report["verification"]["host_cutoff_hz"] is not None
    assert report["verification"]["host_cutoff_hz"] == pytest.approx(TARGET_CUTOFF_HZ, rel=0.02), report["verification"]
    assert len(routes) == 1, routes
    route = routes[0]
    assert (route["source"], route["destination"]) == ("env_2", "osc_1_transpose"), route
    assert route["amount"] == pytest.approx(0.124733943, rel=1e-3), route
    assert float(route["power"]) == pytest.approx(POWER), route
    # the sound: a sub, no clipping, in tune, with a drop at every note
    assert analysis["n_clipped_samples"] == 0, analysis["peak_dbfs"]
    assert analysis["sub_below_120"] > 0.98, analysis["sub_below_120"]
    assert analysis["h2_db"] is not None and analysis["h2_db"] < BASELINE_H2_DB - 10, analysis["h2_db"]
    for note, offset in analysis["steady_offset_st"].items():
        assert offset is not None and abs(offset) < 0.25, (note, offset)
    assert analysis["attack_pitch_st_offset"] is not None and analysis["attack_pitch_st_offset"] > 3.0, analysis
    assert render["settings_restored"] is True, render
