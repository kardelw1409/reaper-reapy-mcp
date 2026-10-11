"""P19 probe B2 — the pitch destinations' *actual* travel per amount unit.

**Why.** Probe B measured the depth-7 request (amount 0.07276146858930588)
producing **+17.03 st** on `voice_transpose` - 2.43x the provider's own depth
conversion (`native_per_unit = 96.19`, i.e. "7 st"). The fit derives the travel
from the destination's measured display list (here `-48..48`) and assumes the
amount is a fraction of it; that assumption was never checked against rendered
pitch (the POC's own open question). This probe measures it:

* `osc_1_transpose` at two amounts (2x step) - the corpus's standard pitch
  destination (444 routes), whose document value IS the displayed semitones;
* `voice_transpose` at a third amount - to see whether its 2.43x is a constant;
* the plugin's own display for a known `voice_transpose` document value, so the
  native<->display relation for that control is measured instead of assumed.

Every arm keeps `env_2` at sustain = 1 (a constant envelope), so the steady
pitch of each note IS the offset; `osc_2_level = 0` isolates the shifted
oscillator. One render per arm; the route, the controls and the base value are
restored and verified in the `finally`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

import live_harness as H
import probe_p19_common as C

pytestmark = pytest.mark.live_write

VOICE_TRANSPOSE_INDEX = 722
ENV2_KEYS = ["env_2_attack", "env_2_decay", "env_2_sustain", "env_2_release"]
TOUCHED = ["osc_2_level", "voice_transpose", *ENV2_KEYS]

#: The measured depth-7 amount from probe B (7 / 96.1948...); its half.
AMOUNT_FROM_DEPTH_7 = 0.07276146858930588
HALF_AMOUNT = AMOUNT_FROM_DEPTH_7 / 2.0
VOICE_AMOUNT = 0.05

P18_PATCH = json.loads((Path(__file__).resolve().parent / "p18_bass_patches.json").read_text(encoding="utf-8"))
RECORDED_BASELINE = {
    "osc_2_level": P18_PATCH["A"]["controls"]["osc_2_level"],
    "voice_transpose": 0.0,
    "env_2_attack": 0.000499534,
    "env_2_decay": 1.0,
    "env_2_sustain": 1.0,
    "env_2_release": 0.0899194,
}


def test_probe_b2_pitch_travel_per_amount():
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
    window_start, _item = C.paragraph_window(server, track_index)
    instance = {"track_index": track_index, "fx_index": 0}
    track = RPR.GetTrack(0, track_index)

    report: dict[str, Any] = {
        "probe": "p19_pitch_travel",
        "utc": H.utc_now(),
        "live_write": True,
        "track_index": track_index,
        "window_start": window_start,
        "recorded_baseline": RECORDED_BASELINE,
        "control_patch": {
            "osc_2_level": 0.0,
            "env_2_attack": 0.0,
            "env_2_decay": 1.0,
            "env_2_sustain": 1.0,
            "env_2_release": 0.1,
        },
        "arms": {},
    }
    renders: dict[str, Path] = {}
    slots_created: list[int] = []

    def call(name: str, args: dict) -> dict:
        payload = C.call(server, name, args)
        assert payload.get("ok") is True, json.dumps(payload, default=str)[:900]
        return payload

    def write(request: dict[str, Any]) -> dict[str, Any]:
        payload = call("plugin_write", {"instance": instance, "request": request, "dry_run": False})
        data = payload.get("data") or {}
        return {"verdict": (data.get("outcome") or {}).get("verdict"), "committed": data.get("committed")}

    def read_touched() -> dict[str, Any]:
        values = call("plugin_read", {"instance": instance, "controls": TOUCHED, "include_display": True})["data"]["values"]
        return {
            "native": {key: row.get("native") for key, row in values.items()},
            "display": {key: row.get("display") for key, row in values.items()},
            "host_voice_transpose": C.host_display(track, VOICE_TRANSPOSE_INDEX),
        }

    def active_routes() -> list[dict[str, Any]]:
        data = call("plugin_modulation", {"instance": instance, "op": "list"})["data"]
        return [row for row in data.get("routes") or [] if row.get("destination")]

    def render_arm(name: str) -> dict[str, Any]:
        out = H.EVIDENCE_DIR / f"p19_travel_{name}__{H.run_stamp()}.wav"
        result = C.render_window(server, project, out, window_start)
        renders[name] = out
        analysis = C.analyse(out)
        assert analysis["peak_dbfs"] > -40.0, (name, analysis)
        return {"render": result, "analysis": analysis}

    def same_value(left: Any, right: Any) -> bool:
        if isinstance(left, str) or isinstance(right, str):
            return str(left) == str(right)
        try:
            return abs(float(left) - float(right)) <= 6e-3
        except (TypeError, ValueError):
            return False

    def run_route_arm(name: str, destination: str, amount: float) -> None:
        request = {
            "routes_create": [{"source": "env_2", "destination": destination, "amount": amount, "bipolar": 0.0}],
            "controls": dict(report["control_patch"]),
        }
        report["arms"][name] = {"destination": destination, "amount": amount, "write": write(request)}
        live = active_routes()
        report["arms"][name]["routes"] = live
        assert len(live) == 1, live
        slots_created.append(int(live[0]["slot"]))
        report["arms"][name]["document_amount"] = live[0].get("amount")
        report["arms"][name]["power"] = live[0].get("power")
        report["arms"][name].update(render_arm(name))
        # the route is removed again before the next arm, so every arm starts
        # from the same route state (one active route at a time)
        call("plugin_modulation", {"instance": instance, "op": "remove", "selector": {"slot": int(live[0]["slot"])}})
        report["arms"][name]["removed"] = not active_routes()

    try:
        live = read_touched()
        report["before_live"] = live["display"]
        drifted = {key: live["display"].get(key) for key in TOUCHED if not same_value(live["display"].get(key), RECORDED_BASELINE.get(key))}
        if drifted:
            report["recovered"] = {"from": drifted, "to": RECORDED_BASELINE, "write": write({"controls": dict(RECORDED_BASELINE)})}
        report["before"] = read_touched()
        report["routes_before"] = active_routes()

        run_route_arm("osc1_depth7_amount", "osc_1_transpose", AMOUNT_FROM_DEPTH_7)
        run_route_arm("osc1_half_amount", "osc_1_transpose", HALF_AMOUNT)
        run_route_arm("voice_amount_0.05", "voice_transpose", VOICE_AMOUNT)

        # the base-value calibrations (display vs document) for both controls
        calibration = {}
        for label, control, index, values in (
            ("voice_transpose", "voice_transpose", VOICE_TRANSPOSE_INDEX, (12.0, -12.0)),
            ("osc_1_transpose", "osc_1_transpose", 391, (12.0, -12.0)),
        ):
            calibration[label] = {}
            for document_value in values:
                write({"controls": {control: document_value}})
                calibration[label][str(document_value)] = {
                    "host_display": C.host_display(track, index),
                    "v3_native": call("plugin_read", {"instance": instance, "controls": [control]})["data"]["values"][control].get("native"),
                }
            write({"controls": {control: 0.0}})
        report["transpose_calibration"] = calibration

        # ---- the reading -------------------------------------------------------
        def travel(name: str) -> float | None:
            arm = report["arms"][name]
            offsets = arm["analysis"]["steady_offset_st"]
            values = [value for value in offsets.values() if value is not None]
            if not values or arm["amount"] == 0:
                return None
            return round(float(sum(values) / len(values)) / arm["amount"], 4)

        report["verdict"] = {
            "travel_st_per_amount": {
                "osc_1_transpose@depth7_amount": travel("osc1_depth7_amount"),
                "osc_1_transpose@half_amount": travel("osc1_half_amount"),
                "voice_transpose@0.05": travel("voice_amount_0.05"),
            },
            "offsets": {
                name: report["arms"][name]["analysis"]["steady_offset_st"]
                for name in ("osc1_depth7_amount", "osc1_half_amount", "voice_amount_0.05")
            },
            "provider_depth_fit_says_st_per_unit": 96.1948,
            "voice_transpose_calibration": calibration,
        }
    finally:
        restore: dict[str, Any] = {}
        for slot in slots_created:
            C.call(server, "plugin_modulation", {"instance": instance, "op": "remove", "selector": {"slot": slot}})
        restore["controls"] = write({"controls": dict(RECORDED_BASELINE)})
        after = read_touched()
        restore["after"] = after
        restore["active_routes"] = active_routes()
        restore["values_match"] = all(same_value(after["display"].get(key), RECORDED_BASELINE.get(key)) for key in TOUCHED)
        restore["host_voice_transpose_is_zero"] = after["host_voice_transpose"].strip() in ("0", "0.00", "0 semitones")
        report["restore"] = restore
        path = H.write_run_json("p19_pitch_travel", report, H.EVIDENCE_DIR)
        print(f"evidence: {path}")
        print(f"travel per amount: {json.dumps(report.get('verdict', {}).get('travel_st_per_amount'))}")
        print(f"offsets: {json.dumps(report.get('verdict', {}).get('offsets'))}")
        print(f"voice_transpose calibration: {json.dumps(report.get('voice_transpose_calibration'))}")
        print(f"restore: {json.dumps({k: v for k, v in restore.items() if k != 'after'}, default=str)}")

    assert report["restore"]["values_match"], report["restore"]
    assert report["restore"]["active_routes"] == [], report["restore"]
