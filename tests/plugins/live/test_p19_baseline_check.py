"""P19 — read-only state check: which of the two *recorded* states is the instance in?

Run after any probe that touches the pitch. Two states are recorded for this
instance and both are checked against their own measured numbers:

- **P18 baseline**: no routes, `filter_1_cutoff` 50 (the pre-P19 patch), h2 ~ -6.8,
  sub ~ 0.956;
- **accepted 808 patch** (P19): one route `env_2 -> osc_1_transpose` with amount
  0.12473393976688385 and power 0, `filter_1_cutoff` 33.91945457458496, morph
  `None` @ 0, `osc_2` off, h2 ~ -22.2, sub ~ 0.9994, in tune within 0.35 st.

**Why it identifies the state instead of asserting only the baseline:** a check that
knows one state reports the *intended* final state as a failure (it did, at the end
of this round), and a check that accepts "no routes, in tune" cannot tell the P18
baseline from a half-restored instance. Anything that is **neither** recorded state
fails, with the measured rows printed either way.
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest

import live_harness as H
import probe_p19_common as C

pytestmark = pytest.mark.live_write  # only because it renders; it writes no plugin state

CONTROLS = ["osc_1_transpose", "voice_transpose", "osc_1_spectral_morph_type", "osc_1_spectral_morph_amount",
            "osc_2_level", "osc_1_level", "filter_1_cutoff", "env_2_sustain"]

#: The two recorded states, as measured (durations, amounts and metrics included).
P18_BASELINE = {
    "name": "P18 baseline",
    "routes": 0,
    "filter_1_cutoff": 50.0,
    "morph": ("None", 0.0),
    "h2_db": (-10.0, -4.0),
    "sub_below_120": (0.90, 0.98),
}
ACCEPTED_808 = {
    "name": "accepted 808 patch (P19)",
    "routes": 1,
    "route": {"source": "env_2", "destination": "osc_1_transpose", "amount": 0.12473393976688385, "power": 0.0},
    "filter_1_cutoff": 33.91945457458496,
    "morph": ("None", 0.0),
    "h2_db": (-24.0, -18.0),
    "sub_below_120": (0.98, 1.001),
}


def test_p19_baseline_check():
    try:
        import reapy

        project = reapy.Project()
        project.id  # noqa: B018 - reachability probe
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"REAPER is not reachable, so the check did not run: {exc}")

    os.environ.setdefault("REAPER_MCP_PLUGIN_PROVIDERS", "vital")
    from reaper_reapy_mcp.server import create_server

    server = create_server(debug=False)
    track_index = C.track_by_name(server)
    window_start, _item = C.paragraph_window(server, track_index)

    report: dict[str, Any] = {"probe": "p19_baseline_check", "utc": H.utc_now()}
    report["routes"] = C.call(server, "plugin_modulation", {"instance": {"track_index": track_index, "fx_index": 0}, "op": "list"})["data"]
    report["controls"] = C.call(
        server,
        "plugin_read",
        {"instance": {"track_index": track_index, "fx_index": 0}, "controls": CONTROLS, "include_display": True},
    )["data"]["values"]
    out = H.EVIDENCE_DIR / f"p19_baseline_check__{H.run_stamp()}.wav"
    report["render"] = C.render_window(server, project, out, window_start)
    report["analysis"] = C.analyse(out)
    path = H.write_run_json("p19_baseline_check", report, H.EVIDENCE_DIR)
    print(f"evidence: {path}")
    active = [row for row in report["routes"]["routes"] if row.get("destination")]
    print(f"active routes: {json.dumps(active, default=str)}")
    print("controls: " + json.dumps({k: v.get("display") for k, v in report["controls"].items()}, default=str))
    print(f"steady offsets: {json.dumps(report['analysis']['steady_offset_st'])}  "
          f"pitch={json.dumps(report['analysis']['steady_pitch_hz'])}")
    print(f"h2={report['analysis']['h2_db']} h3={report['analysis']['h3_db']} "
          f"sub={report['analysis']['sub_below_120']} centroid={report['analysis']['centroid_hz']} "
          f"peak={report['analysis']['peak_dbfs']} rms={report['analysis']['rms_dbfs']}")
    assert active == [] or len(active) == 1, f"an unexpected number of active routes: {active}"
    morph = (report["controls"]["osc_1_spectral_morph_type"].get("display"), float(report["controls"]["osc_1_spectral_morph_amount"].get("native") or 0.0))
    candidates = []
    for state in (P18_BASELINE, ACCEPTED_808):
        if len(active) != state["routes"] or morph != state["morph"]:
            continue
        if abs(float(report["controls"]["filter_1_cutoff"].get("native")) - state["filter_1_cutoff"]) > 0.01:
            continue
        low, high = state["h2_db"]
        if report["analysis"]["h2_db"] is None or not (low <= report["analysis"]["h2_db"] <= high):
            continue
        low, high = state["sub_below_120"]
        if not (low <= report["analysis"]["sub_below_120"] <= high):
            continue
        if state["routes"] == 1:
            route = active[0]
            expected = state["route"]
            if (route.get("source"), route.get("destination")) != (expected["source"], expected["destination"]):
                continue
            if abs(float(route.get("amount", 0.0)) - expected["amount"]) > 1e-6:
                continue
            if abs(float(route.get("power", 0.0)) - expected["power"]) > 1e-6:
                continue
        candidates.append(state["name"])
    report["state"] = {"identified": candidates, "routes": len(active), "morph": morph}
    H.write_run_json("p19_baseline_check", report, H.EVIDENCE_DIR)
    assert candidates, (
        "the instance is in neither recorded state; the measured rows are above "
        f"(routes={len(active)}, morph={morph}, h2={report['analysis']['h2_db']}, sub={report['analysis']['sub_below_120']}, "
        f"cutoff={report['controls']['filter_1_cutoff'].get('native')})"
    )
    print(f"state identified: {candidates}")
    for note, value in report["analysis"]["steady_offset_st"].items():
        assert value is not None and abs(value) < 0.35, f"the instance is not at its own pitch: {report['analysis']}"
