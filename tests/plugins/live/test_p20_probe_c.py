"""P20 probe C - the saturation sweep: which drive makes the reference's odd series.

Measured targets from the reference (`p20_reference_analysis__*`, body window):
h2 -27.9, h3 -19.6, h4 -39.6, h5 -24.0, h6 -40.6, h7 -29.5 dB - an **odd-dominant**
series that no filter setting can produce from a sawtooth (probe B: 58/116/232 Hz
give h2 -20.2/-8.7/-7.8, always above or near h3).

Arms (compressor Off, cutoff 58 Hz, route 12 st, volume -12 dB so the render
cannot clip; ratios are level-independent):
- C1..C4  distortion On, Soft Clip, mix 1.0, drive +6/+12/+18/+24 dB
- C5      drive +18, Hard Clip (a different odd series for comparison)
- C6      distortion Off, filter style 12dB (is the style control effective?)
- C7      restore the baseline state
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

import live_harness as H
import probe_p19_common as C
import probe_p20_sample as S

pytestmark = pytest.mark.live_write

SLOT = 1
AMOUNT_12ST = 0.12473393976688385
BASELINE = {
    "filter_1_cutoff": 33.91945457458496,
    "volume": -1.0,
    "compressor_on": "On",
    "filter_1_style": "24dB",
    "distortion_on": "Off",
}
REFERENCE_H_DB = [0.0, -27.86, -19.64, -39.57, -23.97, -40.55, -29.51, -43.81]


def test_p20_probe_c_saturation():
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
    watched = sorted(BASELINE)
    before = C.call(server, "plugin_read", {"instance": instance, "controls": watched, "include_display": True})
    before_values = {key: value.get("native") for key, value in ((before.get("data") or {}).get("values") or {}).items()}

    report: dict[str, Any] = {
        "probe": "p20_probe_c_saturation",
        "utc": H.utc_now(),
        "live_write": True,
        "track": {"index": track_index, "name": C.TRACK_NAME, "item": str(item), "window_start": window_start},
        "baseline_read": before_values,
        "reference_target_h_db": REFERENCE_H_DB,
        "arms": [],
    }

    def run(label: str, controls: dict[str, Any], amount: float = AMOUNT_12ST) -> dict[str, Any]:
        request: dict[str, Any] = {"controls": controls, "routes_update": [{"slot": SLOT, "amount": amount}]}
        preview = C.call(server, "plugin_write", {"instance": instance, "request": request, "dry_run": True})
        assert (preview.get("data") or {}).get("would_write") is True, (label, preview)
        applied = C.call(server, "plugin_write", {"instance": instance, "request": request, "dry_run": False})
        data = applied.get("data") or {}
        assert applied.get("ok") is True and data.get("committed") is True, (label, applied)
        out = H.EVIDENCE_DIR / f"p20_probe_c_{label}__{H.run_stamp()}.wav"
        render = C.render_window(server, project, out, window_start)
        analysis = S.measure(out, label=label)
        return {
            "label": label,
            "request": request,
            "verdict": (data.get("outcome") or {}).get("verdict"),
            "render": {"file": str(out), **render},
            "sha256": hashlib.sha256(out.read_bytes()).hexdigest()[:16],
            "summary": {
                "h_db": (analysis["harmonics"].get("h_db") or [])[:9],
                "share_above_300hz": analysis["harmonics"].get("share_above_300hz"),
                "noise_floor_db": analysis["harmonics"].get("noise_floor_db"),
                "peak_dbfs": analysis["meta"]["peak_dbfs"],
                "rms_dbfs": analysis["meta"]["rms_dbfs"],
                "crest_db": round(float(analysis["meta"]["peak_dbfs"]) - float(analysis["meta"]["rms_dbfs"]), 2),
                "click_step_over_peak": analysis["attack"]["click_step_over_peak"],
                "click_step_vs_body_step": analysis["attack"]["click_step_vs_body_step"],
                "level_db_at": analysis["attack"]["level_db_at"],
                "envelope_points_db": analysis["envelope_points_db"],
                "body_f0_hz": analysis["pitch"]["body_f0_hz"],
                "drop_start_st": analysis["pitch"].get("drop_start_st"),
                "settle_to_0.5st_s": analysis["pitch"].get("settle_to_0.5st_s"),
            },
            "analysis": analysis,
        }

    common = {"compressor_on": "Off", "volume": -12.0, "filter_1_cutoff": BASELINE["filter_1_cutoff"]}
    for label, drive in (("c1_soft_6", 6.0), ("c2_soft_12", 12.0), ("c3_soft_18", 18.0), ("c4_soft_24", 24.0)):
        report["arms"].append(
            run(
                label,
                {
                    **common,
                    "distortion_on": "On",
                    "distortion_type": "Soft Clip",
                    "distortion_mix": 1.0,
                    "distortion_drive": drive,
                },
            )
        )
    report["arms"].append(
        run(
            "c5_hard_18",
            {
                **common,
                "distortion_on": "On",
                "distortion_type": "Hard Clip",
                "distortion_mix": 1.0,
                "distortion_drive": 18.0,
            },
        )
    )
    report["arms"].append(run("c6_style_12db", {**common, "distortion_on": "Off", "filter_1_style": "12dB"}))

    # restore
    restore_request: dict[str, Any] = {
        "controls": {
            "compressor_on": BASELINE["compressor_on"],
            "volume": BASELINE["volume"],
            "filter_1_cutoff": BASELINE["filter_1_cutoff"],
            "filter_1_style": BASELINE["filter_1_style"],
            "distortion_on": BASELINE["distortion_on"],
        },
        "routes_update": [{"slot": SLOT, "amount": AMOUNT_12ST}],
    }
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
        "restored_read": after_values,
        "routes": routes_after,
        "host_text": {
            control_id: C.host_display(track, host_index[control_id])
            for control_id in watched
            if isinstance(host_index.get(control_id), int)
        },
        "matches_baseline": {
            key: (
                before_values.get(key) is not None
                and after_values.get(key) is not None
                and abs(float(after_values[key]) - float(before_values[key])) <= 1e-3
            )
            for key in watched
            if isinstance(before_values.get(key), (int, float))
        },
    }

    path = H.write_run_json("p20_probe_c_saturation", report, H.EVIDENCE_DIR)
    print(f"evidence: {path}")
    print(f"reference target h_db: {REFERENCE_H_DB}")
    for arm in report["arms"]:
        summary = arm["summary"]
        print(f"\n== {arm['label']}  ({arm['verdict']})")
        print(f"   h {summary['h_db']}")
        print(f"   >300Hz {summary['share_above_300hz']}  peak {summary['peak_dbfs']} rms {summary['rms_dbfs']} crest {summary['crest_db']} dB")
        print(f"   click {summary['click_step_over_peak']} (x{summary['click_step_vs_body_step']})  level {summary['level_db_at']}")
        print(f"   drop {summary['drop_start_st']} st settle {summary['settle_to_0.5st_s']} s  env {json.dumps(summary['envelope_points_db'])}")
    print(f"\nrestore: {json.dumps(report['restore']['matches_baseline'])} host {json.dumps(report['restore']['host_text'])}")

    assert all(report["restore"]["matches_baseline"].values()), report["restore"]
    assert len(routes_after) == 1 and abs(float(routes_after[0]["amount"]) - AMOUNT_12ST) < 1e-3, routes_after
