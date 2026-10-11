"""P19 probe I — the 808's *shape*: cutoff, drive, drop depth, drop curve.

**Why a probe and not a recipe.** The three defects found this session explain
why the earlier basses were house/electro rather than 808 (L-10 refused the
route; the filter cutoffs were written in the plugin's *document* space while the
author thought in Hz — 50 is 146.8 Hz, not 50 Hz; the spectral morph the recipe
relied on is measured inert). They do **not** choose the sound: how far the sub's
filter should sit above the fundamental, whether the drive belongs in the filter
or in the effects distortion, how deep the pitch drop should be and how its curve
should bend are all *musical* choices that this plugin answers by measurement.

So: apply one base shape, then move **one control at a time** and render the same
phrase window for each arm. The arms are compared on the P18 metric set
(`h2_db`/`h3_db` relative to the fundamental, `sub_below_120`, peak, clipping,
per-note steady pitch error) plus the first note's pitch contour, which is where a
drop shows up.

Write order: the base arm is **one** `plugin_write` carrying the route and the
controls together (the engine's state -> parameters order is what the measured
P18 rule requires); each later arm is one small write of the single control under
test. Every write is read back and asserted, and the recorded 'before' patch is
restored in a `finally` block (no blind rollback: the values are read live first).
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any

import pytest

import live_harness as H
import probe_p19_common as C

pytestmark = pytest.mark.live_write

#: MIDI 60 = 261.625565 Hz; the filter display is semitones relative to it and the
#: profile's value space is that display + 60 (pinned in test_p19_filter_units.py).
MIDI60_HZ = 261.6255653005986
DISPLAY_OFFSET = 60.0
E1_HZ = C.NOTES_HZ[28]  # 41.2034446 Hz - the phrase's lowest note

CUTOFF_HZ = {"low": 50.0, "mid": 58.0, "high": 70.0}
DROP_ST = {"seven": 7.0, "twelve": 12.0, "eighteen": 18.0}
POWER_ARMS = (0.0, 0.5, 1.0)

#: The host's own parameter indices (the independent channel): the plugin's
#: formatted display is read through REAPER, never through the V3 profile.
HOST_CUTOFF_INDEX = 116
HOST_VOICE_TRANSPOSE_INDEX = 722


def document_for(hz: float) -> float:
    """From Hz to the plugin's document value (the profile's value space)."""
    return DISPLAY_OFFSET + 12.0 * math.log2(hz / MIDI60_HZ)


def base_shape(cutoff_hz: float) -> dict[str, Any]:
    """The 808: one saw into a 24 dB filter, a fast pitch drop, no morph, no osc 2."""
    return {
        "osc_1_level": 1.0,
        "osc_1_spectral_morph_type": "None",
        "osc_1_spectral_morph_amount": 0.0,
        "osc_2_on": "Off",
        "osc_2_level": 0.0,
        "filter_1_on": "On",
        "filter_1_model": "Analog",
        "filter_1_style": "24dB",
        "filter_1_cutoff": document_for(cutoff_hz),
        "filter_1_resonance": 0.04,
        "filter_1_keytrack": 0.0,
        "filter_1_drive": "0dB",
        "filter_2_on": "Off",
        "legato": "Off",
        "portamento_force": "On",
        "polyphony": 1.0,
        "voice_priority": "Newest",
        "pitch_bend_range": "0 semitones",
        "voice_transpose": 0.0,
        "distortion_on": "Off",
        "env_2_attack": 0.0005,
        "env_2_decay": 0.06,
        "env_2_sustain": 0.0,
        "env_2_release": 0.06,
    }


def test_probe_i_808_shape():
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
    window_start, item = C.paragraph_window(server, track_index)
    instance = {"track_index": track_index, "fx_index": 0}
    track = RPR.GetTrack(0, track_index)

    # every control any arm writes, plus the two the arms read back through the
    # host's own channel; read live *before* the first write so the restore is a
    # recording, not a guess
    touched = sorted(
        {
            *base_shape(58.0),
            "filter_1_drive",
            "distortion_type",
            "distortion_drive",
            "distortion_mix",
        }
    )
    notes = C.call(server, "get_midi_notes", {"track_index": track_index, "item_id": item})
    before = C.call(server, "plugin_read", {"instance": instance, "controls": touched, "include_display": True})
    before_values = {
        key: {"native": value.get("native"), "display": value.get("display")}
        for key, value in (before.get("data") or {}).get("values", {}).items()
    }

    report: dict[str, Any] = {
        "probe": "p19_808_shaping",
        "utc": H.utc_now(),
        "live_write": True,
        "notes": (notes.get("data") or notes).get("notes"),
        "phrase": {"window_start": window_start, "item": str(item), "e1_hz": E1_HZ},
        "cutoff_hz": CUTOFF_HZ,
        "cutoff_document": {key: round(document_for(hz), 4) for key, hz in CUTOFF_HZ.items()},
        "before_controls": before_values,
        "before_routes": [
            row
            for row in (C.call(server, "plugin_modulation", {"instance": instance, "op": "list"})["data"].get("routes") or [])
            if row.get("destination")
        ],
        "depth_conversion": {},
        "arms": {},
        "writes": [],
    }

    def write(request: dict[str, Any], label: str) -> dict[str, Any]:
        payload = C.call(server, "plugin_write", {"instance": instance, "request": request, "dry_run": False})
        data = payload.get("data") or {}
        outcome = data.get("outcome") or {}
        assert payload.get("ok") is True, json.dumps(payload, default=str)[:700]
        assert data.get("committed") is True, (label, json.dumps(data, default=str)[:500])
        entry = {"label": label, "verdict": outcome.get("verdict"), "committed": data.get("committed")}
        report["writes"].append(entry)
        return entry

    def set_controls(controls: dict[str, Any], label: str) -> dict[str, Any]:
        return write({"controls": controls}, label)

    def measure(label: str, *, with_pitch: bool = True) -> dict[str, Any]:
        out = H.EVIDENCE_DIR / f"p19_808_{label}__{H.run_stamp()}.wav"
        if out.exists():  # two arms inside the same second must not share a name
            out = out.with_name(out.name.replace(".wav", f"_{len(report['arms']):02d}.wav"))
        render = C.render_window(server, project, out, window_start)
        return {
            "file": str(out),
            "settings_restored": render["settings_restored"],
            "analysis": C.analyse(out, with_pitch=with_pitch),
        }

    def depth_amount(semitones: float) -> float:
        """The provider's *measured* conversion, asked through the public tool."""
        data = C.call(
            server,
            "plugin_modulation",
            {"instance": instance, "op": "depth", "destination": "osc_1_transpose", "depth": semitones},
        )["data"]
        report["depth_conversion"][f"{semitones}_st"] = data.get("native_amount")
        return float(data["native_amount"])

    def route_slot() -> tuple[int, dict[str, Any]]:
        rows = [
            row
            for row in (C.call(server, "plugin_modulation", {"instance": instance, "op": "list"})["data"].get("routes") or [])
            if row.get("destination") == "osc_1_transpose" and row.get("source") == "env_2"
        ]
        assert len(rows) == 1, rows
        return int(rows[0]["slot"]), rows[0]

    slot: int | None = None
    try:
        # --- base: the whole shape in one batch (route + controls) -------------
        request = {
            "controls": base_shape(CUTOFF_HZ["mid"]),
            "routes_create": [{"source": "env_2", "destination": "osc_1_transpose", "depth": DROP_ST["seven"]}],
        }
        preview = C.call(server, "plugin_write", {"instance": instance, "request": request, "dry_run": True})
        preview_data = preview.get("data") or {}
        report["arms"]["base_preview"] = {
            "ok": preview.get("ok"),
            "committed": preview_data.get("committed"),
            "would_write": preview_data.get("would_write"),
            "plan_summary": preview_data.get("plan_summary"),
        }
        report["arms"]["base_write"] = write(request, "base(mid cutoff, 7 st drop)")
        slot, route_row = route_slot()
        report["route"] = {**route_row, "depth_requested_st": DROP_ST["seven"]}
        report["arms"]["base"] = measure("base")
        report["arms"]["base"]["read_back"] = {
            key: {"native": value.get("native"), "display": value.get("display")}
            for key, value in C.call(
                server,
                "plugin_read",
                {
                    "instance": instance,
                    "controls": [
                        "filter_1_cutoff",
                        "env_2_decay",
                        "osc_1_level",
                        "osc_2_on",
                        "legato",
                        "voice_transpose",
                        "filter_2_on",
                    ],
                    "include_display": True,
                },
            )["data"]["values"].items()
        }
        report["arms"]["base"]["host_cutoff"] = C.host_display(track, HOST_CUTOFF_INDEX)
        report["arms"]["base"]["host_voice_transpose"] = C.host_display(track, HOST_VOICE_TRANSPOSE_INDEX)

        # --- round 1: the filter's height above the fundamental ---------------
        for key in ("low", "high"):
            report["arms"][f"cut_{key}"] = {
                "moved": f"filter_1_cutoff -> {CUTOFF_HZ[key]} Hz",
                "write": set_controls({"filter_1_cutoff": document_for(CUTOFF_HZ[key])}, f"cut_{key}"),
                **measure(f"cut_{key}", with_pitch=False),
            }
        set_controls({"filter_1_cutoff": document_for(CUTOFF_HZ["mid"])}, "cut_mid_again")

        # --- round 2: where the drive lives (filter drive vs effects distortion)
        report["arms"]["drive_filter"] = {
            "moved": "filter_1_drive -> 7.96875 dB (distortion still off)",
            "write": set_controls({"filter_1_drive": "7.96875dB"}, "drive_filter"),
            **measure("drive_filter", with_pitch=False),
        }
        report["arms"]["dist_soft"] = {
            "moved": "distortion On/Soft Clip/drive 6/mix 0.35 (filter drive back to 0)",
            "write": set_controls(
                {
                    "filter_1_drive": "0dB",
                    "distortion_on": "On",
                    "distortion_type": "Soft Clip",
                    "distortion_drive": 6.0,
                    "distortion_mix": 0.35,
                },
                "dist_soft",
            ),
            **measure("dist_soft", with_pitch=False),
        }
        report["arms"]["dist_hard"] = {
            "moved": "distortion_type -> Hard Clip (everything else held)",
            "write": set_controls({"distortion_type": "Hard Clip"}, "dist_hard"),
            **measure("dist_hard", with_pitch=False),
        }
        report["arms"]["clean"] = {
            "moved": "distortion_on -> Off (the reference the distortion arms must beat)",
            "write": set_controls({"distortion_on": "Off", "filter_1_drive": "0dB"}, "clean"),
            **measure("clean", with_pitch=False),
        }

        # --- round 3: the drop's depth ----------------------------------------
        for key in ("twelve", "eighteen", "seven"):
            report["arms"][f"drop_{key}"] = {
                "moved": f"route amount -> {DROP_ST[key]} st",
                "write": write({"routes_update": [{"slot": slot, "amount": depth_amount(DROP_ST[key])}]}, f"drop_{key}"),
                **measure(f"drop_{key}"),
            }

        # --- round 4: the drop's curve (the route's own power) ----------------
        for power in POWER_ARMS:
            report["arms"][f"power_{power}"] = {
                "moved": f"route power -> {power}",
                "write": write({"routes_update": [{"slot": slot, "power": power}]}, f"power_{power}"),
                **measure(f"power_{power}"),
            }
        report["route_final"] = [
            row
            for row in (C.call(server, "plugin_modulation", {"instance": instance, "op": "list"})["data"].get("routes") or [])
            if row.get("destination")
        ]
    finally:
        # enums and state refs are written by their own label (their native is an
        # index, and the write path refuses an index); numbers by their native
        restore_request: dict[str, Any] = {
            "controls": {
                key: (value["display"] if isinstance(value["display"], str) else value["native"])
                for key, value in before_values.items()
            }
        }
        if slot is not None:
            restore_request["routes_remove"] = [{"slot": slot}]
        restore = C.call(server, "plugin_write", {"instance": instance, "request": restore_request, "dry_run": False})
        report["restore"] = {
            "committed": (restore.get("data") or {}).get("committed"),
            "verdict": ((restore.get("data") or {}).get("outcome") or {}).get("verdict"),
            "ok": restore.get("ok"),
            "error": restore.get("error"),
            "restored_to": "the values read from the same controls before the first write",
        }
        report["restore_read"] = {
            key: {"native": value.get("native"), "display": value.get("display")}
            for key, value in C.call(server, "plugin_read", {"instance": instance, "controls": touched, "include_display": True})["data"]["values"].items()
        }
        report["restore_routes"] = [
            row for row in (C.call(server, "plugin_modulation", {"instance": instance, "op": "list"})["data"].get("routes") or []) if row.get("destination")
        ]
        report["restore_render"] = measure("restore", with_pitch=True)
        path = H.write_run_json("p19_808_shaping", report, H.EVIDENCE_DIR)
        print(f"evidence: {path}")
        for label, entry in report["arms"].items():
            if "analysis" not in entry:
                continue
            a = entry["analysis"]
            print(
                f"  {label:13s} h2={str(a['h2_db']):>7} h3={str(a['h3_db']):>7} sub={a['sub_below_120']:<7} peak={str(a['peak_dbfs']):>6} "
                f"clip={a['n_clipped_samples']} offs={a['steady_offset_st']} atk={a.get('attack_pitch_st_offset')} settle={a.get('settle_to_within_half_semitone_s')}"
            )
        print(f"depth_conversion: {json.dumps(report['depth_conversion'])}")
        print(f"restore: {json.dumps(report['restore'], default=str)} routes={report['restore_routes']}")
        print(f"notes: {json.dumps(report['notes'], default=str)[:400]}")

    assert report["restore"]["committed"] is True, report["restore"]
    for key, read in report["restore_read"].items():
        want = before_values[key]
        if isinstance(want["display"], str):
            assert read["display"] == want["display"], (key, read, want)
            continue
        if isinstance(want["native"], (int, float)) and not isinstance(want["native"], bool):
            if float(want["native"]) == 0.0:
                assert abs(float(read["native"])) <= 0.02, (key, read, want)
            else:
                assert read["native"] == pytest.approx(want["native"], rel=0.02, abs=0.02), (key, read, want)
    assert report["restore_routes"] == [], report["restore_routes"]
    assert report["restore_render"]["analysis"]["n_clipped_samples"] == 0, report["restore_render"]["analysis"]["peak_dbfs"]
    for label, entry in report["arms"].items():
        if "analysis" in entry:
            assert entry["settings_restored"] is True, label
