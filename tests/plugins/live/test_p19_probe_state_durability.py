"""P19 probe C — does a state write *stay* applied? (the state channel's durability)

**Why this exists.** Probe B's route create bent the pitch (audibly applied), but
its later `remove` - reported `ok: true` with a verified read-back - was gone by
the next read, and a subsequent create into slot 2 failed with `WriteNotApplied`
(the read-back showed the slot empty). Both are the same question:

> after `SetTrackStateChunk` and a verified read-back, does the plugin's own
> state agree - or does the plugin re-serialize its state and undo the write?

This probe measures that directly, read-mostly:

1. **autonomy**: two chunk reads 2 s apart with no write in between - a digest
   change is a re-serialization by the plugin itself;
2. **a document-only leaf write** (`settings.chorus_delay_1`, no parameter
   mirror): polled at +0.4 s / +1.5 s / +4 s;
3. **a route create** (`env_2 -> osc_1_transpose`): polled at the same intervals;
4. **a route removal**: the same, and it is also used to clear the route probe B
   left behind - the probe restores the instance to the route state it recorded
   at entry.

Every poll records the chunk digest, the leaf, and the route list, so the
artifact is a timeline rather than a verdict. No render is made; nothing here
needs audio to answer "did the write survive".
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

import live_harness as H
import probe_p19_common as C
from reaper_reapy_mcp.plugins.model import PluginInstanceRef, text_digest

pytestmark = pytest.mark.live_write

LEAF_KEY = "chorus_delay_1"
LEAF_VALUE = 3.0
POLLS = (0.4, 1.5, 4.0)


def test_probe_c_state_write_durability():
    try:
        import reapy

        project = reapy.Project()
        project.id  # noqa: B018 - reachability probe
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"REAPER is not reachable, so the probe did not run: {exc}")

    os.environ.setdefault("REAPER_MCP_PLUGIN_PROVIDERS", "vital")
    from reaper_reapy_mcp.plugins.host.reaper import ReaperHostBridge
    from reaper_reapy_mcp.plugins.providers.vital import VitalProvider
    from reaper_reapy_mcp.server import create_server

    server = create_server(debug=False)
    track_index = C.track_by_name(server)
    instance = {"track_index": track_index, "fx_index": 0}

    bridge = ReaperHostBridge()
    provider = VitalProvider()
    facts = bridge.fx_facts(track_index, 0)
    ref = PluginInstanceRef(
        host="reaper",
        location={"track_index": track_index, "fx_index": 0},
        provider="vital",
        identity=provider.identify(facts),
        display="p19 durability",
    )

    report: dict[str, Any] = {
        "probe": "p19_state_durability",
        "utc": H.utc_now(),
        "live_write": True,
        "track_index": track_index,
        "leaf_key": LEAF_KEY,
        "polls": list(POLLS),
        "steps": {},
    }

    def observe() -> dict[str, Any]:
        chunk = bridge.read_state_chunk(ref)
        slice_ = bridge.locate_state_slice(ref, facts, chunk)
        document = provider.codec().decode(slice_)
        routes = [
            {
                "slot": row.get("slot"),
                "source": row.get("source"),
                "destination": row.get("destination"),
                "amount": row.get("amount"),
                "power": row.get("power"),
            }
            for row in (document.get("routes") or [])
            if row.get("destination")
        ]
        payload = C.call(server, "plugin_modulation", {"instance": instance, "op": "list"})
        via_tool = [
            {"slot": row.get("slot"), "destination": row.get("destination"), "amount": row.get("amount")}
            for row in ((payload.get("data") or {}).get("routes") or [])
            if row.get("destination")
        ]
        return {
            "chunk_chars": len(chunk),
            "block_digest": slice_.block_digest,
            "chunk_digest": text_digest(chunk),
            "leaf": (document.get("settings") or {}).get(LEAF_KEY),
            "routes_codec": routes,
            "routes_via_tool": via_tool,
            "tracked_route_count": len(routes),
        }

    def settle_and_poll(label: str, before: dict[str, Any]) -> dict[str, Any]:
        timeline = []
        previous = time.perf_counter()
        for seconds in POLLS:
            time.sleep(max(0.0, seconds - (time.perf_counter() - previous)))
            timeline.append({"at_s": seconds, **observe()})
        report["steps"].setdefault(label, {})
        report["steps"][label].update({"before": before, "timeline": timeline})
        return timeline[-1]

    def write(request: dict[str, Any], *, label: str) -> dict[str, Any]:
        payload = C.call(server, "plugin_write", {"instance": instance, "request": request, "dry_run": False})
        data = payload.get("data") or {}
        return {
            "ok": payload.get("ok"),
            "verdict": (data.get("outcome") or {}).get("verdict"),
            "error": (payload.get("error") or {}).get("code"),
            "request": request,
        }

    try:
        entry = observe()
        report["entry"] = entry

        # 1. autonomy: does the chunk change with no write?
        first = observe()
        time.sleep(2.0)
        second = observe()
        report["steps"]["autonomy"] = {
            "digest_1": first["chunk_digest"],
            "digest_2": second["chunk_digest"],
            "changed_without_a_write": first["chunk_digest"] != second["chunk_digest"],
            "leaf_1": first["leaf"],
            "leaf_2": second["leaf"],
            "routes_1": first["routes_codec"],
            "routes_2": second["routes_codec"],
        }

        # 2. a document-only leaf write
        before = observe()
        report["steps"]["leaf_write"] = {"write": write({"state": {f"settings.{LEAF_KEY}": LEAF_VALUE}}, label="leaf")}
        settle_and_poll("leaf_write", before)

        # 3. a route create
        before = observe()
        report["steps"]["route_create"] = {
            "write": write(
                {"routes_create": [{"source": "env_2", "destination": "osc_1_transpose", "amount": 0.02}]},
                label="create",
            )
        }
        last = settle_and_poll("route_create", before)
        created_slot = last["routes_codec"][0]["slot"] if last["routes_codec"] else None
        report["steps"]["route_create"]["slot"] = created_slot

        # 4. a route removal - first the one just created, then probe B's leftover
        if created_slot is not None:
            before = observe()
            removed = C.call(
                server, "plugin_modulation", {"instance": instance, "op": "remove", "selector": {"slot": created_slot}}
            )
            report["steps"]["route_remove"] = {"ok": removed.get("ok"), "slot": created_slot}
            settle_and_poll("route_remove", before)

        leftovers = [row["slot"] for row in observe()["routes_codec"]]
        report["steps"]["leftover_cleanup"] = {"slots_before": leftovers, "removals": []}
        for slot in leftovers:
            before = observe()
            removed = C.call(server, "plugin_modulation", {"instance": instance, "op": "remove", "selector": {"slot": slot}})
            report["steps"]["leftover_cleanup"]["removals"].append({"slot": slot, "ok": removed.get("ok")})
            settle_and_poll(f"leftover_remove_slot_{slot}", before)

        # restore the leaf to the value observed at entry
        if entry["leaf"] != observe()["leaf"]:
            report["steps"]["leaf_restore"] = write({"state": {f"settings.{LEAF_KEY}": entry["leaf"]}}, label="leaf_restore")
            settle_and_poll("leaf_restore", observe())
    finally:
        final = observe()
        report["final"] = final
        report["restored_to_entry_routes"] = final["routes_codec"] == entry["routes_codec"]
        report["leaf_restored"] = final["leaf"] == entry["leaf"]
        path = H.write_run_json("p19_state_durability", report, H.EVIDENCE_DIR)
        print(f"evidence: {path}")
        print(f"autonomy: {json.dumps({k: v for k, v in report['steps']['autonomy'].items() if k not in ('routes_1', 'routes_2')}, default=str)}")
        for label in ("leaf_write", "route_create", "route_remove"):
            step = report["steps"].get(label)
            if not step:
                continue
            print(f"{label}: write={json.dumps({k: v for k, v in (step.get('write') or {'ok': step.get('ok')}).items() if k != 'request'}, default=str)}")
            for row in step.get("timeline") or []:
                print(f"   +{row['at_s']}s digest={row['chunk_digest'][:12]} leaf={row['leaf']} routes={[r['destination'] for r in row['routes_codec']]}")
        for label, step in report["steps"].items():
            if label.startswith("leftover_remove") or label.startswith("leaf_restore"):
                print(f"{label}: {json.dumps({k: v for k, v in step.items() if k in ('ok', 'verdict', 'slot', 'error')}, default=str)} "
                      f"routes_after={[r['destination'] for r in (step.get('timeline') or [{}])[-1].get('routes_codec', [])]}")
        print(f"final routes={final['routes_codec']} leaf={final['leaf']} (entry leaf={entry['leaf']})")

    assert report["leaf_restored"], report["final"]
