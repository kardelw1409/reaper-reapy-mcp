"""P19 probe H — `voice_transpose`'s *true* value space (a measured law correction).

**The defect.** The registry's correspondence row for `voice_transpose` declares
`kind: affine, native -36..24`; the V3 profile therefore maps the caller's value
into host-normalized `(value + 36) / 60`. The *parameter's own* measured states
(`vital_scales_full.json`: 97 states, display `-48..48` over uniform steps) say
the display runs -48..+48 semitones **linearly in host-normalized**, i.e. the
neutral sits at normalized 0.5 - not at the value the corpus range implies. The
live consequence, measured: writing `voice_transpose = 0.0` (the value every
restore in this session used as "no transpose") lands at normalized 0.6 and the
instance plays **+10 semitones sharp**, on a route-free table, in tune before the
write.

This probe measures the relation so the correction is not a guess: it writes the
declared range's endpoints and several points between, reading the plugin's own
formatted value each time.

Ends by writing the value that displays "0 semitones" (so the instance is left in
tune), restoring the recorded patch values, and asserting the route table is
empty.
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
OSC1_TRANSPOSE_INDEX = 391
ENV2_KEYS = ["env_2_attack", "env_2_decay", "env_2_sustain", "env_2_release"]
P18_PATCH = json.loads((Path(__file__).resolve().parent / "p18_bass_patches.json").read_text(encoding="utf-8"))
BASELINE = {
    "osc_2_level": P18_PATCH["A"]["controls"]["osc_2_level"],
    "osc_1_level": P18_PATCH["A"]["controls"]["osc_1_level"],
    "osc_1_transpose": 0.0,
    "env_2_attack": 0.000499534,
    "env_2_decay": 1.0,
    "env_2_sustain": 1.0,
    "env_2_release": 0.0899194,
}
WRITE_POINTS = (24.0, 0.0, -24.0, 12.0, -12.0)


def test_probe_h_voice_transpose_space():
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
        "probe": "p19_voice_transpose_space",
        "utc": H.utc_now(),
        "live_write": True,
        "declared_law": "affine native -36..24 (corpus range)",
        "sweep_evidence": "97 states, display -48..48 over uniform normalized steps",
        "points": [],
    }

    def write(controls: dict[str, Any]) -> dict[str, Any]:
        payload = C.call(server, "plugin_write", {"instance": instance, "request": {"controls": controls}, "dry_run": False})
        assert payload.get("ok") is True, json.dumps(payload, default=str)[:800]
        data = payload.get("data") or {}
        return {"verdict": ((data.get("outcome") or {}).get("verdict"))}

    def snapshot() -> dict[str, Any]:
        values = C.call(server, "plugin_read", {"instance": instance, "controls": ["voice_transpose"], "include_display": True})["data"]["values"]["voice_transpose"]
        return {
            "v3_native": values.get("native"),
            "v3_display": values.get("display"),
            "host_display": C.host_display(track, VOICE_TRANSPOSE_INDEX),
            "host_norm": round(C.host_norm(track, VOICE_TRANSPOSE_INDEX), 6),
        }

    report["before"] = snapshot()
    try:
        for value in WRITE_POINTS:
            entry = {"written": value, "write": write({"voice_transpose": value})}
            entry.update(snapshot())
            report["points"].append(entry)
        # solve for "no transpose": the sweep says the display is linear in normalized
        # with 0 at 0.5; the declared space maps value -> (value+36)/60, so 0 at
        # normalized 0.5 needs value = 0.5*60 - 36 = -6
        candidate = -6.0
        entry = {"written": candidate, "label": "candidate_zero", "write": write({"voice_transpose": candidate})}
        entry.update(snapshot())
        report["points"].append(entry)
        report["conclusion"] = {
            "neutral_value_in_declared_space": candidate,
            "neutral_host_display": entry["host_display"],
            "relation": "host_normalized = (declared_value + 36) / 60; the plugin's own display = (host_normalized - 0.5) * 96 semitones",
        }
    finally:
        # leave the instance in tune: the measured neutral, then the recorded patch
        C.call(server, "plugin_write", {"instance": instance, "request": {"controls": {"voice_transpose": -6.0}}, "dry_run": False})
        write(dict(BASELINE))
        report["restore"] = snapshot()
        out = H.EVIDENCE_DIR / f"p19_tuned_baseline__{H.run_stamp()}.wav"
        report["render"] = C.render_window(server, project, out, window_start)
        report["analysis"] = C.analyse(out)
        report["routes"] = [
            row for row in (C.call(server, "plugin_modulation", {"instance": instance, "op": "list"})["data"].get("routes") or [])
            if row.get("destination")
        ]
        path = H.write_run_json("p19_voice_transpose_space", report, H.EVIDENCE_DIR)
        print(f"evidence: {path}")
        for entry in report["points"]:
            print(f"  written={entry['written']:>7} -> native={entry['v3_native']} norm={entry['host_norm']} display={entry['host_display']!r}")
        print(f"restore: {json.dumps(report['restore'], default=str)} routes={report['routes']}")
        print(f"tuned baseline offsets: {json.dumps(report['analysis']['steady_offset_st'])} "
              f"h2={report['analysis']['h2_db']} h3={report['analysis']['h3_db']} sub={report['analysis']['sub_below_120']}")

    assert report["restore"]["host_display"].strip().startswith("0"), report["restore"]
    for note, value in report["analysis"]["steady_offset_st"].items():
        assert value is not None and abs(value) < 0.35, report["analysis"]
