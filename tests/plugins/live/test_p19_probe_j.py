"""P19 probe J — the enum bands, measured through REAPER, and the fix's proof.

**Why.** While shaping the 808, the *host's* formatted value at the filter-style
parameter read **"Notch Blend"** immediately after a write of `filter_1_style =
"24dB"` that had committed and verified `ALL_VERIFIED`. The V3's own read-back
agreed with the write ("24dB") because the same uniform arithmetic
(``index / (n - 1)``) ran in both directions; only the independent channel
disagreed. The registry's own sweeps say why: "24dB" occupies normalized
``[0.0625, 0.1875)``, while the uniform mapping writes **0.25**, which is inside
"Notch Blend"'s band.

This probe measures the control's bands **through the host** - the plugin's own
formatted strings over a normalized grid, written with `TrackFX_SetParamNormalized`
so the layer under test is not involved - and then verifies the corrected mapping
end-to-end: write through `plugin_write`, read through REAPER, compare.

Restores the control's original normalized value; the grid writes are direct
parameter writes, and the instance is left as found.
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest

import live_harness as H
import probe_p19_common as C

pytestmark = pytest.mark.live_write

#: The parameter the defect was found on, and its mirror (indexes from the
#: provider profile's binding - the probe reads them from the live profile rather
#: than restating them, because a hardcoded index is exactly how the first
#: version mislabelled this measurement).
GRID_STEPS = 64
STYLE_CONTROLS = ("filter_1_style", "filter_2_style")


def test_probe_j_enum_bands_through_the_host():
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
    instance = {"track_index": track_index, "fx_index": 0}
    track = RPR.GetTrack(0, track_index)

    profile = C.call(server, "plugin_find", {"instance": instance, "kind": "control", "limit": 4000})
    rows = (profile.get("data") or {}).get("matches") or []
    index_by_id = {str(row.get("id")): (row.get("binding") or {}).get("index") for row in rows}
    report: dict[str, Any] = {
        "probe": "p19_enum_bands_through_host",
        "utc": H.utc_now(),
        "live_write": True,
        "grid_steps": GRID_STEPS,
        "controls": {},
    }

    def band_map(index: int) -> dict[str, Any]:
        """The plugin's own labels over the normalized grid (direct host writes)."""
        grid: list[dict[str, Any]] = []
        for step in range(GRID_STEPS + 1):
            normalized = step / GRID_STEPS
            RPR.TrackFX_SetParamNormalized(track, 0, index, normalized)
            grid.append({"normalized": round(normalized, 6), "display": C.host_display(track, index)})
        spans: list[dict[str, Any]] = []
        for row in grid:
            label = row["display"]
            if spans and spans[-1]["label"] == label:
                continue
            spans.append({"label": label, "start": row["normalized"], "end": 1.0})
        for earlier, later in zip(spans, spans[1:]):
            earlier["end"] = later["start"]
        return {"grid": grid, "spans": spans}

    try:
        for control_id in STYLE_CONTROLS:
            index = index_by_id.get(control_id)
            assert isinstance(index, int), (control_id, index)
            original = C.host_norm(track, index)
            measured = band_map(index)
            RPR.TrackFX_SetParamNormalized(track, 0, index, original)
            report["controls"][control_id] = {
                "index": index,
                "original_normalized": round(float(original), 6),
                "restored_display": C.host_display(track, index),
                "measured_spans": measured["spans"],
                "grid": measured["grid"],
            }
        # --- the fix, verified end to end on the same instance ----------------
        probe: dict[str, Any] = {}
        for control_id in STYLE_CONTROLS:
            index = index_by_id[control_id]
            before_display = C.host_display(track, index)
            before_normalized = round(C.host_norm(track, index), 6)
            preview = C.call(
                server,
                "plugin_write",
                {"instance": instance, "request": {"controls": {control_id: "24dB"}}, "dry_run": True},
            )
            write = C.call(
                server,
                "plugin_write",
                {"instance": instance, "request": {"controls": {control_id: "24dB"}}, "dry_run": False},
            )
            read = C.call(server, "plugin_read", {"instance": instance, "controls": [control_id], "include_display": True})
            after_normalized = round(C.host_norm(track, index), 6)
            probe[control_id] = {
                "preview_would_write": (preview.get("data") or {}).get("would_write"),
                "preview_committed": (preview.get("data") or {}).get("committed"),
                "v3_read_back_display": read["data"]["values"][control_id].get("display"),
                "client_verdict": ((write.get("data") or {}).get("outcome") or {}).get("verdict"),
                "committed": (write.get("data") or {}).get("committed"),
                "host_display_before_write": before_display,
                "host_normalized_before_write": before_normalized,
                "host_display_after_write": C.host_display(track, index),
                "host_normalized_after_write": after_normalized,
                "span_containing_the_written_value": next(
                    (
                        span
                        for span in report["controls"][control_id]["measured_spans"]
                        if span["start"] <= after_normalized <= span["end"]
                    ),
                    None,
                ),
            }
            RPR.TrackFX_SetParamNormalized(track, 0, index, report["controls"][control_id]["original_normalized"])
        report["fix_verification"] = probe
    finally:
        report["restored"] = {
            control_id: C.host_display(track, index_by_id[control_id])
            for control_id in STYLE_CONTROLS
            if isinstance(index_by_id.get(control_id), int)
        }
        report["restored_matches_original"] = {
            control_id: report["controls"][control_id]["restored_display"] == report["restored"][control_id]
            for control_id in STYLE_CONTROLS
            if control_id in report["controls"]
        }
        path = H.write_run_json("p19_enum_bands_through_host", report, H.EVIDENCE_DIR)
        print(f"evidence: {path}")
        for control_id, entry in report["controls"].items():
            print(f"  {control_id} (index {entry['index']}): original norm {entry['original_normalized']} -> {entry['restored_display']!r}")
            for span in entry["measured_spans"]:
                print(f"     host band {span['label']!r:<16} [{span['start']:.6f}, {span['end']:.6f})")
        for control_id, entry in (report.get("fix_verification") or {}).items():
            print(
                f"  write {control_id}='24dB': host {entry['host_display_before_write']!r} @ {entry['host_normalized_before_write']} "
                f"-> {entry['host_display_after_write']!r} @ {entry['host_normalized_after_write']} (v3 read-back {entry['v3_read_back_display']!r}), "
                f"written value lands in {entry['span_containing_the_written_value']}"
            )
        print(f"restored: {json.dumps(report['restored'], default=str)} matches={report['restored_matches_original']}")

    for control_id, entry in (report.get("fix_verification") or {}).items():
        assert entry["committed"] is True, (control_id, entry)
        assert entry["host_display_after_write"] == "24dB", (control_id, entry)
        span = entry["span_containing_the_written_value"]
        assert span is not None and span["label"] == "24dB", (control_id, span)
        # the counter-example, on the same instance: the uniform mapping writes 0.25,
        # which the host formats as a *different* member (this is the defect: the
        # layer's own read-back said "24dB" for it)
        uniform = 0.25
        host_band = next(
            (s for s in report["controls"][control_id]["measured_spans"] if s["start"] <= uniform <= s["end"]), None
        )
        assert host_band is not None and host_band["label"] != "24dB", (control_id, host_band)
        assert abs(entry["host_normalized_after_write"] - uniform) > 0.01, (control_id, entry)
    assert all(report["restored_matches_original"].values()), report["restored_matches_original"]
    assert report["controls"], "the probe must have measured at least one control"
    for control_id, entry in report["controls"].items():
        labels = [span["label"] for span in entry["measured_spans"]]
        assert len(labels) >= 2, (control_id, labels)
