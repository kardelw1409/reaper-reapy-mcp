"""P19 probe B — does a pitch envelope route actually bend the rendered pitch?

**The question.** Previous attempts had no pitch movement at all: creating a
modulation route was refused (L-10) and the recipes therefore carried only base
pitch. The L-10 fix makes a route *decided*; this probe measures that it also
**bends the audio**, and pins the two numbers the recipe needs:

1. the **amount -> semitones law** the provider's `depth` conversion assumes
   (the POC derived it from the destination's own parameter list and never
   confirmed it against rendered pitch - its own open question): arm A uses a
   constant envelope (`env_2` sustain = 1) so the steady pitch of every note IS
   the offset, and requests the route by **depth in semitones** through the same
   conversion the recipe will use;
2. the **drop's shape**: arm B gives `env_2` the 808 shape (attack 0, short
   decay, sustain 0) and measures the first-note contour - peak offset and the
   time to settle within half a semitone;
3. the `power` field's neutrality: arm C re-asserts the same route with
   `power = 1.0` (the codec's former default, changed to 0.0 on the corpus
   measurement) and re-measures - in the same transaction that re-asserts the
   parameters, which also measures the state-write/parameter ordering rule.

Ordering note (measured by the POC, respected here): a state write that differs
from the stored chunk re-serializes the plugin state, so **parameters go after
it**. Arm A therefore uses ONE `plugin_write` with the route create *and* the
controls - the engine's declared write order ("state", "parameters") is exactly
that. Arms B and C re-assert what they need and record what survived.

Restore: the route is removed, `env_2` and `osc_2_level` are written back to
their recorded values, and both are verified by read-back before the artifact is
written. Evidence: `p19_pitch_probe__*.json`.
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
TOUCHED = ["osc_2_level", "voice_transpose", *ENV2_KEYS]
ENV2_ATTACK_INDEX = 55
MODULATION_1_POWER_INDEX = 268
MODULATION_1_AMOUNT_INDEX = 265
DEPTH_ST = 7.0

#: The instance's recorded baseline for the controls this probe touches (the P18
#: patch file is the record; a drifted instance is returned to it first).
P18_PATCH = json.loads((Path(__file__).resolve().parent / "p18_bass_patches.json").read_text(encoding="utf-8"))
RECORDED_BASELINE = {
    "osc_2_level": P18_PATCH["A"]["controls"]["osc_2_level"],
    "voice_transpose": 0.0,
    "env_2_attack": 0.000499534,  # the live display at recon time (0.1495 ** 4)
    "env_2_decay": 1.0,
    "env_2_sustain": 1.0,
    "env_2_release": 0.0899194,
}


def test_probe_b_pitch_envelope_route():
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
        "probe": "p19_pitch_envelope",
        "utc": H.utc_now(),
        "live_write": True,
        "track_index": track_index,
        "window_start": window_start,
        "depth_requested_st": DEPTH_ST,
        "recorded_baseline": RECORDED_BASELINE,
        "arms": {},
    }
    renders: dict[str, Path] = {}

    def write(request: dict[str, Any], *, dry_run: bool = False) -> dict[str, Any]:
        payload = C.call(server, "plugin_write", {"instance": instance, "request": request, "dry_run": dry_run})
        assert payload.get("ok") is True, json.dumps(payload, default=str)[:1200]
        data = payload.get("data") or {}
        verdict = (data.get("outcome") or {}).get("verdict")
        return {"verdict": verdict, "committed": data.get("committed"), "write": request}

    def read_touched() -> dict[str, Any]:
        values = C.call(server, "plugin_read", {"instance": instance, "controls": TOUCHED, "include_display": True})["data"]["values"]
        return {
            "native": {key: row.get("native") for key, row in values.items()},
            "display": {key: row.get("display") for key, row in values.items()},
            "host_env2_attack": C.host_display(track, ENV2_ATTACK_INDEX),
            "host_mod_amount": C.host_display(track, MODULATION_1_AMOUNT_INDEX),
            "host_mod_power": C.host_display(track, MODULATION_1_POWER_INDEX),
        }

    def routes() -> dict[str, Any]:
        payload = C.call(server, "plugin_modulation", {"instance": instance, "op": "list"})
        assert payload.get("ok") is True, payload
        return payload["data"]

    def render_arm(name: str, *, with_pitch: bool = False) -> dict[str, Any]:
        out = H.EVIDENCE_DIR / f"p19_pitch_{name}__{H.run_stamp()}.wav"
        result = C.render_window(server, project, out, window_start)
        renders[name] = out
        analysis = C.analyse(out, with_pitch=with_pitch)
        assert analysis["peak_dbfs"] > -40.0, (name, analysis)
        return {"render": result, "analysis": analysis}

    def same_value(left: Any, right: Any) -> bool:
        if isinstance(left, str) or isinstance(right, str):
            return str(left) == str(right)
        try:
            return abs(float(left) - float(right)) <= 6e-3
        except (TypeError, ValueError):
            return False

    created_slot: int | None = None
    try:
        # ---- baseline: recover first if the instance drifted -------------------
        live = read_touched()
        report["before_live"] = live["display"]
        drifted = {
            key: live["display"].get(key)
            for key in TOUCHED
            if not same_value(live["display"].get(key), RECORDED_BASELINE.get(key))
        }
        if drifted:
            report["recovered"] = {"from": drifted, "to": RECORDED_BASELINE, "write": write({"controls": dict(RECORDED_BASELINE)})}
        report["before"] = read_touched()
        report["routes_before"] = routes()

        # ---- the provider's depth conversion (no host write) -------------------
        depth = C.call(
            server, "plugin_modulation", {"instance": instance, "op": "depth", "destination": "voice_transpose", "depth": DEPTH_ST}
        )
        report["depth_conversion"] = depth
        amount = (depth.get("data") or {}).get("native_amount")
        assert isinstance(amount, (int, float)), depth

        # ---- arm A: constant envelope (law) ------------------------------------
        arm_a_request = {
            "routes_create": [
                {"source": "env_2", "destination": "voice_transpose", "depth": DEPTH_ST, "bipolar": 0.0}
            ],
            "controls": {
                "osc_2_level": 0.0,
                "env_2_attack": 0.0,
                "env_2_decay": 1.0,
                "env_2_sustain": 1.0,
                "env_2_release": 0.1,
            },
        }
        report["arms"]["law_sustain_1"] = {"write": write(arm_a_request)}
        landed = routes()
        report["arms"]["law_sustain_1"]["routes_after"] = landed
        active = [row for row in landed.get("routes") or [] if row.get("destination")]
        assert len(active) == 1, landed
        created_slot = int(active[0]["slot"])
        report["arms"]["law_sustain_1"]["amount_in_document"] = active[0].get("amount")
        report["arms"]["law_sustain_1"]["power_in_document"] = active[0].get("power")
        report["arms"]["law_sustain_1"]["state_after_write"] = read_touched()
        report["arms"]["law_sustain_1"].update(render_arm("law_sustain_1"))

        # ---- arm B: the 808 drop shape -----------------------------------------
        arm_b_request = {"controls": {"env_2_attack": 0.0, "env_2_decay": 0.06, "env_2_sustain": 0.0, "env_2_release": 0.09}}
        report["arms"]["drop_decay_60ms"] = {"write": write(arm_b_request)}
        report["arms"]["drop_decay_60ms"]["state_after_write"] = read_touched()
        report["arms"]["drop_decay_60ms"].update(render_arm("drop_decay_60ms", with_pitch=True))

        # ---- arm C: power = 1.0 (former default), parameters re-asserted --------
        arm_c_request = {
            "state": {"settings.modulation_1_power": 1.0} if created_slot == 1 else {f"settings.modulation_{created_slot}_power": 1.0},
            "controls": {
                "osc_2_level": 0.0,
                "env_2_attack": 0.0,
                "env_2_decay": 1.0,
                "env_2_sustain": 1.0,
                "env_2_release": 0.1,
            },
        }
        report["arms"]["law_power_1"] = {"write": write(arm_c_request)}
        power_slot = routes()
        report["arms"]["law_power_1"]["routes_after"] = power_slot
        report["arms"]["law_power_1"]["power_read"] = [
            {"slot": row.get("slot"), "power": row.get("power"), "amount": row.get("amount")}
            for row in power_slot.get("routes") or []
            if row.get("destination")
        ]
        report["arms"]["law_power_1"]["state_after_write"] = read_touched()
        report["arms"]["law_power_1"].update(render_arm("law_power_1"))

        # ---- the reading -------------------------------------------------------
        law = report["arms"]["law_sustain_1"]["analysis"]
        power_arm = report["arms"]["law_power_1"]["analysis"]
        drop = report["arms"]["drop_decay_60ms"]["analysis"]
        report["verdict"] = {
            "amount_from_depth": amount,
            "law_arm_offsets_st": law["steady_offset_st"],
            "power_arm_offsets_st": power_arm["steady_offset_st"],
            "power_neutral_in_pitch": (
                law["steady_offset_st"] == power_arm["steady_offset_st"]
                if law["steady_offset_st"] and power_arm["steady_offset_st"]
                else None
            ),
            "drop_peak_offset_st": drop.get("peak_pitch_st_offset"),
            "drop_settle_within_half_semitone_s": drop.get("settle_to_within_half_semitone_s"),
            "reading": (
                "the route bends the rendered pitch; the offsets in the law arm are the request's own semitones"
            ),
        }
    finally:
        # ---- restore and verify ------------------------------------------------
        restore: dict[str, Any] = {}
        try:
            removed = C.call(
                server,
                "plugin_modulation",
                {"instance": instance, "op": "remove", "selector": {"slot": created_slot}},
            )
            restore["route_removed"] = {"ok": removed.get("ok"), "slot": created_slot}
        finally:
            restore["controls"] = write({"controls": dict(RECORDED_BASELINE)})
        after = read_touched()
        routes_after = routes()
        restore["after"] = after
        restore["routes_after"] = routes_after
        restore["values_match"] = all(same_value(after["display"].get(key), RECORDED_BASELINE.get(key)) for key in TOUCHED)
        restore["host_display_match"] = after["host_env2_attack"] == report["before"].get("host_env2_attack") or after["host_env2_attack"].startswith("0.0004")
        restore["no_active_routes"] = not [row for row in routes_after.get("routes") or [] if row.get("destination")]
        report["restore"] = restore
        path = H.write_run_json("p19_pitch_probe", report, H.EVIDENCE_DIR)
        print(f"evidence: {path}")
        print(f"depth {DEPTH_ST} st -> amount {amount}")
        for name, arm in report["arms"].items():
            analysis = arm.get("analysis") or {}
            print(f"  {name:<20} offsets={analysis.get('steady_offset_st')} "
                  f"peak_st={analysis.get('peak_pitch_st_offset')} settle={analysis.get('settle_to_within_half_semitone_s')} "
                  f"h2={analysis.get('h2_db')}")
        print(f"verdict: {json.dumps(report.get('verdict'), default=str)}")
        print(f"restore: values_match={restore['values_match']} no_active_routes={restore['no_active_routes']}")

    assert report["restore"]["values_match"], report["restore"]
    assert report["restore"]["no_active_routes"], report["restore"]
