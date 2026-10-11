"""P20 acceptance - the final 808 patch: one preview, one apply, verified twice, rendered.

**What the measurements chose** (probes B-G, `p20_probe_*`, re-derived with the
corrected instruments in `p20_reanalysis__*`):

- **the click** was the multiband compressor FX, ON in the failed patch (mix 0.85,
  low band +16.3 dB, band +11.7 dB): with only `compressor_on = Off` the onset's
  sample-to-sample step falls from 13.6 % of peak (21x the body's own slope) to
  0.55 % (0.9x) - a single-control differential (probes A/B);
- **the closed sub** was the absence of harmonics, not a filter setting: the same
  58 Hz lowpass that makes the sub cannot produce the reference's odd-dominant
  series from a sawtooth (58/116/232 Hz give h2 -20.2/-8.7/-7.8). Saturating the
  *filtered* sub with the FX distortion does: Soft Clip +24 dB at **mix 0.20**
  with the cutoff at **50 Hz** fits the reference's h1..h7 to **1.46 dB RMS**
  (h2 -26.6 vs -27.8, h3 -16.5 vs -19.6, h4 -38.8 vs -39.6, h5 -22.9 vs -24.0,
  h6 -39.2 vs -40.5, h7 -29.7 vs -29.5) - probes C/D/E;
- **the dragged attack** (no zero crossing for 20 ms at cutoff 50 Hz) is fixed by
  a second route `env_2 -> filter_1_cutoff` at +12 st: the cutoff opens while the
  pitch is high and closes with the drop, so the first half-cycle arrives at
  8.7 ms and the onset step is 2.78 % of peak, 1.79x the body slope - *softer*
  than the reference's own 4.73 % / 3.3x (probe G);
- **the drop**: 18 st requested, measured 9.9 st / settle 41 ms through the same
  instrument that reads the reference at 7.7 st / 26 ms (the instrument averages
  the flight, so it is a lower bound; the reference's own cycle-based start is
  +18.2 st per the independent review);
- **the taper**: `env_1` decay 2.0 s / sustain 0.45 / release 0.6 s gives -2.4 dB
  over 0.55 s against the reference's -2.05 dB (the failed patch held a plateau).

The route depth on the existing slot is written as a **semantic depth** - the D1
defect that made `routes_update` refuse one is fixed in the provider and pinned by
`tests/plugins/test_p20_routes_update_depth.py`.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pytest

import live_harness as H
import probe_p19_common as C
import probe_p20_sample as S

pytestmark = pytest.mark.live_write

MIDI60_HZ = 261.6255653005986
DISPLAY_OFFSET = 60.0
SLOT = 1
DROP_ST = 18.0
SWEEP_ST = 12.0
TARGET_CUTOFF_HZ = 50.0
E1_HZ = C.NOTES_HZ[28]

#: The final patch: the failed patch's shape with the four measured corrections.
PATCH: dict[str, Any] = {
    # the compressor was ON in the failed patch and is the measured click
    "compressor_on": "Off",
    # the grit: a soft-clipped blend of the clean sub (measured fit 1.46 dB RMS)
    "distortion_on": "On",
    "distortion_type": "Soft Clip",
    "distortion_drive": 24.0,
    "distortion_mix": 0.20,
    # the source: the same oscillator, one filter notch tighter
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
    # the amp envelope: a decay that tapers, like the reference's
    "env_1_attack": 0.008,
    "env_1_decay": 2.0,
    "env_1_sustain": 0.45,
    "env_1_release": 0.6,
    # the pitch envelope: a deeper, faster drop
    "env_2_attack": 0.0005,
    "env_2_decay": 0.05,
    "env_2_decay_power": 0.0,
    "env_2_sustain": 0.0,
    # voicing, unchanged from the failed patch (the glide is the track's point)
    "legato": "Off",
    "portamento_force": "On",
    "polyphony": 1.0,
    "voice_priority": "Newest",
    "pitch_bend_range": "0 semitones",
    "voice_transpose": 0.0,
    # output level: the reference peaks at 0 dBFS after normalisation; -3 dB keeps
    # the render off the ceiling with the same crest factor class
    "volume": -3.0,
}
ENUM_INTENT = {
    "compressor_on": "Off",
    "distortion_on": "On",
    "distortion_type": "Soft Clip",
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
}
#: The reference's body series measured through the same instrument
#: (`p20_reanalysis__*`), for a fit assertion rather than a re-typed target.
REFERENCE_H_DB = [0.0, -27.84, -19.63, -39.62, -23.95, -40.53, -29.48]


def test_p20_final_808_patch():
    try:
        import reapy

        project = reapy.Project()
        project.id  # noqa: B018 - reachability probe
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"REAPER is not reachable, so the probe did not run: {exc}")

    import os

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
        for key, value in ((before.get("data") or {}).get("values") or {}).items()
    }
    routes_before = [
        row
        for row in (C.call(server, "plugin_modulation", {"instance": instance, "op": "list"})["data"].get("routes") or [])
        if row.get("destination")
    ]
    report: dict[str, Any] = {
        "probe": "p20_final_808_patch",
        "utc": H.utc_now(),
        "live_write": True,
        "track": {"index": track_index, "name": C.TRACK_NAME, "fx_index": 0, "item": str(item), "window_start": window_start},
        "patch_controls": PATCH,
        "enum_intent": ENUM_INTENT,
        "intent": {"cutoff_hz": TARGET_CUTOFF_HZ, "drop_st": DROP_ST, "sweep_st": SWEEP_ST, "route_slot": SLOT},
        "before_controls": before_values,
        "before_routes": routes_before,
    }

    request = {
        "controls": PATCH,
        # the existing route's depth is semantic now (D1 fixed): the provider converts it
        "routes_update": [{"slot": SLOT, "destination": "osc_1_transpose", "depth": DROP_ST}],
        # the attack sweep: the cutoff opens while the pitch is high
        "routes_create": [{"source": "env_2", "destination": "filter_1_cutoff", "depth": SWEEP_ST}],
    }
    # idempotence: if a previous run already created the sweep route, remove it in the
    # same batch before recreating it, so a re-run of this probe cannot stack duplicates
    stale = [int(row["slot"]) for row in routes_before if row.get("destination") == "filter_1_cutoff"]
    if stale:
        request = {
            **request,
            "routes_remove": [{"slot": slot} for slot in stale],
            "routes_create": request["routes_create"],
        }
    report["stale_sweep_slots_removed"] = stale

    # --- preview, then one apply ---------------------------------------------
    preview = C.call(server, "plugin_write", {"instance": instance, "request": request, "dry_run": True})
    preview_data = preview.get("data") or {}
    report["preview"] = {
        "ok": preview.get("ok"),
        "would_write": preview_data.get("would_write"),
        "committed": preview_data.get("committed"),
        "plan_summary": preview_data.get("plan_summary"),
        "route_plan": preview_data.get("route_plan"),
    }
    assert report["preview"]["would_write"] is True, report["preview"]
    assert not report["preview"]["committed"], report["preview"]

    applied = C.call(server, "plugin_write", {"instance": instance, "request": request, "dry_run": False})
    applied_data = applied.get("data") or {}
    report["applied"] = {
        "ok": applied.get("ok"),
        "committed": applied_data.get("committed"),
        "verdict": (applied_data.get("outcome") or {}).get("verdict"),
        "error": applied.get("error"),
        "route_plan": applied_data.get("route_plan"),
    }
    assert applied.get("ok") is True and applied_data.get("committed") is True, report["applied"]
    assert report["applied"]["verdict"] == "ALL_VERIFIED", report["applied"]

    # --- verification: the layer, then the host's own formatter --------------
    after = C.call(server, "plugin_read", {"instance": instance, "controls": sorted(PATCH), "include_display": True})
    after_values = {
        key: {"native": value.get("native"), "display": value.get("display")}
        for key, value in ((after.get("data") or {}).get("values") or {}).items()
    }
    host_text = {
        control_id: C.host_display(track, host_index[control_id])
        for control_id in sorted(PATCH)
        if isinstance(host_index.get(control_id), int)
    }
    routes = [
        row
        for row in (C.call(server, "plugin_modulation", {"instance": instance, "op": "list"})["data"].get("routes") or [])
        if row.get("destination")
    ]
    report["verification"] = {
        "read_back": after_values,
        "host_text": host_text,
        "routes": routes,
        "host_cutoff_hz": None,
    }
    cutoff_text = str(host_text.get("filter_1_cutoff") or "")
    try:
        report["verification"]["host_cutoff_hz"] = round(
            MIDI60_HZ * 2 ** (float(cutoff_text.split()[0]) / 12.0), 2
        )
    except (ValueError, IndexError):
        report["verification"]["host_cutoff_hz"] = None

    # --- the render and its measurement --------------------------------------
    out = H.EVIDENCE_DIR / f"p20_final_808__{H.run_stamp()}.wav"
    render = C.render_window(server, project, out, window_start)
    analysis = S.measure(out, label="final 808 patch", note_window=(0.18, 1.16))
    mono, fs = C.read_mono(out)
    per_note: dict[str, Any] = {}
    for name, (low, high) in C.STEADY.items():
        segment = mono[int(low * fs) : int(high * fs)]
        f0 = C.autocorr_f0(segment, fs, 20.0, 200.0)
        notes = {"n1": 41.2034446, "n2": 48.9994295, "n3": 54.9999999}
        per_note[name] = {
            "f0_hz": None if f0 is None else round(float(f0), 3),
            "offset_st": None if f0 is None else round(12.0 * float(np.log2(f0 / notes[name])), 3),
            "harmonics": S.harmonic_measures(mono, fs, float(f0 or 0.0), low + 0.05, high),
        }
    fit = None
    series = analysis["harmonics"].get("h_db") or []
    if len(series) >= 7 and all(value is not None for value in series[:7]):
        fit = round(float(sum((float(a) - float(b)) ** 2 for a, b in zip(series[:7], REFERENCE_H_DB)) / 7.0) ** 0.5, 2)

    # A/B audition copies (new files; neither input is ever written)
    ref = S.load(Path(
        "E:/samples/download/Stickz - Future Bass Toolkit/Stickz - Future Bass Toolkit - Sample Pack/"
        "Stickz FBT - 808s/Stickz FBT - 808 10 - F#.wav"
    ))
    reference = S.measure(Path(ref["meta"]["path"]), label="reference F#1")
    stretch = float(reference["pitch"]["body_f0_hz"]) / E1_HZ
    ref_at_e1 = S.resample_to_pitch(ref["mono"], ref["fs"], float(reference["pitch"]["body_f0_hz"]), E1_HZ)
    stamp = H.run_stamp()
    auditions = [
        S.write_aligned(H.EVIDENCE_DIR / f"p20_ab_final_reference_at_e1__{stamp}.wav", ref_at_e1, ref["fs"]),
        S.write_aligned(
            H.EVIDENCE_DIR / f"p20_ab_final_note1__{stamp}.wav",
            S.slice_seconds(mono, fs, 0.18, 1.16),
            fs,
        ),
        S.write_aligned(H.EVIDENCE_DIR / f"p20_ab_final_phrase__{stamp}.wav", mono, fs),
    ]

    report.update(
        {
            "render": {"file": str(out), **render},
            "sha256": hashlib.sha256(out.read_bytes()).hexdigest()[:16],
            "analysis": analysis,
            "per_note": per_note,
            "harmonic_fit_vs_reference_db": fit,
            "reference_h_db": REFERENCE_H_DB,
            "reference_envelope_points_db": reference["envelope_points_db"],
            "auditions": auditions,
        }
    )
    path = H.write_run_json("p20_final_808_patch", report, H.EVIDENCE_DIR)
    print(f"evidence: {path}")
    print(f"render:   {out}")
    print(f"preview:  would_write={report['preview']['would_write']} committed={report['preview']['committed']} plan={json.dumps(report['preview']['plan_summary'])}")
    print(f"applied:  committed={report['applied']['committed']} verdict={report['applied']['verdict']} route_plan={json.dumps(report['applied'].get('route_plan'))}")
    print(f"host:     cutoff {cutoff_text!r} -> {report['verification']['host_cutoff_hz']} Hz; enums {json.dumps({k: host_text.get(k) for k in ENUM_INTENT})}")
    print(f"routes:   {json.dumps(routes)}")
    print(f"measured: h {series[:7]} fit {fit} dB; peak {analysis['meta']['peak_dbfs']} rms {analysis['meta']['rms_dbfs']}")
    print(f"onset:    step {analysis['attack']['click_step_over_peak']} of peak (x{analysis['attack']['click_step_vs_body_step']} body) levels {json.dumps(analysis['attack']['level_db_at'])}")
    print(f"crossings {analysis['zero_crossings']['crossings_ms'][:5]} half {analysis['zero_crossings']['half_periods_ms'][:4]}")
    print(f"envelope: {json.dumps(analysis['envelope_points_db'])} (reference {json.dumps(reference['envelope_points_db'])})")
    print(f"drop:     {analysis['pitch'].get('drop_start_st')} st settle {analysis['pitch'].get('settle_to_0.5st_s')} s")
    print(f"per note: {json.dumps(per_note, default=str)[:600]}")
    print("auditions:")
    for entry in auditions:
        print(f"  {entry['file']}")

    # --- assertions ----------------------------------------------------------
    for control_id, intent in ENUM_INTENT.items():
        assert host_text.get(control_id) == intent, (control_id, host_text.get(control_id), intent)
    for control_id, value in PATCH.items():
        if control_id in ENUM_INTENT:
            continue
        assert after_values[control_id]["native"] == pytest.approx(value, rel=0.01, abs=0.002), (
            control_id,
            after_values[control_id],
            value,
        )
    assert report["verification"]["host_cutoff_hz"] == pytest.approx(TARGET_CUTOFF_HZ, rel=0.02), report["verification"]
    assert len(routes) == 2, routes
    by_destination = {row["destination"]: row for row in routes}
    assert abs(float(by_destination["osc_1_transpose"]["amount"]) - 0.18710091710090637) < 5e-4, routes
    assert abs(float(by_destination["filter_1_cutoff"]["amount"]) - 0.09375) < 5e-4, routes
    assert analysis["meta"]["n_clipped_samples"] == 0, analysis["meta"]["peak_dbfs"]
    assert fit is not None and fit <= 3.0, fit
    assert float(analysis["attack"]["click_step_over_peak"]) < 0.05, analysis["attack"]
    for name, row in per_note.items():
        assert row["offset_st"] is not None and abs(float(row["offset_st"])) < 0.35, (name, row)
    assert render["settings_restored"] is True, render
