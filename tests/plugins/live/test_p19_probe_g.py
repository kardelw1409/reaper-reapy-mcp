"""P19 probe G — `osc_1_transpose` (the corpus's own pitch destination), cleaned first.

Probe B measured `voice_transpose`: amount 0.07276146858930588 -> **+17.03 st**
(234 st per amount unit, while the provider's depth fit claims 96.19), and the
drop-shaped envelope fell only from +13.3 to **+10.1 st** and stayed there - the
note never returned to its own pitch, which is musically wrong for an 808.

The corpus prefers `osc_1_transpose` (444 routes vs 107 for `voice_transpose`;
`env_2 -> osc_1_transpose` alone is 75), so this probe measures *that*
destination, and cleans up first: the earlier probes left four routes on the
target instance (slots 1-4), each bending the pitch; they are removed with
`dry_run: false` and the empty table is asserted before any measurement.

Arms (route-only writes; controls after the state write, per the measured
ordering rule):

1. `env_2 -> osc_1_transpose`, amount 0.02, constant envelope -> pitch offsets;
2. the same route's amount updated to 0.01 -> pitch offsets (linearity);
3. the 808 drop shape (attack 0, decay 60 ms, sustain 0) -> the contour: does
   the pitch **return to the note**?

Restore: the route is removed, the controls are written back to the recorded P18
baseline, and the artifact records the verified end state (empty table, values,
host displays).
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

ENV2_KEYS = ["env_2_attack", "env_2_decay", "env_2_sustain", "env_2_release"]
TOUCHED = ["osc_2_level", "osc_1_transpose", *ENV2_KEYS]
AMOUNT_A = 0.02
AMOUNT_B = 0.01

P18_PATCH = json.loads((Path(__file__).resolve().parent / "p18_bass_patches.json").read_text(encoding="utf-8"))
RECORDED_BASELINE = {
    "osc_2_level": P18_PATCH["A"]["controls"]["osc_2_level"],
    "osc_1_transpose": 0.0,
    "env_2_attack": 0.000499534,
    "env_2_decay": 1.0,
    "env_2_sustain": 1.0,
    "env_2_release": 0.0899194,
}


def test_probe_g_osc_transpose_travel_and_drop():
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
        "probe": "p19_osc_transpose_travel",
        "utc": H.utc_now(),
        "live_write": True,
        "track_index": track_index,
        "window_start": window_start,
        "recorded_baseline": RECORDED_BASELINE,
        "arms": {},
    }
    created_slot: int | None = None

    def call(name: str, args: dict) -> dict:
        payload = C.call(server, name, args)
        return payload

    def must(name: str, args: dict) -> dict:
        payload = call(name, args)
        assert payload.get("ok") is True, f"{name}: {json.dumps(payload, default=str)[:900]}"
        return payload

    def write(request: dict[str, Any]) -> dict[str, Any]:
        payload = must("plugin_write", {"instance": instance, "request": request, "dry_run": False})
        data = payload.get("data") or {}
        outcome = data.get("outcome") or {}
        return {"verdict": outcome.get("verdict"), "host": outcome.get("host"), "plan": data.get("plan_summary")}

    def read_touched() -> dict[str, Any]:
        values = must("plugin_read", {"instance": instance, "controls": TOUCHED, "include_display": True})["data"]["values"]
        return {
            "native": {key: row.get("native") for key, row in values.items()},
            "display": {key: row.get("display") for key, row in values.items()},
            "host_env2_decay": C.host_display(track, 57),
        }

    def active_routes() -> list[dict[str, Any]]:
        data = must("plugin_modulation", {"instance": instance, "op": "list"})["data"]
        return [row for row in data.get("routes") or [] if row.get("destination")]

    def remove(slot: int) -> dict[str, Any]:
        payload = must("plugin_modulation", {"instance": instance, "op": "remove", "selector": {"slot": int(slot)}, "dry_run": False})
        data = payload.get("data") or {}
        outcome = data.get("outcome") or {}
        return {"verdict": outcome.get("verdict"), "committed": data.get("committed"), "host": outcome.get("host")}

    def render_arm(name: str, *, with_pitch: bool = False) -> dict[str, Any]:
        out = H.EVIDENCE_DIR / f"p19_osc_{name}__{H.run_stamp()}.wav"
        result = C.render_window(server, project, out, window_start)
        analysis = C.analyse(out, with_pitch=with_pitch)
        assert analysis["peak_dbfs"] > -40.0, (name, analysis)
        report["arms"][name] = {"render": result, "analysis": analysis}
        return analysis

    def same_value(left: Any, right: Any) -> bool:
        if isinstance(left, str) or isinstance(right, str):
            return str(left) == str(right)
        try:
            return abs(float(left) - float(right)) <= 6e-3
        except (TypeError, ValueError):
            return False

    try:
        report["entry"] = {"controls": read_touched(), "routes": active_routes()}

        # ---- clean up every route the earlier probes left --------------------
        report["cleanup"] = []
        for row in active_routes():
            report["cleanup"].append({"slot": row["slot"], "remove": remove(row["slot"])})
        remaining = active_routes()
        report["cleanup_remaining"] = remaining
        assert remaining == [], remaining

        # ---- arm 1: constant envelope, amount 0.02 ---------------------------
        created = must(
            "plugin_write",
            {
                "instance": instance,
                "request": {"routes_create": [{"source": "env_2", "destination": "osc_1_transpose", "amount": AMOUNT_A}]},
                "dry_run": False,
            },
        )
        report["arms"]["create"] = {"verdict": ((created.get("data") or {}).get("outcome") or {}).get("verdict")}
        live = active_routes()
        assert len(live) == 1, live
        created_slot = int(live[0]["slot"])
        report["arms"]["create"]["slot"] = created_slot
        report["controls_write_1"] = write(
            {"controls": {"osc_2_level": 0.0, "env_2_attack": 0.0, "env_2_decay": 1.0, "env_2_sustain": 1.0, "env_2_release": 0.1}}
        )
        report["arms"]["constant_0.02"] = {"analysis": render_arm("constant_0.02")}

        # ---- arm 2: the same route, amount 0.01 ------------------------------
        must(
            "plugin_write",
            {
                "instance": instance,
                "request": {"routes_update": [{"slot": created_slot, "amount": AMOUNT_B}]},
                "dry_run": False,
            },
        )
        report["arms"]["constant_0.01"] = {"analysis": render_arm("constant_0.01")}

        # ---- arm 3: the 808 drop shape ---------------------------------------
        report["controls_write_3"] = write({"controls": {"env_2_attack": 0.0, "env_2_decay": 0.06, "env_2_sustain": 0.0, "env_2_release": 0.09}})
        report["arms"]["drop_60ms"] = {"analysis": render_arm("drop_60ms", with_pitch=True)}
    finally:
        restore: dict[str, Any] = {"removals": [], "errors": []}
        try:
            for row in active_routes():
                restore["removals"].append({"slot": row["slot"], "remove": remove(row["slot"])})
            restore["controls"] = write({"controls": dict(RECORDED_BASELINE)})
        except Exception as exc:  # noqa: BLE001 - the artifact must exist
            restore["errors"].append(f"{type(exc).__name__}: {exc}")
        after = read_touched()
        restore["after"] = {"controls": after, "routes": active_routes()}
        restore["values_match"] = all(same_value(after["display"].get(key), RECORDED_BASELINE.get(key)) for key in TOUCHED)
        report["restore"] = restore

        constant_a = report["arms"].get("constant_0.02", {}).get("analysis") or {}
        constant_b = report["arms"].get("constant_0.01", {}).get("analysis") or {}
        drop = report["arms"].get("drop_60ms", {}).get("analysis") or {}

        def average_offset(analysis: dict[str, Any]) -> float | None:
            values = [value for value in (analysis.get("steady_offset_st") or {}).values() if value is not None]
            return round(float(sum(values) / len(values)), 3) if values else None

        offset_a = average_offset(constant_a)
        offset_b = average_offset(constant_b)
        report["verdict"] = {
            "offset_at_0.02_st": offset_a,
            "offset_at_0.01_st": offset_b,
            "travel_st_per_amount": None if not offset_a else round(offset_a / AMOUNT_A, 3),
            "linear_within_a_factor": (
                None if offset_a is None or offset_b is None else round(offset_a / max(offset_b, 1e-9), 3)
            ),
            "provider_depth_fit_says_st_per_unit": 96.1948,
            "drop_peak_st": drop.get("peak_pitch_st_offset"),
            "drop_steady_offset_st": drop.get("steady_offset_st"),
            "drop_settle_within_half_semitone_s": drop.get("settle_to_within_half_semitone_s"),
            "drop_contour_first_30": [row for row in (drop.get("contour") or [])[:30]],
        }
        path = H.write_run_json("p19_osc_transpose_travel", report, H.EVIDENCE_DIR)
        print(f"evidence: {path}")
        print(f"cleanup: {json.dumps(report.get('cleanup'), default=str)} -> remaining {report.get('cleanup_remaining')}")
        for name in ("constant_0.02", "constant_0.01", "drop_60ms"):
            analysis = report["arms"].get(name, {}).get("analysis") or {}
            print(f"  {name:<14} offsets={analysis.get('steady_offset_st')} peak={analysis.get('peak_pitch_st_offset')} "
                  f"settle={analysis.get('settle_to_within_half_semitone_s')} h2={analysis.get('h2_db')} sub={analysis.get('sub_below_120')}")
        print(f"verdict: travel/amount={report['verdict']['travel_st_per_amount']} "
              f"ratio(0.02/0.01)={report['verdict']['linear_within_a_factor']}")
        print(f"restore: values_match={restore['values_match']} routes={restore['after']['routes']}")

    assert report["restore"]["values_match"], report["restore"]
    assert report["restore"]["after"]["routes"] == [], report["restore"]
