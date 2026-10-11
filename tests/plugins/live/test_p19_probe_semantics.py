"""P19 probe E — the plugin's own load semantics for the route table.

Probe D isolated the difference between two writes that both "empty" a slot:

* an **update** that clears `source`/`destination` and leaves the amount -> **sticks**;
* the tool's **remove** (`source`/`destination` cleared **and** `modulation_N_amount = 0.0`)
  -> reports `ok: true` with a verified read-back at ~0.4 s and is **gone again** by
  the next read (the chunk returns to its pre-write digest).

The plugin's own empty slots look like the second shape, so "it refuses its own
shape" cannot be the whole story. This probe measures the load semantics directly:

1. create a route with **amount = 0.0** (non-empty pair) - does the slot appear?
2. create a route with **amount = 0.001** - does it?
3. write the **whole `settings.modulations` table** plus all 64 amounts as zeros
   (a canonical all-empty table) - do the existing routes vanish?

Restore: every slot the probe touches is returned to its entry state through the
same writes whose durability is being measured, and the artifact records what the
restore achieved.
"""

from __future__ import annotations

import copy
import json
import os
import time
from typing import Any

import pytest

import live_harness as H
import probe_p19_common as C
from reaper_reapy_mcp.plugins.model import PluginInstanceRef, text_digest

pytestmark = pytest.mark.live_write

POLLS = (0.5, 1.5, 3.5)
ROUTE_SLOTS = 64


def test_probe_e_load_semantics():
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
        display="p19 semantics",
    )

    report: dict[str, Any] = {"probe": "p19_load_semantics", "utc": H.utc_now(), "live_write": True, "steps": {}}

    def observe() -> dict[str, Any]:
        chunk = bridge.read_state_chunk(ref)
        slice_ = bridge.locate_state_slice(ref, facts, chunk)
        document = provider.codec().decode(slice_)
        return {
            "chunk_digest": text_digest(chunk),
            "routes": [
                {
                    "slot": row.get("slot"),
                    "source": row.get("source"),
                    "destination": row.get("destination"),
                    "amount": row.get("amount"),
                    "power": row.get("power"),
                }
                for row in (document.get("routes") or [])
                if row.get("destination")
            ],
            "document": document,
        }

    def write(request: dict[str, Any]) -> dict[str, Any]:
        payload = C.call(server, "plugin_write", {"instance": instance, "request": request, "dry_run": False})
        data = payload.get("data") or {}
        outcome = data.get("outcome") or {}
        return {
            "ok": payload.get("ok"),
            "error": (payload.get("error") or {}).get("code"),
            "error_detail": (payload.get("error") or {}).get("detail"),
            "verdict": outcome.get("verdict"),
            "final_state": outcome.get("final_state"),
            "host": outcome.get("host"),
            "request": request,
        }

    def poll(label: str, before: dict[str, Any]) -> dict[str, Any]:
        step = report["steps"].setdefault(label, {})
        step["before"] = {k: v for k, v in before.items() if k != "document"}
        timeline = step.setdefault("timeline", [])
        started = time.perf_counter()
        for seconds in POLLS:
            time.sleep(max(0.0, seconds - (time.perf_counter() - started)))
            row = observe()
            timeline.append({"at_s": seconds, "chunk_digest": row["chunk_digest"], "routes": row["routes"]})
        return {"chunk_digest": timeline[-1]["chunk_digest"], "routes": timeline[-1]["routes"]}

    entry = observe()
    report["entry"] = {"chunk_digest": entry["chunk_digest"], "routes": entry["routes"]}
    slots = {row["slot"]: row for row in entry["routes"]}
    free = [slot for slot in range(1, ROUTE_SLOTS + 1) if slot not in slots]

    try:
        # 1. a create with amount = 0.0
        slot_zero = free[0]
        report["steps"]["create_amount_zero"] = {
            "slot": slot_zero,
            "write": write({"routes_update": [{"slot": slot_zero, "source": "env_2", "destination": "osc_1_transpose", "amount": 0.0}]}),
        }
        poll("create_amount_zero", entry)

        # 2. a create with amount = 0.001
        slot_small = free[1]
        report["steps"]["create_amount_small"] = {
            "slot": slot_small,
            "write": write({"routes_update": [{"slot": slot_small, "source": "env_2", "destination": "osc_1_transpose", "amount": 0.001}]}),
        }
        poll("create_amount_small", observe())

        # 3. the whole table as a canonical empty table (plus the amounts zeroed)
        current = observe()
        table = [{"destination": "", "source": ""} for _ in range(ROUTE_SLOTS)]
        state: dict[str, Any] = {"settings.modulations": table}
        for number in range(1, ROUTE_SLOTS + 1):
            state[f"settings.modulation_{number}_amount"] = 0.0
        report["steps"]["whole_table_empty"] = {"write": write({"state": state})}
        poll("whole_table_empty", current)
    finally:
        # restore: re-assert every entry route through an update (the shape measured
        # to stick), and clear anything this probe added
        restore: dict[str, Any] = {"restored_slots": [], "cleared_slots": [], "errors": []}
        try:
            for slot, row in slots.items():
                restore["restored_slots"].append(
                    {
                        "slot": slot,
                        "write": write(
                            {
                                "routes_update": [
                                    {
                                        "slot": slot,
                                        "source": row.get("source") or "env_2",
                                        "destination": row.get("destination"),
                                        "amount": row["amount"],
                                        "power": row.get("power") or 0.0,
                                    }
                                ]
                            }
                        ),
                    }
                )
            for slot in (free[0], free[1]):
                live = observe()
                present = [r for r in live["routes"] if r["slot"] == slot]
                if present:
                    restore["cleared_slots"].append(
                        {"slot": slot, "write": write({"routes_update": [{"slot": slot, "source": "", "destination": ""}]})}
                    )
            if slots:  # make sure the whole-table write did not take the entry routes with it
                after = observe()
                missing = [slot for slot in slots if slot not in {r["slot"] for r in after["routes"]}]
                restore["missing_after"] = missing
                for slot in missing:
                    row = slots[slot]
                    restore.setdefault("reasserted", []).append(
                        {
                            "slot": slot,
                            "write": write(
                                {
                                    "routes_update": [
                                        {
                                            "slot": slot,
                                            "source": row.get("source") or "env_2",
                                            "destination": row.get("destination"),
                                            "amount": row["amount"],
                                            "power": row.get("power") or 0.0,
                                        }
                                    ]
                                }
                            ),
                        }
                    )
        except Exception as exc:  # noqa: BLE001 - the artifact must exist even then
            restore["errors"].append(f"{type(exc).__name__}: {exc}")
        report["restore"] = restore
        report["restore"]["after"] = {k: v for k, v in observe().items() if k != "document"}

        path = H.write_run_json("p19_load_semantics", report, H.EVIDENCE_DIR)
        print(f"evidence: {path}")
        print(f"entry: {json.dumps(report['entry']['routes'], default=str)}")
        for label in ("create_amount_zero", "create_amount_small", "whole_table_empty"):
            step = report["steps"][label]
            print(f"{label} (slot={step.get('slot')}): ok={step['write'].get('ok')} verdict={step['write'].get('verdict')} error={step['write'].get('error')}")
            for row in step["timeline"]:
                print(f"   +{row['at_s']}s digest={row['chunk_digest'][:12]} routes={[(r['slot'], r['destination'], r['amount']) for r in row['routes']]}")
        print(f"final: {json.dumps(report['restore']['after']['routes'], default=str)}")

    entry_keys = {(row["slot"], row["destination"]) for row in entry["routes"]}
    final_keys = {(row["slot"], row["destination"]) for row in report["restore"]["after"]["routes"]}
    assert final_keys == entry_keys, report["restore"]["after"]
