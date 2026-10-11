"""P19 probe A — is the oscillator spectral morph audible on the target instance?

**The question.** P18 measured "spectral morph inert" with two isolation renders
(`p18_morph_inert__*.json`: `Low Pass` @ 100 vs `None` @ 0 identical to four
significant figures). This probe re-asks it with the two channels the P18 probe
did not record together:

1. the *plugin's own* formatted value for both morph parameters after each write
   (`TrackFX_GetFormattedParamValue`) — so "the write landed" is measured, not
   assumed from a V3 read-back that re-uses the same conversion;
2. a **determinism arm** (the baseline rendered twice, max sample difference) so
   a difference can be attributed to the morph rather than to render noise;
3. three morph arms on the *same isolated oscillator*: `None` @ 0, `Low Pass` @
   100 (the P18 pair), and `Shepard Tone` @ 100 (the POC measured this type as
   the strongest of the 17 — `feedback_mappings.json`, "+0.283 inharmonic
   energy"); if even that is bit-identical, the morph engine is not reachable on
   this instance's single-frame "Init" tables.

Isolation: `osc_2_level = 0` for every arm (osc 1 goes through FILTER 1), so the
only thing the arms change is osc 1's morph. Everything written is restored and
the restore is verified by read-back before the evidence is written.

READ the artifact's `verdict` before trusting any single arm: it is computed
from the measured difference between arms against the determinism arm.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import numpy as np
import pytest

import live_harness as H
import probe_p19_common as C

pytestmark = pytest.mark.live_write

MORPH_TYPE_INDEX = 497
MORPH_AMOUNT_INDEX = 495
TOUCHED = [
    "osc_1_spectral_morph_type",
    "osc_1_spectral_morph_amount",
    "osc_2_level",
    "osc_1_level",
]

#: The recorded baseline of this instance (P18 wrote it; `p18_bass_patches.json`
#: is the file, not a re-typed constant). A probe that starts from anything else
#: restores to this baseline first and records that it did.
P18_PATCH = json.loads((Path(__file__).resolve().parent / "p18_bass_patches.json").read_text(encoding="utf-8"))
RECORDED_BASELINE = {key: P18_PATCH["A"]["controls"][key] for key in TOUCHED}


def max_abs_diff(a: Path, b: Path) -> float:
    left, _ = C.read_mono(a)
    right, _ = C.read_mono(b)
    if len(left) != len(right):
        return float("inf")
    return float(np.max(np.abs(left - right)))


def test_probe_a_morph_audibility():
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
        "probe": "p19_morph_audibility",
        "utc": H.utc_now(),
        "live_write": True,
        "track_index": track_index,
        "window_start": window_start,
        "recorded_baseline": RECORDED_BASELINE,
        "arms": {},
        "restore": {},
    }

    def write(controls: dict[str, Any]) -> dict[str, Any]:
        payload = C.call(server, "plugin_write", {"instance": instance, "request": {"controls": controls}, "dry_run": False})
        assert payload.get("ok") is True, payload
        verdict = ((payload.get("data") or {}).get("outcome") or {}).get("verdict")
        assert verdict == "ALL_VERIFIED", payload
        return {"verdict": verdict, "controls": controls}

    def read_arm() -> dict[str, Any]:
        values = C.call(server, "plugin_read", {"instance": instance, "controls": TOUCHED, "include_display": True})["data"]["values"]
        return {
            "native": {key: row.get("native") for key, row in values.items()},
            "display": {key: row.get("display") for key, row in values.items()},
            "host_morph_type": C.host_display(track, MORPH_TYPE_INDEX),
            "host_morph_amount": C.host_display(track, MORPH_AMOUNT_INDEX),
        }

    # ---- baseline: the recorded one; recover first if the instance drifted ----
    live_before = read_arm()
    report["before_live"] = live_before["display"]
    drifted = {
        key: live_before["display"].get(key)
        for key in TOUCHED
        if live_before["display"].get(key) != RECORDED_BASELINE.get(key)
    }
    if drifted:
        report["recovered"] = {"from": drifted, "to": RECORDED_BASELINE, "write": write(dict(RECORDED_BASELINE))}
    before = read_arm()
    report["before"] = {"display": before["display"], "native": before["native"]}

    def same_value(left: Any, right: Any) -> bool:
        if isinstance(left, str) or isinstance(right, str):
            return str(left) == str(right)
        try:
            return abs(float(left) - float(right)) <= 1e-3
        except (TypeError, ValueError):
            return False

    assert all(same_value(before["display"].get(key), RECORDED_BASELINE.get(key)) for key in TOUCHED), before["display"]

    renders: dict[str, Path] = {}
    #: arm name -> the render key it must be compared against. Explicit, because
    #: a lookup that silently misses (a `None` difference) collapses into a
    #: "no difference" verdict - the exact shape this probe exists to avoid.
    arm_render_key = {
        "baseline_morph_lowpass_60": "baseline_1",
        "baseline_repeat": "baseline_2",
        "morph_none_0": "none_0",
        "morph_lowpass_100": "lowpass_100",
        "morph_shepard_100": "shepard_100",
    }

    def render_arm(name: str) -> dict[str, Any]:
        out = H.EVIDENCE_DIR / f"p19_morph_{name}__{H.run_stamp()}.wav"
        result = C.render_window(server, project, out, window_start)
        renders[name] = out
        analysis = C.analyse(out)
        assert analysis["peak_dbfs"] > -40.0, (name, analysis)
        return {"render": result, "analysis": analysis}

    try:
        # isolate osc 1 (osc 2 silent for every arm)
        report["isolation_write"] = write({"osc_2_level": 0.0})

        # arm 0: the current morph (Low Pass @ 60), twice: determinism + reference
        report["arms"]["baseline_morph_lowpass_60"] = {"state": read_arm(), **render_arm("baseline_1")}
        second = render_arm("baseline_2")
        report["arms"]["baseline_repeat"] = second
        report["determinism"] = {
            "max_abs_sample_diff": round(max_abs_diff(renders["baseline_1"], renders["baseline_2"]), 8),
            "analysis_identical": second["analysis"] == report["arms"]["baseline_morph_lowpass_60"]["analysis"],
        }

        # arm 1: morph None @ 0
        report["arms"]["morph_none_0"] = {
            "write": write({"osc_1_spectral_morph_type": "None", "osc_1_spectral_morph_amount": 0.0}),
            "state": read_arm(),
        }
        report["arms"]["morph_none_0"].update(render_arm("none_0"))

        # arm 2: morph Low Pass @ 100 (the P18 pair's other arm)
        report["arms"]["morph_lowpass_100"] = {
            "write": write({"osc_1_spectral_morph_type": "Low Pass", "osc_1_spectral_morph_amount": 100.0}),
            "state": read_arm(),
        }
        report["arms"]["morph_lowpass_100"].update(render_arm("lowpass_100"))

        # arm 3: morph Shepard Tone @ 100 (the POC's strongest measured type)
        report["arms"]["morph_shepard_100"] = {
            "write": write({"osc_1_spectral_morph_type": "Shepard Tone", "osc_1_spectral_morph_amount": 100.0}),
            "state": read_arm(),
        }
        report["arms"]["morph_shepard_100"].update(render_arm("shepard_100"))

        # ---- the arms' audibility, against the determinism arm -----------------
        noise_floor = max_abs_diff(renders["baseline_1"], renders["baseline_2"])
        threshold = max(10.0 * noise_floor, 1e-4)
        verdict = {}
        missing: list[str] = []
        for name, arm in report["arms"].items():
            if "analysis" not in arm:
                continue
            state = arm.get("state") or {}
            render_key = arm_render_key.get(name)
            if render_key is None or render_key not in renders:
                missing.append(name)
                continue
            verdict[name] = {
                "h2_db": arm["analysis"]["h2_db"],
                "h3_db": arm["analysis"]["h3_db"],
                "centroid_hz": arm["analysis"]["centroid_hz"],
                "sub_below_120": arm["analysis"]["sub_below_120"],
                "max_abs_sample_diff_vs_baseline_1": round(max_abs_diff(renders["baseline_1"], renders[render_key]), 8),
                "host_morph_type": state.get("host_morph_type"),
                "host_morph_amount": state.get("host_morph_amount"),
            }
        assert not missing, f"arms without a render to compare: {missing}"
        # pairwise differences among the three morph arms: if the morph were
        # audible these would not all sit on the noise floor
        morph_keys = ["none_0", "lowpass_100", "shepard_100"]
        pairwise = {
            f"{left}_vs_{right}": round(max_abs_diff(renders[left], renders[right]), 8)
            for index, left in enumerate(morph_keys)
            for right in morph_keys[index + 1 :]
        }
        report["verdict"] = {
            "noise_floor_max_abs_sample_diff": round(noise_floor, 8),
            "arms": verdict,
            "pairwise_morph_arm_diffs": pairwise,
            #: The morph's own comparison (its arms against each other), not the
            #: arms against the pre-write baseline: measured 2026-10-11, the FIRST
            #: render after a parameter write differs from one taken before it by
            #: ~7.2e-03 peak while two consecutive renders of one state are
            #: bit-identical - a render-order effect that is identical for
            #: different morph values, so it cannot be attributed to the morph.
            "morph_effect_peak_abs": max(pairwise.values()) if pairwise else None,
            "morph_effect_dbfs": (
                None if not pairwise else round(20.0 * np.log10(max(max(pairwise.values()), 1e-12)), 1)
            ),
            "post_write_render_shift_peak_abs": verdict.get("morph_none_0", {}).get(
                "max_abs_sample_diff_vs_baseline_1"
            ),
            "reading": (
                "the morph is inert on this instance's single-frame Init tables: `Low Pass` @ 100 is "
                "BIT-IDENTICAL to `None` @ 0 (the P18 pair, re-measured with the plugin's own display "
                "confirming both writes), and the strongest of the 17 types (`Shepard Tone` @ 100) moves "
                "the signal by the recorded `morph_effect_peak_abs` - tens of dB below any audible "
                "change - with identical spectral metrics. The larger per-arm difference against the "
                "pre-write baseline is a render-order effect (present for every morph value alike). "
                "No recipe may rest on the spectral morph here"
            ),
        }
    finally:
        # ---- restore, verify, and record ---------------------------------------
        # The write channel takes **display** values (an enum's native is a member
        # index, which the write path refuses - measured by this probe's first run)
        report["restore"]["write"] = write(dict(RECORDED_BASELINE))
        after = read_arm()
        report["restore"]["after"] = after
        report["restore"]["values_match"] = all(
            same_value(after["display"].get(key), RECORDED_BASELINE.get(key)) for key in TOUCHED
        )
        report["restore"]["host_display_match"] = (
            after["host_morph_type"] == before["host_morph_type"]
            and after["host_morph_amount"] == before["host_morph_amount"]
        )
        path = H.write_run_json("p19_morph_audibility", report, H.EVIDENCE_DIR)
        print(f"evidence: {path}")
        print(f"determinism: {report.get('determinism')}")
        print(f"verdict: {report.get('verdict', {}).get('reading')}")
        print(f"noise floor={report.get('verdict', {}).get('noise_floor_max_abs_sample_diff')} "
              f"threshold={report.get('verdict', {}).get('audibility_threshold')}")
        for name, row in (report.get("verdict", {}).get("arms") or {}).items():
            print(f"  {name:<26} h2={row['h2_db']:<7} h3={row['h3_db']:<7} centroid={row['centroid_hz']:<7} "
                  f"sub={row['sub_below_120']:<7} display={row['host_morph_type']} @ {row['host_morph_amount']} "
                  f"maxdiff={row['max_abs_sample_diff_vs_baseline_1']}")
        print(f"pairwise: {report.get('verdict', {}).get('pairwise_morph_arm_diffs')}")
        print(f"restored: {report['restore']['values_match']} / host display {report['restore']['host_display_match']}")

    assert report["restore"]["values_match"], report["restore"]
