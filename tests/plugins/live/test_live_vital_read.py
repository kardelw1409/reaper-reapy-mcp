"""P7 L3: a **read-only** live read of the real Vital instance through the provider.

What this measures, and what it does not:

- it resolves the live instance through the real bridge and the Vital provider
  (`matches`/`identify` on fresh host facts), then decodes the instance's state
  block through the provider codec and reports the identity fields, the route
  table size and the document's `synth_version`;
- it performs **no write**: no write verb is reachable (the suite's conftest gate
  refuses one outright), and every call here is a read;
- it is skipped with an explicit reason when REAPER is not reachable, and the
  registry used for the profile is the pinned fixture subset, so the suite never
  needs `C:\\REAPER`.

It is evidence about the *read* path only. Nothing here certifies a live write
(that is P8), and nothing here is reported as such.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reaper_reapy_mcp.plugins.errors import NotConnected, Unreadable
from reaper_reapy_mcp.plugins.host import reaper as R
from reaper_reapy_mcp.plugins.providers.vital import VitalProvider, load_registry
from reaper_reapy_mcp.plugins.providers.vital import codec as VC

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
REGISTRY_DIR = FIXTURES / "vital_registry"


def _provider() -> VitalProvider:
    return VitalProvider(registry=load_registry(directory=str(REGISTRY_DIR)))


def test_live_vital_read_is_read_only_and_shape_checkable():
    bridge = R.ReaperHostBridge(timeout=10.0)
    try:
        tracks = bridge.list_tracks()
    except NotConnected as exc:
        pytest.skip(f"REAPER is not reachable, so the live Vital read did not run: {exc.message}")
    except Unreadable as exc:
        pytest.skip(f"the live host answered with an unreadable contract: {exc.message}")

    provider = _provider()
    found = None
    for track in tracks:
        for fx_index in range(int(track.n_fx)):
            facts = bridge.fx_facts(track.track_index, fx_index)
            if provider.matches(facts):
                found = (track, fx_index, facts)
                break
        if found:
            break
    if found is None:
        pytest.skip("no Vital VST3 instance is present in the open project; the read was not exercised")

    track, fx_index, facts = found
    identity = provider.identify(facts)
    assert identity["class_id"], identity
    assert identity["module"], identity
    assert isinstance(identity["param_count"], int) and identity["param_count"] > 0

    from reaper_reapy_mcp.plugins.model import PluginInstanceRef

    ref = PluginInstanceRef(
        host="reaper",
        location={"track_index": track.track_index, "fx_index": fx_index},
        provider="vital",
        identity=identity,
    )
    chunk = bridge.read_state_chunk(ref)
    slice_ = bridge.locate_state_slice(ref, facts, chunk)
    assert slice_.is_valid_for(chunk)
    codec = VC.VitalCodec()
    view = codec.decode(slice_)
    assert len(view["routes"]) == VC.ROUTE_SLOTS
    active = [row for row in view["routes"] if row["destination"]]
    assert len(active) <= VC.ROUTE_SLOTS

    # the block rebuilds byte-for-byte from its own parse (no write involved)
    framing = VC.parse_block_framing(slice_.block_text)
    rebuilt = VC.rebuild_block_text(framing, view["document"])
    assert rebuilt == slice_.block_text, "a no-op rebuild of the live block must be byte-identical"

    # read-only evidence artifact: written to the run directory (outside the repo, P32)
    from live_harness import EVIDENCE_DIR as evidence_dir  # noqa: PLC0415 - local by design

    evidence_dir.mkdir(parents=True, exist_ok=True)
    stamp = __import__("datetime").datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    payload = {
        "probe": "p7_vital_live_read",
        "read_only": True,
        "utc": stamp,
        "identity": identity,
        "param_count": facts.param_count,
        "block_lines": slice_.line_end - slice_.line_start + 1,
        "n_slots_total": len(view["routes"]),
        "n_active_routes": len(active),
        "synth_version": view["synth_version"],
        "preset_name": view["preset_name"],
        "json_offset_in_B": VC.decode_document(framing["B"])[1],
        "rebuild_byte_identical": True,
    }
    (evidence_dir / f"p7_vital_read__{stamp}.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
    )
    assert payload["rebuild_byte_identical"] is True
