"""P20 probe E - the last three levers: cutoff, and an attack-time filter sweep.

D4 (the candidate) fits the reference's harmonic series to 1.85 dB RMS and its
amplitude taper to ~2 dB over 0.6 s, but two measurable differences remain:

- **the onset is dragged**: our first zero crossing is at 11.5 ms (the 24 dB
  lowpass at 58 Hz takes that long to swing at the note's start), while the
  reference's first half-cycle is 3.3 ms and its first four half-cycles descend
  149/119/99/81 Hz. The independent review also measured an HF burst +26 dB over
  the body in the reference's first 20 ms - a bright attack, not a drag;
- **h2 is 3 dB high** (-24.9 vs the reference's -27.9): the source's own second
  harmonic (the filtered saw) leaks through the clipper.

Arms:
- E1  candidate + cutoff 70 Hz (less drag)
- E2  candidate + cutoff 50 Hz (cleaner source, more drag)
- E3  candidate + a second route `env_2 -> filter_1_cutoff` at +24 st: the cutoff
      opens to 232 Hz while the pitch is high and closes with the drop (one extra
      route, removed again in the restore and the table verified)
- (restore: the route table returns to exactly its baseline row)
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

import pytest

import live_harness as H
import probe_p19_common as C
import probe_p20_sample as S

pytestmark = pytest.mark.live_write

MIDI60_HZ = 261.6255653005986
SLOT = 1
AMOUNT_PER_ST = 0.12473393976688385 / 12.0
REFERENCE_H_DB = [0.0, -27.86, -19.64, -39.57, -23.97, -40.55, -29.51, -43.81]
CANDIDATE: dict[str, Any] = {
    "compressor_on": "Off",
    "volume": -3.0,
    "distortion_on": "On",
    "distortion_type": "Soft Clip",
    "distortion_drive": 24.0,
    "distortion_mix": 0.20,
    "env_1_attack": 0.008,
    "env_1_decay": 2.0,
    "env_1_sustain": 0.45,
    "env_1_release": 0.6,
    "env_2_attack": 0.0005,
    "env_2_decay": 0.05,
    "env_2_decay_power": 0.0,
    "env_2_sustain": 0.0,
}


def cutoff_doc(hz: float) -> float:
    return 60.0 + 12.0 * math.log2(hz / MIDI60_HZ)


def test_p20_probe_e_cutoff_and_attack_sweep():
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
    watched = sorted({*CANDIDATE, "filter_1_cutoff", "filter_1_style"})
    before = C.call(server, "plugin_read", {"instance": instance, "controls": watched, "include_display": True})
    before_values = {key: value.get("native") for key, value in ((before.get("data") or {}).get("values") or {}).items()}
    before_displays = {key: value.get("display") for key, value in ((before.get("data") or {}).get("values") or {}).items()}
    routes_before = [
        row
        for row in (C.call(server, "plugin_modulation", {"instance": instance, "op": "list"})["data"].get("routes") or [])
        if row.get("destination")
    ]

    report: dict[str, Any] = {
        "probe": "p20_probe_e_cutoff_and_attack_sweep",
        "utc": H.utc_now(),
        "live_write": True,
        "track": {"index": track_index, "name": C.TRACK_NAME, "item": str(item), "window_start": window_start},
        "baseline_controls": before_values,
        "baseline_routes": routes_before,
        "candidate": CANDIDATE,
        "reference_target_h_db": REFERENCE_H_DB,
        "arms": [],
    }

    def run(label: str, controls: dict[str, Any], *, create_route: dict[str, Any] | None = None, remove_slot: int | None = None) -> dict[str, Any]:
        request: dict[str, Any] = {"controls": controls, "routes_update": [{"slot": SLOT, "amount": 18.0 * AMOUNT_PER_ST}]}
        if create_route:
            request["routes_create"] = [create_route]
        if remove_slot is not None:
            request["routes_remove"] = [{"slot": remove_slot}]
        preview = C.call(server, "plugin_write", {"instance": instance, "request": request, "dry_run": True})
        assert (preview.get("data") or {}).get("would_write") is True, (label, preview)
        applied = C.call(server, "plugin_write", {"instance": instance, "request": request, "dry_run": False})
        data = applied.get("data") or {}
        assert applied.get("ok") is True and data.get("committed") is True, (label, applied)
        out = H.EVIDENCE_DIR / f"p20_probe_e_{label}__{H.run_stamp()}.wav"
        render = C.render_window(server, project, out, window_start)
        analysis = S.measure(out, label=label)
        h_db = (analysis["harmonics"].get("h_db") or [])[:9]
        fit = (
            round(float(sum((float(a) - float(b)) ** 2 for a, b in zip(h_db[:7], REFERENCE_H_DB[:7])) / 7.0) ** 0.5, 2)
            if len(h_db) >= 7
            else None
        )
        routes_now = [
            row
            for row in (C.call(server, "plugin_modulation", {"instance": instance, "op": "list"})["data"].get("routes") or [])
            if row.get("destination")
        ]
        return {
            "label": label,
            "request": request,
            "verdict": (data.get("outcome") or {}).get("verdict"),
            "render": {"file": str(out), **render},
            "sha256": hashlib.sha256(out.read_bytes()).hexdigest()[:16],
            "routes_after_arm": routes_now,
            "summary": {
                "h_db": h_db,
                "harmonic_rms_error_vs_reference_db": fit,
                "share_above_300hz": analysis["harmonics"].get("share_above_300hz"),
                "peak_dbfs": analysis["meta"]["peak_dbfs"],
                "rms_dbfs": analysis["meta"]["rms_dbfs"],
                "crest_db": round(float(analysis["meta"]["peak_dbfs"]) - float(analysis["meta"]["rms_dbfs"]), 2),
                "click_step_over_peak": analysis["attack"]["click_step_over_peak"],
                "level_db_at": analysis["attack"]["level_db_at"],
                "zero_crossings": analysis["zero_crossings"],
                "envelope_points_db": analysis["envelope_points_db"],
                "drop_start_st": analysis["pitch"].get("drop_start_st"),
                "settle_to_0.5st_s": analysis["pitch"].get("settle_to_0.5st_s"),
                "tail": analysis["tail"],
            },
            "analysis": analysis,
        }

    report["arms"].append(run("e1_cutoff70", {**CANDIDATE, "filter_1_cutoff": cutoff_doc(70.0)}))
    report["arms"].append(run("e2_cutoff50", {**CANDIDATE, "filter_1_cutoff": cutoff_doc(50.0)}))
    report["arms"].append(
        run(
            "e3_attack_sweep",
            {**CANDIDATE, "filter_1_cutoff": cutoff_doc(58.0)},
            create_route={"source": "env_2", "destination": "filter_1_cutoff", "depth": 24.0},
        )
    )

    # restore: the candidate's own state is left as the pre-probe one + the extra route removed
    restore_controls: dict[str, Any] = {}
    for key in ("compressor_on", "distortion_on", "filter_1_style", "distortion_type"):
        if before_displays.get(key) is not None:
            restore_controls[key] = before_displays[key]
    for key, value in before_values.items():
        if key in restore_controls:
            continue
        if isinstance(value, (int, float)):
            restore_controls[key] = float(value)
        elif value is None:
            continue
        else:
            restore_controls[key] = value
    sweep_slot = None
    for row in report["arms"][2]["routes_after_arm"]:
        if row.get("destination") == "filter_1_cutoff":
            sweep_slot = int(row["slot"])
    report["sweep_slot"] = sweep_slot
    restore_request: dict[str, Any] = {
        "controls": restore_controls,
        "routes_update": [{"slot": SLOT, "amount": float(routes_before[0]["amount"]) if routes_before else 18.0 * AMOUNT_PER_ST}],
    }
    if sweep_slot is not None:
        restore_request["routes_remove"] = [{"slot": sweep_slot}]
    preview = C.call(server, "plugin_write", {"instance": instance, "request": restore_request, "dry_run": True})
    assert (preview.get("data") or {}).get("would_write") is True, preview
    applied = C.call(server, "plugin_write", {"instance": instance, "request": restore_request, "dry_run": False})
    data = applied.get("data") or {}
    assert applied.get("ok") is True and data.get("committed") is True, applied
    after = C.call(server, "plugin_read", {"instance": instance, "controls": watched, "include_display": True})
    after_values = {key: value.get("native") for key, value in ((after.get("data") or {}).get("values") or {}).items()}
    routes_after = [
        row
        for row in (C.call(server, "plugin_modulation", {"instance": instance, "op": "list"})["data"].get("routes") or [])
        if row.get("destination")
    ]
    report["restore"] = {
        "verdict": (data.get("outcome") or {}).get("verdict"),
        "request": restore_request,
        "restored_read": after_values,
        "routes": routes_after,
        "host_text": {
            control_id: C.host_display(track, host_index[control_id])
            for control_id in ("compressor_on", "distortion_on", "distortion_mix", "filter_1_cutoff", "env_1_decay")
            if isinstance(host_index.get(control_id), int)
        },
        "matches_baseline": {
            key: abs(float(after_values[key]) - float(value)) <= 1e-3
            for key, value in before_values.items()
            if isinstance(value, (int, float)) and isinstance(after_values.get(key), (int, float))
        },
    }

    path = H.write_run_json("p20_probe_e_cutoff_sweep", report, H.EVIDENCE_DIR)
    print(f"evidence: {path}")
    for arm in report["arms"]:
        summary = arm["summary"]
        print(f"\n== {arm['label']} ({arm['verdict']})")
        print(f"   h {summary['h_db']}  fit {summary['harmonic_rms_error_vs_reference_db']} dB")
        print(f"   peak {summary['peak_dbfs']} rms {summary['rms_dbfs']} crest {summary['crest_db']} dB  click {summary['click_step_over_peak']}")
        print(f"   level {summary['level_db_at']}")
        print(f"   crossings {summary['zero_crossings']['crossings_ms']} half {summary['zero_crossings']['half_periods_ms']} -> {summary['zero_crossings']['implied_hz']} Hz")
        print(f"   drop {summary['drop_start_st']} st settle {summary['settle_to_0.5st_s']} s  env {json.dumps(summary['envelope_points_db'])}")
        print(f"   routes: {json.dumps(arm['routes_after_arm'])}")
    print(f"\nrestore matches: {json.dumps(report['restore']['matches_baseline'])} host {json.dumps(report['restore']['host_text'])}")
    print(f"routes after restore: {json.dumps(routes_after)}")

    assert all(report["restore"]["matches_baseline"].values()), report["restore"]
    assert len(routes_after) == 1, routes_after
    assert abs(float(routes_after[0]["amount"]) - 0.12473393976688385) < 1e-3, routes_after
