"""P20 probe B - the compressor confound, the filter's real slope, and the source.

Probe A showed the onset ripple survives with the pitch route at depth 0 and
disappears when the filter is opened, and that the attenuation between harmonics
is ~11 dB/oct while the style parameter says "24dB". It also found an FX block
that no P19 patch ever set: the **multiband compressor is ON** (mix 0.85, low band
+16.3 dB, band +11.7 dB, thresholds -25..-36 dB).

Arms (one batch each, restored between them; the route depth is written as the
native amount because `routes_update` with a semantic depth is refused - D1):

- B1 compressor Off, everything else untouched
- B2 compressor Off + filter open at 2 kHz + route 0 (the clean source)
- B3 compressor Off + filter 58 Hz + route 0 (the filter's response, clean)
- B4 compressor Off + filter 116 Hz + route 0 (one octave up -> the slope)
- B5 compressor Off + filter 232 Hz + route 0 (two octaves up)
- B6 restore: compressor On, filter 58 Hz, route 12 st, verified
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
DISPLAY_OFFSET = 60.0
SLOT = 1
AMOUNT_12ST = 0.12473393976688385
BASELINE = {
    "filter_1_cutoff": 33.91945457458496,
    "volume": -1.0,
    "compressor_on": "On",
}


def cutoff_doc(hz: float) -> float:
    return DISPLAY_OFFSET + 12.0 * math.log2(hz / MIDI60_HZ)


def test_p20_probe_b_compressor_and_filter():
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
    watched = ["filter_1_cutoff", "volume", "compressor_on", "compressor_mix"]
    before = C.call(server, "plugin_read", {"instance": instance, "controls": watched, "include_display": True})
    before_values = {key: value.get("native") for key, value in ((before.get("data") or {}).get("values") or {}).items()}

    report: dict[str, Any] = {
        "probe": "p20_probe_b_compressor_and_filter",
        "utc": H.utc_now(),
        "live_write": True,
        "track": {"index": track_index, "name": C.TRACK_NAME, "item": str(item), "window_start": window_start},
        "baseline_read": before_values,
        "arms": [],
    }

    def apply(controls: dict[str, Any], amount: float, label: str) -> dict[str, Any]:
        request: dict[str, Any] = {"controls": controls, "routes_update": [{"slot": SLOT, "amount": amount}]}
        preview = C.call(server, "plugin_write", {"instance": instance, "request": request, "dry_run": True})
        assert (preview.get("data") or {}).get("would_write") is True, (label, preview)
        applied = C.call(server, "plugin_write", {"instance": instance, "request": request, "dry_run": False})
        data = applied.get("data") or {}
        assert applied.get("ok") is True and data.get("committed") is True, (label, applied)
        return {"label": label, "request": request, "verdict": (data.get("outcome") or {}).get("verdict")}

    def render_and_measure(label: str) -> dict[str, Any]:
        out = H.EVIDENCE_DIR / f"p20_probe_b_{label}__{H.run_stamp()}.wav"
        render = C.render_window(server, project, out, window_start)
        analysis = S.measure(out, label=label)
        return {
            "render": {"file": str(out), **render},
            "sha256": hashlib.sha256(out.read_bytes()).hexdigest()[:16],
            "summary": {
                "click_step_over_peak": analysis["attack"]["click_step_over_peak"],
                "click_step_t_s": analysis["attack"]["click_step_t_s"],
                "click_step_vs_body_step": analysis["attack"]["click_step_vs_body_step"],
                "level_db_at": analysis["attack"]["level_db_at"],
                "zero_crossings": analysis["zero_crossings"],
                "h_db": (analysis["harmonics"].get("h_db") or [])[:9],
                "noise_floor_db": analysis["harmonics"].get("noise_floor_db"),
                "share_above_300hz": analysis["harmonics"].get("share_above_300hz"),
                "body_f0_hz": analysis["pitch"]["body_f0_hz"],
                "drop_start_st": analysis["pitch"].get("drop_start_st"),
                "settle_to_0.5st_s": analysis["pitch"].get("settle_to_0.5st_s"),
                "envelope_points_db": analysis["envelope_points_db"],
                "peak_dbfs": analysis["meta"]["peak_dbfs"],
                "rms_dbfs": analysis["meta"]["rms_dbfs"],
                "tail": analysis["tail"],
            },
            "analysis": analysis,
        }

    # B1: compressor off only
    arm = {"b1_compressor_off": apply({"compressor_on": "Off"}, AMOUNT_12ST, "B1 compressor off")}
    arm.update(render_and_measure("b1_compressor_off"))
    report["arms"].append(arm)

    # B2..B5: compressor off, one filter value at a time, no pitch drop
    for label, hz in (("b2_source_open", 2000.0), ("b3_filter_58", 58.0), ("b4_filter_116", 116.0), ("b5_filter_232", 232.0)):
        arm = {
            f"{label}_apply": apply(
                {"compressor_on": "Off", "filter_1_cutoff": cutoff_doc(hz), "volume": -12.0}, 0.0, f"{label} ({hz} Hz, route 0)"
            )
        }
        arm.update(render_and_measure(label))
        report["arms"].append(arm)

    # B6: restore the baseline
    restore = apply(
        {"compressor_on": "On", "filter_1_cutoff": BASELINE["filter_1_cutoff"], "volume": BASELINE["volume"]},
        AMOUNT_12ST,
        "B6 restore",
    )
    after = C.call(server, "plugin_read", {"instance": instance, "controls": watched, "include_display": True})
    after_values = {key: value.get("native") for key, value in ((after.get("data") or {}).get("values") or {}).items()}
    routes_after = [
        row
        for row in (C.call(server, "plugin_modulation", {"instance": instance, "op": "list"})["data"].get("routes") or [])
        if row.get("destination")
    ]
    restore["restored_read"] = after_values
    restore["restored_routes"] = routes_after
    restore["host_text"] = {
        control_id: C.host_display(track, host_index[control_id])
        for control_id in watched
        if isinstance(host_index.get(control_id), int)
    }
    restore["matches_baseline"] = {
        key: (
            before_values.get(key) is not None
            and after_values.get(key) is not None
            and (
                abs(float(after_values[key]) - float(before_values[key])) <= 1e-3
                if isinstance(before_values[key], (int, float))
                else after_values[key] == before_values[key]
            )
        )
        for key in watched
    }
    report["restore"] = restore

    path = H.write_run_json("p20_probe_b_compressor_filter", report, H.EVIDENCE_DIR)
    print(f"evidence: {path}")
    for arm in report["arms"]:
        summary = arm.get("summary")
        if not summary:
            continue
        print(f"\n== {arm['render']['file']}")
        print(f"   click {summary['click_step_over_peak']} of peak at {summary['click_step_t_s']} s (x{summary['click_step_vs_body_step']} body step)")
        print(f"   level {summary['level_db_at']}   peak {summary['peak_dbfs']} dBFS  rms {summary['rms_dbfs']}")
        print(f"   crossings {summary['zero_crossings']['crossings_ms']}  half {summary['zero_crossings']['half_periods_ms']}")
        print(f"   h {summary['h_db']}  >300Hz {summary['share_above_300hz']}  floor {summary['noise_floor_db']} dB")
        print(f"   envelope {json.dumps(summary['envelope_points_db'])}")
        print(f"   drop {summary['drop_start_st']} st settle {summary['settle_to_0.5st_s']} s  tail {json.dumps(summary['tail'])}")
    print(f"\nrestore matches baseline: {json.dumps(report['restore']['matches_baseline'])}")
    print(f"routes: {json.dumps(routes_after)}")

    assert all(report["restore"]["matches_baseline"].values()), report["restore"]
    assert len(routes_after) == 1 and abs(float(routes_after[0]["amount"]) - AMOUNT_12ST) < 1e-3, routes_after
