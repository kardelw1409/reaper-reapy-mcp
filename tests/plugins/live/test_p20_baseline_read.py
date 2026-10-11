"""P20 - read-only baseline of the live instance before any mutation.

Nothing here writes: it resolves the target, lists the controls, reads every
control this round may touch (through the layer), and reads the host's own
formatted text for the same controls (the independent channel that found the P19
enum defect). The route table is recorded as the recovery *contract*: what a
later diagnostic arm must restore.

Evidence: `p20_baseline_read__*.json`.
"""

from __future__ import annotations

import json
from typing import Any

import live_harness as H
import probe_p19_common as C

CONTROLS = [
    # amplitude envelope (env_1 is the amp env; the P19 patch never touched it)
    "env_1_attack", "env_1_attack_power", "env_1_decay", "env_1_decay_power", "env_1_sustain",
    "env_1_release", "env_1_release_power",
    # pitch envelope
    "env_2_attack", "env_2_attack_power", "env_2_decay", "env_2_decay_power", "env_2_sustain",
    "env_2_release", "env_2_release_power",
    # oscillator 1 (the whole signal source)
    "osc_1_on", "osc_1_level", "osc_1_pan", "osc_1_phase", "osc_1_random_phase", "osc_1_wave_frame",
    "osc_1_transpose", "osc_1_tune", "osc_1_unison_voices", "osc_1_unison_detune",
    "osc_1_distortion_type", "osc_1_distortion_amount", "osc_1_distortion_phase", "osc_1_distortion_spread",
    # filter 1
    "filter_1_on", "filter_1_model", "filter_1_style", "filter_1_cutoff", "filter_1_resonance",
    "filter_1_keytrack", "filter_1_drive", "filter_1_mix", "filter_1_filter_input", "filter_1_blend",
    "filter_2_on",
    # the FX distortion (post-filter) and the FX filter
    "distortion_on", "distortion_type", "distortion_drive", "distortion_mix",
    "distortion_filter_cutoff", "distortion_filter_blend", "distortion_filter_order", "distortion_filter_resonance",
    "filter_fx_on", "filter_fx_model", "filter_fx_style", "filter_fx_cutoff", "filter_fx_resonance",
    "filter_fx_drive", "filter_fx_mix", "filter_fx_blend",
    # global voicing
    "legato", "polyphony", "voice_priority", "pitch_bend_range", "voice_transpose",
    "portamento_time", "portamento_slope", "portamento_force", "volume", "oversampling",
    # the remaining FX switches (never verified before: an on chorus/flanger/compressor
    # would create exactly the kind of sub-millisecond onset ripple P20 probe A measured)
    "chorus_on", "chorus_dry_wet", "chorus_feedback", "chorus_mod_depth",
    "delay_on", "delay_dry_wet", "delay_feedback",
    "eq_on", "flanger_on", "flanger_dry_wet", "flanger_feedback", "flanger_mod_depth",
    "phaser_on", "phaser_dry_wet", "phaser_feedback", "phaser_mod_depth",
    "reverb_on", "reverb_dry_wet", "compressor_on", "compressor_mix", "effect_chain_order",
    # the compressor is the only FX that is ON: its full multiband face
    "compressor_attack", "compressor_release", "compressor_enabled_bands", "compressor_band_gain",
    "compressor_low_gain", "compressor_high_gain", "compressor_band_lower_ratio", "compressor_band_lower_threshold",
    "compressor_band_upper_ratio", "compressor_band_upper_threshold", "compressor_low_lower_ratio",
    "compressor_low_lower_threshold", "compressor_low_upper_ratio", "compressor_low_upper_threshold",
    "compressor_high_lower_ratio", "compressor_high_lower_threshold", "compressor_high_upper_ratio",
    "compressor_high_upper_threshold",
]


def test_p20_baseline_read():
    try:
        import reapy

        project = reapy.Project()
        project.id  # noqa: B018 - reachability probe
    except Exception as exc:  # pragma: no cover - environment dependent
        import pytest

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

    instances = C.call(server, "plugin_instances", {})
    listing = C.call(server, "plugin_find", {"instance": instance, "kind": "control", "limit": 4000})
    matches = (listing.get("data") or {}).get("matches") or []
    host_index: dict[str, int] = {}
    labels: dict[str, dict[str, Any]] = {}
    for row in matches:
        control_id = str(row.get("id"))
        binding = row.get("binding") or {}
        if isinstance(binding.get("index"), int):
            host_index[control_id] = int(binding["index"])
        labels[control_id] = {
            "type": row.get("type"),
            "law": ((row.get("law") or {}).get("kind")),
            "state_key": binding.get("state_key"),
        }

    payload = C.call(server, "plugin_read", {"instance": instance, "controls": sorted(set(CONTROLS)), "include_display": True})
    values = (payload.get("data") or {}).get("values") or {}
    read_errors = (payload.get("data") or {}).get("errors") or payload.get("error")

    rows: dict[str, Any] = {}
    for control_id in CONTROLS:
        value = values.get(control_id)
        index = host_index.get(control_id)
        host_text = C.host_display(track, index) if isinstance(index, int) else None
        host_normalized = C.host_norm(track, index) if isinstance(index, int) else None
        rows[control_id] = {
            "in_profile": control_id in labels,
            "profile": labels.get(control_id),
            "host_index": index,
            "native": None if value is None else value.get("native"),
            "display": None if value is None else value.get("display"),
            "host_text": host_text,
            "host_normalized": None if host_normalized is None else round(float(host_normalized), 6),
            "agrees": (
                None
                if value is None or host_normalized is None or value.get("native") is None
                else abs(float(value["native"]) - float(host_normalized)) < 1e-4
            ),
        }

    routes = C.call(server, "plugin_modulation", {"instance": instance, "op": "list"})
    smoothing = C.call(server, "plugin_find", {"instance": instance, "kind": "control", "limit": 1})
    report = {
        "probe": "p20_baseline_read",
        "utc": H.utc_now(),
        "live_write": False,
        "track": {"index": track_index, "name": C.TRACK_NAME, "fx_index": 0, "item": str(item), "window_start": window_start},
        "instances": (instances.get("data") or {}).get("instances"),
        "n_controls_in_profile": len(matches),
        "read_errors": read_errors,
        "controls": rows,
        "routes": (routes.get("data") or {}).get("routes"),
        "sample_listing": (smoothing.get("data") or {}).get("matches"),
    }
    path = H.write_run_json("p20_baseline_read", report, H.EVIDENCE_DIR)
    print(f"evidence: {path}")
    print(f"track {track_index} '{C.TRACK_NAME}' fx 0; profile controls listed: {len(matches)}")
    for control_id in CONTROLS:
        row = rows[control_id]
        if not row["in_profile"]:
            print(f"  {control_id:<30} NOT IN PROFILE")
            continue
        print(
            f"  {control_id:<30} native={row['native']!s:<22} display={str(row['display'])[:28]!r:<30}"
            f" host={str(row['host_text'])[:26]!r:<30} agrees={row['agrees']}"
        )
    print(f"routes: {json.dumps(report['routes'], default=str)}")
