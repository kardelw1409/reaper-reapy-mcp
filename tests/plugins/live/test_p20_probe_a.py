"""P20 probe A - where the click comes from, and what the oscillator really is.

Four arms, each one small write batch, restored before the next one starts:

- **A0** no write at all: re-render the *current* patch and compare it with the
  P19 WAV - a reproducibility witness for every measurement in this round (if the
  waveform is not reproducible, the click's attribution would be about a moment,
  not about the patch);
- **A1 source**: filter opened to 2 kHz, the pitch route at depth 0, output -12 dB
  - what the oscillator emits on its own (saw? its harmonic series? its onset?);
- **A2 source + drop**: A1 with the route back at 12 st - does the onset click
  need the pitch sweep?
- **A3 filter, no drop**: the filter back at the patch's 58 Hz, route depth 0 -
  does the click need the pitch sweep, or is the *filter* the thing that drags the
  waveform through the attack?
- **A4 restore**: the baseline controls and the route depth written back, verified
  through the layer *and* the host, then rendered again (a second reproducibility
  check, and the state the next probe must find).

Everything this probe writes is recorded *before* the first write and restored
before the artifact is written: `filter_1_cutoff`, `volume`, and route slot 1's
depth. No structural operation, no `force`, no project save.
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
BASELINE_CUTOFF_DOC = 33.91945457458496  # the patch's 58 Hz
BASELINE_VOLUME = -1.0
BASELINE_DEPTH = 12.0
QUIET_VOLUME = -12.0
OPEN_CUTOFF_HZ = 2000.0
OPEN_CUTOFF_DOC = DISPLAY_OFFSET + 12.0 * math.log2(OPEN_CUTOFF_HZ / MIDI60_HZ)


def _apply(server: Any, instance: dict[str, Any], *, controls: dict[str, Any], depth: float, label: str) -> dict[str, Any]:
    """Preview then apply one batch; both legs must agree.

    The route depth is written as the **native amount**, not as a semantic depth:
    `routes_update` with `depth` is refused by the codec as "no field to change"
    (only `routes_create` converts a depth), which is recorded as defect D1 of
    this round and fixed in the provider afterwards. The amount for 12 st was
    measured in P19 (0.12473393976688385 = 12 st x 96.1948 st per unit).
    """
    amount = 0.12473393976688385 * (float(depth) / 12.0)
    request: dict[str, Any] = {"controls": controls, "routes_update": [{"slot": SLOT, "amount": amount}]}
    preview = C.call(server, "plugin_write", {"instance": instance, "request": request, "dry_run": True})
    preview_data = preview.get("data") or {}
    assert preview_data.get("would_write") is True, (label, preview)
    applied = C.call(server, "plugin_write", {"instance": instance, "request": request, "dry_run": False})
    data = applied.get("data") or {}
    assert applied.get("ok") is True and data.get("committed") is True, (label, applied)
    return {
        "label": label,
        "request": request,
        "requested_depth_st": depth,
        "preview": {"would_write": preview_data.get("would_write"), "committed": preview_data.get("committed")},
        "applied": {"committed": data.get("committed"), "verdict": (data.get("outcome") or {}).get("verdict")},
    }


def test_p20_probe_a_onset_and_source():
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
    watched = ["filter_1_cutoff", "volume"]
    before = C.call(server, "plugin_read", {"instance": instance, "controls": watched, "include_display": True})
    before_values = {key: value.get("native") for key, value in ((before.get("data") or {}).get("values") or {}).items()}
    routes_before = [
        row
        for row in (C.call(server, "plugin_modulation", {"instance": instance, "op": "list"})["data"].get("routes") or [])
        if row.get("destination")
    ]

    report: dict[str, Any] = {
        "probe": "p20_probe_a_onset_and_source",
        "utc": H.utc_now(),
        "live_write": True,
        "track": {"index": track_index, "name": C.TRACK_NAME, "item": str(item), "window_start": window_start},
        "baseline_read": before_values,
        "baseline_routes": routes_before,
        "intent": {
            "open_cutoff_doc": OPEN_CUTOFF_DOC,
            "open_cutoff_hz": OPEN_CUTOFF_HZ,
            "quiet_volume": QUIET_VOLUME,
            "slot": SLOT,
        },
        "defect_recorded": {
            "D1": (
                "routes_update with a semantic `depth` is refused by the codec as 'no field to change' "
                "(codec.update recognizes source/destination and _ROUTE_FIELDS, which does not include depth; "
                "only routes_create calls provider.convert_depth) - the documented example in "
                "06-mcp-tool-contracts.md §9 and §8 cannot work. This probe therefore writes the native amount."
            )
        },
        "arms": [],
    }

    def render_and_measure(label: str) -> dict[str, Any]:
        out = H.EVIDENCE_DIR / f"p20_probe_a_{label}__{H.run_stamp()}.wav"
        render = C.render_window(server, project, out, window_start)
        analysis = S.measure(out, label=label)
        return {
            "render": {"file": str(out), **render},
            "analysis": analysis,
            "sha256": hashlib.sha256(out.read_bytes()).hexdigest()[:16],
            "summary": {
                "click_step_over_peak": analysis["attack"]["click_step_over_peak"],
                "click_step_t_s": analysis["attack"]["click_step_t_s"],
                "click_step_vs_body_step": analysis["attack"]["click_step_vs_body_step"],
                "level_db_at": analysis["attack"]["level_db_at"],
                "zero_crossings": analysis["zero_crossings"],
                "h_db": (analysis["harmonics"].get("h_db") or [])[:9],
                "body_f0_hz": analysis["pitch"]["body_f0_hz"],
                "drop_start_st": analysis["pitch"].get("drop_start_st"),
                "settle_to_0.5st_s": analysis["pitch"].get("settle_to_0.5st_s"),
                "share_above_300hz": analysis["harmonics"].get("share_above_300hz"),
            },
        }

    # --- A0: the current patch, re-rendered, compared with the P19 WAV --------
    a0 = render_and_measure("a0_current")
    previous = H.EVIDENCE_DIR / "p19_808_patch__20261011T143226Z.wav"
    identical = None
    if previous.is_file():
        import numpy as np

        fresh, fs_fresh = C.read_mono(Path(a0["render"]["file"]))
        old, fs_old = C.read_mono(previous)
        identical = bool(len(fresh) == len(old) and fs_fresh == fs_old and np.array_equal(fresh, old))
        if not identical and len(fresh) == len(old):
            difference = np.abs(fresh - old)
            a0["difference_vs_p19"] = {
                "max_abs": round(float(difference.max()), 8),
                "n_samples_over_1e-4": int(np.sum(difference > 1e-4)),
                "first_difference_s": round(float(np.argmax(difference > 1e-4)) / fs_fresh, 6),
            }
        else:
            a0["difference_vs_p19"] = {"same_length": len(fresh) == len(old), "fs": [fs_fresh, fs_old]}
    a0["bit_identical_to_p19_render"] = identical
    report["arms"].append(a0)

    # --- A1..A3 ---------------------------------------------------------------
    report["arms"].append(
        {
            "a1_source_open": _apply(
                server,
                instance,
                controls={"filter_1_cutoff": OPEN_CUTOFF_DOC, "volume": QUIET_VOLUME},
                depth=0.0,
                label="A1 source open, route 0",
            ),
            **render_and_measure("a1_source_open"),
        }
    )
    report["arms"].append(
        {
            "a2_source_drop": _apply(
                server,
                instance,
                controls={"filter_1_cutoff": OPEN_CUTOFF_DOC, "volume": QUIET_VOLUME},
                depth=BASELINE_DEPTH,
                label="A2 source open, route 12 st",
            ),
            **render_and_measure("a2_source_drop"),
        }
    )
    report["arms"].append(
        {
            "a3_filter_no_drop": _apply(
                server,
                instance,
                controls={"filter_1_cutoff": BASELINE_CUTOFF_DOC, "volume": QUIET_VOLUME},
                depth=0.0,
                label="A3 filter 58 Hz, route 0",
            ),
            **render_and_measure("a3_filter_no_drop"),
        }
    )

    # --- A4: restore and verify ----------------------------------------------
    restore = _apply(
        server,
        instance,
        controls={"filter_1_cutoff": BASELINE_CUTOFF_DOC, "volume": BASELINE_VOLUME},
        depth=BASELINE_DEPTH,
        label="A4 restore baseline",
    )
    after = C.call(server, "plugin_read", {"instance": instance, "controls": watched, "include_display": True})
    after_values = {key: value.get("native") for key, value in ((after.get("data") or {}).get("values") or {}).items()}
    routes_after = [
        row
        for row in (C.call(server, "plugin_modulation", {"instance": instance, "op": "list"})["data"].get("routes") or [])
        if row.get("destination")
    ]
    host_after = {
        control_id: C.host_display(track, host_index[control_id])
        for control_id in watched
        if isinstance(host_index.get(control_id), int)
    }
    restore["restored_read"] = after_values
    restore["restored_routes"] = routes_after
    restore["host_text"] = host_after
    restore["matches_baseline"] = {
        key: (
            before_values.get(key) is not None
            and after_values.get(key) is not None
            and abs(float(after_values[key]) - float(before_values[key])) <= 1e-3
        )
        for key in watched
    }
    report["restore"] = restore
    report["arms"].append({"a4_restore_render": render_and_measure("a4_restored")})

    path = H.write_run_json("p20_probe_a_onset_source", report, H.EVIDENCE_DIR)
    print(f"evidence: {path}")
    print(f"A0 re-render bit-identical to the P19 WAV: {report['arms'][0]['bit_identical_to_p19_render']}")
    for arm in report["arms"]:
        summary = arm["summary"]
        print(f"\n== {arm.get('render', {}).get('file', '')}")
        print(f"   click {summary['click_step_over_peak']} of peak at {summary['click_step_t_s']} s, vs body step x{summary['click_step_vs_body_step']}")
        print(f"   level {summary['level_db_at']}  f0 {summary['body_f0_hz']} Hz")
        print(f"   crossings {summary['zero_crossings']['crossings_ms']}")
        print(f"   half periods {summary['zero_crossings']['half_periods_ms']} -> implied {summary['zero_crossings']['implied_hz']} Hz")
        print(f"   drop {summary['drop_start_st']} st, settle {summary['settle_to_0.5st_s']} s; h {summary['h_db']}; >300Hz {summary['share_above_300hz']}")
    print(f"\nrestore matches baseline: {json.dumps(report['restore']['matches_baseline'])}; host: {json.dumps(host_after)}")
    print(f"routes after restore: {json.dumps(routes_after)}")

    assert report["restore"]["matches_baseline"]["filter_1_cutoff"] is True, report["restore"]
    assert abs(float(after_values["volume"]) - float(before_values["volume"])) <= 1e-3, report["restore"]
    assert len(routes_after) == 1 and abs(float(routes_after[0]["amount"]) - 0.124733943) < 1e-3, routes_after
