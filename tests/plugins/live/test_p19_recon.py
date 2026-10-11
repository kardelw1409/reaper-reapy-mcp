"""P19 recon — READ ONLY. Identifies the 808 target and records its live facts.

Nothing here writes: no parameter write, no state write, no render. It answers,
against the live host:

- which track currently carries the name `V3-A Deep Glide 808` (found by NAME,
  not by the stale index), and whether it holds exactly one VST3i Vital;
- what the V3 layer resolves for that instance (identity, state, routes);
- what the controls the 808 recipe depends on currently hold, through **two
  independent channels**: the V3 profile law and the host's own formatted
  display (`TrackFX_GetFormattedParamValue`) — the channel that cannot agree
  with a wrong conversion by re-using it;
- the live document's own state leaves the parameter channel cannot show:
  the oscillator wavetable (name + frame count), the oscillator/envelope state
  values, and all 64 modulation slots.

Evidence: one `p19_recon__<utc>__<hash>.json` artifact under `evidence/`.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest

import live_harness as H

TRACK_NAME = "V3-A Deep Glide 808"

#: The controls the diagnosis reads through V3. Canonical `state_key` ids.
CONTROLS = [
    "osc_1_on", "osc_1_level", "osc_1_transpose", "osc_1_tune", "osc_1_wave_frame",
    "osc_1_spectral_morph_type", "osc_1_spectral_morph_amount",
    "osc_1_distortion_type", "osc_1_distortion_amount", "osc_1_destination",
    "osc_1_unison_voices", "osc_1_unison_detune", "osc_1_stereo_spread",
    "osc_1_phase", "osc_1_random_phase", "osc_1_stack_style",
    "osc_2_on", "osc_2_level", "osc_2_transpose", "osc_2_tune", "osc_2_wave_frame",
    "osc_2_spectral_morph_type", "osc_2_spectral_morph_amount", "osc_2_destination",
    "filter_1_on", "filter_1_model", "filter_1_style", "filter_1_cutoff",
    "filter_1_resonance", "filter_1_keytrack", "filter_1_mix",
    "filter_2_on", "filter_2_model", "filter_2_style", "filter_2_cutoff",
    "filter_2_resonance", "filter_2_mix",
    "env_1_attack", "env_1_decay", "env_1_sustain", "env_1_release",
    "env_1_attack_power", "env_1_decay_power", "env_1_release_power",
    "env_2_attack", "env_2_decay", "env_2_sustain", "env_2_release",
    "env_2_attack_power", "env_2_decay_power", "env_2_release_power",
    "portamento_time", "portamento_slope", "portamento_force",
    "polyphony", "legato", "voice_priority", "pitch_bend_range",
    "distortion_on", "distortion_type", "distortion_drive", "distortion_mix",
    "compressor_on", "compressor_mix", "volume",
]

#: (index, label) — the host's own parameter channel, read straight from REAPER.
HOST_INDICES = [
    (104, "Filter 1 Cutoff"), (114, "Filter 1 Switch"), (111, "Filter 1 Key Track"),
    (119, "Filter 2 Cutoff"), (129, "Filter 2 Switch"),
    (391, "Oscillator 1 Transpose"), (393, "Oscillator 1 Tune"), (397, "Oscillator 1 Wave Frame"),
    (495, "Oscillator 1 Frequency Morph Amount"), (497, "Oscillator 1 Frequency Morph Type"),
    (380, "Oscillator 1 Distortion Type"), (501, "Oscillator 1 Destination"),
    (48, "Envelope 1 Attack"), (50, "Envelope 1 Decay"), (54, "Envelope 1 Sustain"), (52, "Envelope 1 Release"),
    (55, "Envelope 2 Attack"), (57, "Envelope 2 Decay"), (61, "Envelope 2 Sustain"), (59, "Envelope 2 Release"),
    (436, "Portamento Time"), (432, "Polyphony"), (154, "Legato"), (460, "Voice Priority"),
    (462, "Volume"), (265, "Modulation 1 Amount"),
]

#: Document `settings` keys the parameter channel cannot show.
STATE_KEYS = [
    "synth_version", "osc_1_wave_frame", "osc_1_spectral_morph_type", "osc_1_spectral_morph_amount",
    "osc_1_transpose", "osc_1_tune", "osc_1_destination", "osc_1_distortion_type",
    "osc_2_wave_frame", "osc_2_spectral_morph_type", "osc_2_spectral_morph_amount",
    "filter_1_on", "filter_1_cutoff", "filter_1_keytrack", "filter_2_on", "filter_2_cutoff",
    "filter_fx_on", "distortion_on", "distortion_drive", "compressor_on",
    "env_1_attack", "env_1_decay", "env_1_sustain", "env_1_release",
    "env_2_attack", "env_2_decay", "env_2_sustain", "env_2_release",
    "portamento_time", "portamento_slope", "portamento_force", "polyphony", "legato",
    "voice_priority", "pitch_bend_range", "volume",
]


def call(mcp: Any, name: str, arguments: dict | None = None) -> dict:
    result = asyncio.run(mcp.call_tool(name, arguments or {}))
    blocks = result[0] if isinstance(result, tuple) else result
    text = next((block.text for block in blocks if getattr(block, "text", None)), None)
    if text is not None:
        return json.loads(text)
    raise RuntimeError(f"the tool {name!r} answered no readable payload")


def _wavetable_summary(settings: dict[str, Any]) -> list[dict[str, Any]]:
    tables = settings.get("wavetables")
    out: list[dict[str, Any]] = []
    if not isinstance(tables, list):
        return out
    for table in tables:
        if not isinstance(table, dict):
            continue
        frames: list[dict[str, Any]] = []
        for group in table.get("groups") or []:
            for component in (group or {}).get("components") or []:
                keyframes = (component or {}).get("keyframes") or []
                frames.append(
                    {
                        "component_type": (component or {}).get("type"),
                        "n_keyframes": len(keyframes),
                        "first_position": (keyframes[0] or {}).get("position") if keyframes else None,
                        "first_wave_chars": len(str((keyframes[0] or {}).get("wave_data") or "")) if keyframes else 0,
                    }
                )
        out.append({"name": table.get("name"), "author": table.get("author"), "version": table.get("version"), "components": frames})
    return out


def test_recon_of_the_808_target_track():
    try:
        import reapy

        project = reapy.Project()
        project.id  # noqa: B018 - reachability probe
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"REAPER is not reachable, so the recon did not run: {exc}")

    os.environ.setdefault("REAPER_MCP_PLUGIN_PROVIDERS", "vital")
    from reaper_reapy_mcp.server import create_server

    server = create_server(debug=False)
    report: dict[str, Any] = {
        "probe": "p19_recon",
        "utc": H.utc_now(),
        "live_write": False,
        "track_name_sought": TRACK_NAME,
    }

    # ---- 1. the track, found by NAME -----------------------------------------
    tracks = call(server, "get_track_list", {})
    matches = [row for row in tracks.get("tracks") or [] if row.get("name") == TRACK_NAME]
    assert len(matches) == 1, f"expected exactly one track named {TRACK_NAME!r}, found {matches}"
    track_index = int(matches[0]["index"])
    report["track"] = {"track_index": track_index, "name": matches[0]["name"], "n_tracks": tracks.get("count")}

    # its FX chain (names only; the recon must not touch the other tracks)
    fx: list[dict[str, Any]] = []
    from reapy import reascript_api as RPR

    track = RPR.GetTrack(0, track_index)
    n_fx = int(RPR.TrackFX_GetCount(track))
    for fx_index in range(n_fx):
        fx.append({"fx_index": fx_index, "name": str(RPR.TrackFX_GetFXName(track, fx_index, "", 512)[3])})
    report["fx_chain"] = fx
    assert [row for row in fx if row["fx_index"] == 0 and row["name"] == "VST3i: Vital (Vital Audio)"] or any(
        row["name"] == "VST3i: Vital (Vital Audio)" for row in fx
    ), f"track {track_index} carries no VST3i Vital: {fx}"
    vital_slots = [row["fx_index"] for row in fx if row["name"] == "VST3i: Vital (Vital Audio)"]
    assert vital_slots == [0], f"expected exactly one VST3i Vital at fx 0, found at {vital_slots}"

    instance = {"track_index": track_index, "fx_index": 0}

    # ---- 2. what V3 resolves -------------------------------------------------
    instances = call(server, "plugin_instances", {})
    mine = [
        inst for inst in (instances.get("data") or {}).get("instances", [])
        if int((inst.get("location") or {}).get("track_index", -1)) == track_index
    ]
    assert len(mine) == 1, f"V3 must resolve exactly one instance on track {track_index}: {mine}"
    report["identity"] = dict(mine[0])

    state = call(server, "plugin_state", {"instance": instance, "op": "inspect"})
    report["plugin_state"] = (state.get("data") or {})

    routes = call(server, "plugin_modulation", {"instance": instance, "op": "list"})
    report["plugin_modulation"] = (routes.get("data") or {})

    read = call(server, "plugin_read", {"instance": instance, "controls": CONTROLS, "include_display": True})
    values = (read.get("data") or {}).get("values") or {}
    report["v3_read"] = {
        "values": {
            control: {"native": row.get("native"), "display": row.get("display"), "normalized": row.get("normalized")}
            for control, row in values.items()
        },
        "problems": read.get("data", {}).get("problems"),
    }

    # ---- 3. the host's own channel: names + formatted display ----------------
    # The buffer sits at position 4 for these signatures (measured, see
    # host/reaper.py `field_string`); element 0 is the return code and reading it
    # is invisible offline. The guard below refuses a read that returned the
    # index itself, which is what a wrong position produces.
    host_rows: dict[str, Any] = {}
    for index, label in HOST_INDICES:
        name = str(RPR.TrackFX_GetParamName(track, 0, index, "", 512)[4])
        norm = float(RPR.TrackFX_GetParamNormalized(track, 0, index))
        display = str(RPR.TrackFX_GetFormattedParamValue(track, 0, index, "", 512)[4])
        assert name != str(index) and display != str(index), (
            f"index {index}: the string field read the index back ({name!r} / {display!r}); "
            "the tuple position for this signature is wrong"
        )
        host_rows[str(index)] = {"label": label, "host_name": name, "host_norm": round(norm, 6), "plugin_display": display}
    report["host_channel"] = host_rows

    # ---- 4. the live document, decoded through the provider's codec ----------
    from reaper_reapy_mcp.plugins.host.reaper import ReaperHostBridge
    from reaper_reapy_mcp.plugins.model import PluginInstanceRef
    from reaper_reapy_mcp.plugins.providers.vital import VitalProvider

    bridge = ReaperHostBridge()
    provider = VitalProvider()
    facts = bridge.fx_facts(track_index, 0)
    identity = provider.identify(facts)
    ref = PluginInstanceRef(
        host="reaper",
        location={"track_index": track_index, "fx_index": 0},
        provider="vital",
        identity=identity,
        display=f"recon @ track {track_index}",
    )
    chunk = bridge.read_state_chunk(ref)
    slice_ = bridge.locate_state_slice(ref, facts, chunk)
    document = provider.codec().decode(slice_)
    settings = document.get("settings") or {}
    mods = settings.get("modulations") or []
    slots = []
    for index, slot in enumerate(mods):
        number = index + 1
        row = {
            "slot": number,
            "source": (slot or {}).get("source"),
            "destination": (slot or {}).get("destination"),
            "amount": settings.get(f"modulation_{number}_amount"),
            "bipolar": settings.get(f"modulation_{number}_bipolar"),
            "power": settings.get(f"modulation_{number}_power"),
            "bypass": settings.get(f"modulation_{number}_bypass"),
            "stereo": settings.get(f"modulation_{number}_stereo"),
        }
        slots.append(row)
    active = [row for row in slots if row["source"] or row["destination"]]
    report["document"] = {
        "synth_version": document.get("synth_version"),
        "preset_name": document.get("preset_name"),
        "n_settings_keys": len(settings),
        "slice": {"block_digest": slice_.block_digest, "block_chars": len(slice_.block_text), "ordinal": slice_.ordinal},
        "state_values": {key: settings.get(key) for key in STATE_KEYS},
        "wavetables": _wavetable_summary(settings),
        "n_slots": len(mods),
        "active_slots": active,
        "first_empty_slot": next((row for row in slots if not row["source"] and not row["destination"]), None),
        "free_slot_defaults": {
            key: settings.get(key) for key in ("modulation_1_amount", "modulation_1_bipolar", "modulation_1_power", "modulation_1_bypass", "modulation_1_stereo")
        },
    }

    path = H.write_run_json("p19_recon", report, H.EVIDENCE_DIR)
    print(f"evidence: {path}")
    print(f"track {track_index}: {TRACK_NAME}; identity={report['identity']['identity']}")
    print(f"active modulation slots: {len(active)}")
    print(f"wavetables: {json.dumps(report['document']['wavetables'], default=str)}")
    print("host channel:")
    for index, row in report["host_channel"].items():
        print(f"  {index:>4} {row['label']:<34} norm={row['host_norm']:<9} display={row['plugin_display']}")
