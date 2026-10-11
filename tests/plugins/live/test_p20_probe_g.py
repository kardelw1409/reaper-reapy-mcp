"""P20 probe G - two softer ways to un-drag the attack, before the final patch.

F1/F2 (cutoff 50 Hz + `env_2 -> filter_1_cutoff` at +24/+30 st) match the
reference's attack *timing* (full level by 20 ms, first half-cycle at 5.5-6.2 ms)
and its harmonic series (1.46 dB RMS), but the fully opened filter lets the
sawtooth's own edge through at the note start, so the onset step is 5-7 % of peak
against the reference's 4.7 % (and our body, a 80 %-clean sub, has only a 0.6 %
step - so the edge stands out 11x over the body instead of the reference's 3.3x).

Two softer candidates:

- **G1 keytrack**: `filter_1_keytrack = 1.0`, no sweep route. If Vital's keytrack
  follows the *voice* pitch (the pitch envelope included), the cutoff follows the
  drop smoothly, the level stays even through the sweep and the filter is never
  wider than the fundamental needs. If it follows only the MIDI note, nothing
  changes at the onset - that is the measurement.
- **G2 gentle sweep**: the sweep route at +12 st (cutoff 50 -> 100 Hz at the
  attack), which passes the fundamental but attenuates the edge harder.
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
REFERENCE_H_DB = [0.0, -27.84, -19.63, -39.62, -23.95, -40.53, -29.48]
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
    "filter_1_cutoff": 60.0 + 12.0 * math.log2(50.0 / MIDI60_HZ),
    "filter_1_keytrack": 0.0,
}


def test_p20_probe_g_attack_options():
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
    watched = sorted({*CANDIDATE, "filter_1_style"})
    before = C.call(server, "plugin_read", {"instance": instance, "controls": watched, "include_display": True})
    before_values = {key: value.get("native") for key, value in ((before.get("data") or {}).get("values") or {}).items()}
    before_displays = {key: value.get("display") for key, value in ((before.get("data") or {}).get("values") or {}).items()}
    routes_before = [
        row
        for row in (C.call(server, "plugin_modulation", {"instance": instance, "op": "list"})["data"].get("routes") or [])
        if row.get("destination")
    ]

    report: dict[str, Any] = {
        "probe": "p20_probe_g_attack_options",
        "utc": H.utc_now(),
        "live_write": True,
        "track": {"index": track_index, "name": C.TRACK_NAME, "item": str(item), "window_start": window_start},
        "baseline_controls": before_values,
        "baseline_routes": routes_before,
        "reference_target_h_db": REFERENCE_H_DB,
        "arms": [],
    }

    def run(label: str, controls: dict[str, Any], sweep_depth_st: float | None) -> dict[str, Any]:
        request: dict[str, Any] = {
            "controls": controls,
            "routes_update": [{"slot": SLOT, "amount": 18.0 * AMOUNT_PER_ST}],
        }
        if sweep_depth_st:
            request["routes_create"] = [
                {"source": "env_2", "destination": "filter_1_cutoff", "depth": float(sweep_depth_st)}
            ]
        preview = C.call(server, "plugin_write", {"instance": instance, "request": request, "dry_run": True})
        assert (preview.get("data") or {}).get("would_write") is True, (label, preview)
        applied = C.call(server, "plugin_write", {"instance": instance, "request": request, "dry_run": False})
        data = applied.get("data") or {}
        assert applied.get("ok") is True and data.get("committed") is True, (label, applied)
        out = H.EVIDENCE_DIR / f"p20_probe_g_{label}__{H.run_stamp()}.wav"
        render = C.render_window(server, project, out, window_start)
        analysis = S.measure(out, label=label)
        h_db = (analysis["harmonics"].get("h_db") or [])[:9]
        fit = (
            round(float(sum((float(a) - float(b)) ** 2 for a, b in zip(h_db[:7], REFERENCE_H_DB[:7])) / 7.0) ** 0.5, 2)
            if len(h_db) >= 7 and all(value is not None for value in h_db[:7])
            else None
        )
        return {
            "label": label,
            "request": request,
            "verdict": (data.get("outcome") or {}).get("verdict"),
            "render": {"file": str(out), **render},
            "sha256": hashlib.sha256(out.read_bytes()).hexdigest()[:16],
            "routes_after_arm": [
                row
                for row in (C.call(server, "plugin_modulation", {"instance": instance, "op": "list"})["data"].get("routes") or [])
                if row.get("destination")
            ],
            "summary": {
                "h_db": h_db,
                "harmonic_rms_error_vs_reference_db": fit,
                "peak_dbfs": analysis["meta"]["peak_dbfs"],
                "rms_dbfs": analysis["meta"]["rms_dbfs"],
                "crest_db": round(float(analysis["meta"]["peak_dbfs"]) - float(analysis["meta"]["rms_dbfs"]), 2),
                "share_above_300hz": analysis["harmonics"].get("share_above_300hz"),
                "click_step_over_peak": analysis["attack"]["click_step_over_peak"],
                "click_step_vs_body_step": analysis["attack"]["click_step_vs_body_step"],
                "click_step_t_s": analysis["attack"]["click_step_t_s"],
                "level_db_at": analysis["attack"]["level_db_at"],
                "zero_crossings": analysis["zero_crossings"],
                "envelope_points_db": analysis["envelope_points_db"],
                "drop_start_st": analysis["pitch"].get("drop_start_st"),
                "settle_to_0.5st_s": analysis["pitch"].get("settle_to_0.5st_s"),
                "tail": analysis["tail"],
            },
            "analysis": analysis,
        }

    report["arms"].append(run("g1_keytrack", {**CANDIDATE, "filter_1_keytrack": 100.0}, None))
    report["arms"].append(run("g2_sweep12", CANDIDATE, 12.0))

    # restore the pre-probe state
    restore_controls: dict[str, Any] = {}
    for key in ("compressor_on", "distortion_on", "filter_1_style", "distortion_type"):
        if before_displays.get(key) is not None:
            restore_controls[key] = before_displays[key]
    for key, value in before_values.items():
        if key not in restore_controls and isinstance(value, (int, float)):
            restore_controls[key] = float(value)
    live_extra = [
        row
        for row in (C.call(server, "plugin_modulation", {"instance": instance, "op": "list"})["data"].get("routes") or [])
        if row.get("destination") and row.get("destination") != "osc_1_transpose"
    ]
    restore_request: dict[str, Any] = {
        "controls": restore_controls,
        "routes_update": [{"slot": SLOT, "amount": float(routes_before[0]["amount"]) if routes_before else 18.0 * AMOUNT_PER_ST}],
    }
    if live_extra:
        restore_request["routes_remove"] = [{"slot": int(row["slot"])} for row in live_extra]
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
        "matches_baseline": {
            key: abs(float(after_values[key]) - float(value)) <= 1e-3
            for key, value in before_values.items()
            if isinstance(value, (int, float)) and isinstance(after_values.get(key), (int, float))
        },
    }

    path = H.write_run_json("p20_probe_g_attack_options", report, H.EVIDENCE_DIR)
    print(f"evidence: {path}")
    for arm in report["arms"]:
        summary = arm["summary"]
        print(f"\n== {arm['label']} ({arm['verdict']})")
        print(f"   h {summary['h_db']}  fit {summary['harmonic_rms_error_vs_reference_db']} dB")
        print(f"   click {summary['click_step_over_peak']} of peak (x{summary['click_step_vs_body_step']} body) at {summary['click_step_t_s']} s")
        print(f"   level {json.dumps(summary['level_db_at'])}")
        print(f"   crossings {summary['zero_crossings']['crossings_ms']} half {summary['zero_crossings']['half_periods_ms']} -> {summary['zero_crossings']['implied_hz']} Hz")
        print(f"   env {json.dumps(summary['envelope_points_db'])}  drop {summary['drop_start_st']} st settle {summary['settle_to_0.5st_s']} s")
        print(f"   routes {json.dumps(arm['routes_after_arm'])}")
    print(f"\nrestore matches: {json.dumps(report['restore']['matches_baseline'])}")
    print(f"routes after restore: {json.dumps(routes_after)}")

    assert all(report["restore"]["matches_baseline"].values()), report["restore"]
    assert len(routes_after) == 1 and routes_after[0]["destination"] == "osc_1_transpose", routes_after
