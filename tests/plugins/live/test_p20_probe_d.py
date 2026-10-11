"""P20 probe D - the candidate 808: how much grit, and a decay that tapers.

Probe C showed Soft Clip's ballpark (drive +18 lands h3 where the reference is,
but its h5/h7 are 10-18 dB weaker; drive +24 matches the *spacing*, which means
the reference is a **blend** of clean sub and hard-clipped sub, not a fully wet
one). Independent review notes: `Distortion Drive` is bipolar +-30 dB, mix is the
grit control, and the failed patch's `env_1_sustain = 0.82` plateau is part of
"closed sub bass" while the reference keeps tapering (review: -4.1 dB over 0.8 s,
-9.4 dB over 2 s).

Arms (route amount is native - the semantic `depth` on `routes_update` is refused,
defect D1):
- D1..D3  Soft Clip +24 dB with mix 0.20 / 0.30 / 0.40  (the grit blend sweep)
- D4      candidate: best-blend grit + a decay-dominant amp envelope
          (attack 8 ms, decay 2.0 s, sustain 0.45, release 0.6 s) and a deeper,
          slightly faster drop (18 st, env_2 decay 50 ms, power 0)
- D5      restore the pre-probe state and verify
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
AMOUNT_PER_ST = AMOUNT_12ST / 12.0
BASELINE_CONTROLS = [
    "compressor_on", "volume", "filter_1_cutoff", "filter_1_style",
    "distortion_on", "distortion_type", "distortion_mix", "distortion_drive",
    "env_1_attack", "env_1_decay", "env_1_sustain", "env_1_release",
    "env_2_attack", "env_2_decay", "env_2_decay_power", "env_2_sustain",
]
REFERENCE_H_DB = [0.0, -27.86, -19.64, -39.57, -23.97, -40.55, -29.51, -43.81]
REFERENCE_ENVELOPE = {"0.01s": -3.38, "0.02s": -1.66, "0.05s": -2.25, "0.1s": -3.5, "0.2s": -2.87, "0.4s": -3.73, "0.6s": -3.71, "0.8s": -4.69}


def test_p20_probe_d_candidate():
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
    before = C.call(server, "plugin_read", {"instance": instance, "controls": sorted(BASELINE_CONTROLS), "include_display": True})
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
        "probe": "p20_probe_d_candidate",
        "utc": H.utc_now(),
        "live_write": True,
        "track": {"index": track_index, "name": C.TRACK_NAME, "item": str(item), "window_start": window_start},
        "baseline_controls": before_values,
        "baseline_routes": routes_before,
        "reference_target_h_db": REFERENCE_H_DB,
        "reference_envelope_points_db": REFERENCE_ENVELOPE,
        "arms": [],
    }

    def run(label: str, controls: dict[str, Any], depth_st: float, verify: bool = True) -> dict[str, Any]:
        request: dict[str, Any] = {
            "controls": controls,
            "routes_update": [{"slot": SLOT, "amount": depth_st * AMOUNT_PER_ST}],
        }
        preview = C.call(server, "plugin_write", {"instance": instance, "request": request, "dry_run": True})
        assert (preview.get("data") or {}).get("would_write") is True, (label, preview)
        applied = C.call(server, "plugin_write", {"instance": instance, "request": request, "dry_run": False})
        data = applied.get("data") or {}
        assert applied.get("ok") is True and data.get("committed") is True, (label, applied)
        out = H.EVIDENCE_DIR / f"p20_probe_d_{label}__{H.run_stamp()}.wav"
        render = C.render_window(server, project, out, window_start)
        analysis = S.measure(out, label=label)
        h_db = (analysis["harmonics"].get("h_db") or [])[:9]
        fit = None
        if len(h_db) >= 7:
            fit = round(float(sum((float(a) - float(b)) ** 2 for a, b in zip(h_db[:7], REFERENCE_H_DB[:7])) / 7.0) ** 0.5, 2)
        return {
            "label": label,
            "request": request,
            "verdict": (data.get("outcome") or {}).get("verdict"),
            "render": {"file": str(out), **render},
            "sha256": hashlib.sha256(out.read_bytes()).hexdigest()[:16],
            "summary": {
                "h_db": h_db,
                "harmonic_rms_error_vs_reference_db": fit,
                "share_above_300hz": analysis["harmonics"].get("share_above_300hz"),
                "peak_dbfs": analysis["meta"]["peak_dbfs"],
                "rms_dbfs": analysis["meta"]["rms_dbfs"],
                "crest_db": round(float(analysis["meta"]["peak_dbfs"]) - float(analysis["meta"]["rms_dbfs"]), 2),
                "click_step_over_peak": analysis["attack"]["click_step_over_peak"],
                "click_step_vs_body_step": analysis["attack"]["click_step_vs_body_step"],
                "level_db_at": analysis["attack"]["level_db_at"],
                "zero_crossings": analysis["zero_crossings"],
                "envelope_points_db": analysis["envelope_points_db"],
                "body_f0_hz": analysis["pitch"]["body_f0_hz"],
                "drop_start_st": analysis["pitch"].get("drop_start_st"),
                "settle_to_0.5st_s": analysis["pitch"].get("settle_to_0.5st_s"),
                "tail": analysis["tail"],
            },
            "analysis": analysis,
        }

    grit = {"compressor_on": "Off", "volume": -3.0, "distortion_on": "On", "distortion_type": "Soft Clip", "distortion_drive": 24.0}
    for label, mix in (("d1_mix20", 0.20), ("d2_mix30", 0.30), ("d3_mix40", 0.40)):
        report["arms"].append(run(label, {**grit, "distortion_mix": mix}, 12.0))

    best_mix = float(
        min(
            (arm for arm in report["arms"]),
            key=lambda arm: arm["summary"]["harmonic_rms_error_vs_reference_db"] if arm["summary"]["harmonic_rms_error_vs_reference_db"] is not None else 99.0,
        )["request"]["controls"]["distortion_mix"]  # type: ignore[index]
    )
    report["chosen_mix"] = best_mix
    candidate = {
        **grit,
        "distortion_mix": best_mix,
        "env_1_attack": 0.008,
        "env_1_decay": 2.0,
        "env_1_sustain": 0.45,
        "env_1_release": 0.6,
        "env_2_attack": 0.0005,
        "env_2_decay": 0.05,
        "env_2_decay_power": 0.0,
        "env_2_sustain": 0.0,
    }
    report["candidate_controls"] = candidate
    report["arms"].append(run("d4_candidate", candidate, 18.0))

    # restore the pre-probe state
    restore_controls: dict[str, Any] = {}
    for key, value in before_values.items():
        if value.get("display") is not None and key in ("compressor_on", "distortion_on", "filter_1_style", "distortion_type"):
            restore_controls[key] = value["display"]
        elif value.get("native") is not None:
            restore_controls[key] = value["native"]
    restore_request: dict[str, Any] = {
        "controls": restore_controls,
        "routes_update": [{"slot": SLOT, "amount": float(routes_before[0]["amount"]) if routes_before else AMOUNT_12ST}],
    }
    preview = C.call(server, "plugin_write", {"instance": instance, "request": restore_request, "dry_run": True})
    assert (preview.get("data") or {}).get("would_write") is True, preview
    applied = C.call(server, "plugin_write", {"instance": instance, "request": restore_request, "dry_run": False})
    data = applied.get("data") or {}
    assert applied.get("ok") is True and data.get("committed") is True, applied
    after = C.call(server, "plugin_read", {"instance": instance, "controls": sorted(BASELINE_CONTROLS), "include_display": True})
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
            for control_id in ("compressor_on", "distortion_on", "env_1_decay", "env_1_sustain", "filter_1_cutoff")
            if isinstance(host_index.get(control_id), int)
        },
        "matches_baseline": {
            key: abs(float(after_values[key]) - float(value["native"])) <= 1e-3
            for key, value in before_values.items()
            if isinstance(value.get("native"), (int, float)) and isinstance(after_values.get(key), (int, float))
        },
    }

    path = H.write_run_json("p20_probe_d_candidate", report, H.EVIDENCE_DIR)
    print(f"evidence: {path}")
    print(f"reference h_db {REFERENCE_H_DB}   envelope {json.dumps(REFERENCE_ENVELOPE)}")
    for arm in report["arms"]:
        summary = arm["summary"]
        print(f"\n== {arm['label']} ({arm['verdict']})")
        print(f"   h {summary['h_db']}  fit RMS {summary['harmonic_rms_error_vs_reference_db']} dB")
        print(f"   >300Hz {summary['share_above_300hz']} peak {summary['peak_dbfs']} rms {summary['rms_dbfs']} crest {summary['crest_db']} dB")
        print(f"   click {summary['click_step_over_peak']} (x{summary['click_step_vs_body_step']})  level {summary['level_db_at']}")
        print(f"   env {json.dumps(summary['envelope_points_db'])}")
        print(f"   crossings {summary['zero_crossings']['crossings_ms']} half {summary['zero_crossings']['half_periods_ms']}")
        print(f"   drop {summary['drop_start_st']} st settle {summary['settle_to_0.5st_s']} s  tail {json.dumps(summary['tail'])}")
    print(f"\nchosen mix: {report['chosen_mix']}")
    print(f"restore: {json.dumps(report['restore']['matches_baseline'])} host {json.dumps(report['restore']['host_text'])}")

    assert all(report["restore"]["matches_baseline"].values()), report["restore"]
    assert len(routes_after) == 1 and abs(float(routes_after[0]["amount"]) - AMOUNT_12ST) < 1e-3, routes_after
