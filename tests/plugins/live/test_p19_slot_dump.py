"""P19 — read-only dump of the target's route slots and the controls under suspicion.

One question only: with the route table reported empty, which `settings` leaf is
still nonzero and could bend the pitch? Prints every slot whose *any* field is
nonzero, the full list of `modulation_*` leaves with nonzero values, and the
pitch-relevant controls. Writes nothing.
"""

from __future__ import annotations

import json

import probe_p19_common as C
import pytest

import live_harness as H

pytestmark = []

CONTROLS = [
    "osc_1_transpose", "voice_transpose", "osc_1_tune", "voice_tune",
    "osc_1_level", "osc_2_level", "osc_1_on", "osc_2_on",
    "filter_1_cutoff", "filter_1_resonance", "filter_1_on", "filter_2_cutoff", "filter_2_on",
    "osc_1_spectral_morph_type", "osc_1_spectral_morph_amount",
    "osc_1_distortion_type", "osc_1_distortion_amount", "osc_1_unison_voices", "osc_1_unison_detune",
    "env_2_attack", "env_2_decay", "env_2_sustain", "env_2_release",
    "pitch_bend_range", "portamento_time", "polyphony",
]


def test_p19_slot_dump():
    import os

    os.environ.setdefault("REAPER_MCP_PLUGIN_PROVIDERS", "vital")
    from reaper_reapy_mcp.plugins.host.reaper import ReaperHostBridge
    from reaper_reapy_mcp.plugins.model import PluginInstanceRef
    from reaper_reapy_mcp.plugins.providers.vital import VitalProvider
    from reaper_reapy_mcp.server import create_server

    server = create_server(debug=False)
    track_index = C.track_by_name(server)

    bridge = ReaperHostBridge()
    provider = VitalProvider()
    facts = bridge.fx_facts(track_index, 0)
    ref = PluginInstanceRef(
        host="reaper",
        location={"track_index": track_index, "fx_index": 0},
        provider="vital",
        identity=provider.identify(facts),
        display="p19 slot dump",
    )
    chunk = bridge.read_state_chunk(ref)
    document = provider.codec().decode(bridge.locate_state_slice(ref, facts, chunk))
    settings = document["settings"]

    mods = settings.get("modulations") or []
    rows = []
    for index, slot in enumerate(mods):
        number = index + 1
        fields = {
            "amount": settings.get(f"modulation_{number}_amount"),
            "bipolar": settings.get(f"modulation_{number}_bipolar"),
            "bypass": settings.get(f"modulation_{number}_bypass"),
            "power": settings.get(f"modulation_{number}_power"),
            "stereo": settings.get(f"modulation_{number}_stereo"),
        }
        pair = {"source": (slot or {}).get("source"), "destination": (slot or {}).get("destination")}
        if any(pair.values()) or any(float(value or 0.0) != 0.0 for value in fields.values()):
            rows.append({"slot": number, **pair, **fields})
    print("nonzero slots: " + json.dumps(rows, default=str))
    reads = C.call(server, "plugin_read", {"instance": {"track_index": track_index, "fx_index": 0}, "controls": CONTROLS, "include_display": True})["data"]
    print("controls: " + json.dumps({key: row.get("display") for key, row in (reads.get("values") or {}).items()}, default=str))
    print("problems: " + json.dumps(reads.get("problems"), default=str))
    print("slots_total=" + str(len(mods)) + " settings_keys=" + str(len(settings)))
    H.write_run_json("p19_slot_dump", {"slots": rows, "controls": {k: v.get("display") for k, v in (reads.get("values") or {}).items()}}, H.EVIDENCE_DIR)
