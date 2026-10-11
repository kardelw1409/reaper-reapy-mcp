"""P19 probe D — which state-write *shapes* stick? (create / update / clear / remove)

Probe C measured: a leaf write and a route **create** survive at least 4 s with
the chunk digest stable; a route **remove** reports `ok: true` (with a verified
read-back at ~0.4 s) and is gone again a few seconds later - the chunk returns to
its pre-remove digest. This probe narrows which shape is the problem and records
the full envelopes (probe C recorded only `ok`, which is exactly the evidence a
question like this needs):

1. does probe C's create still hold (minutes later)?
2. does an **update** of an existing slot (a fill, `power` included) stick?
3. does a tool **remove** of a canonical route stick?
4. does a pair **clear expressed as an update** (`source`/`destination` = "")
   stick?

Restore: every slot the probe touches is written back to the values observed at
entry, the entry route list is re-asserted, and whatever cannot be restored is
named in the artifact rather than glossed over.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import pytest

import live_harness as H
import probe_p19_common as C
from reaper_reapy_mcp.plugins.model import PluginInstanceRef, text_digest

pytestmark = pytest.mark.live_write

POLLS = (0.5, 1.5, 4.0)


def test_probe_d_which_write_shapes_stick():
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
        display="p19 shapes",
    )

    report: dict[str, Any] = {
        "probe": "p19_write_shapes",
        "utc": H.utc_now(),
        "live_write": True,
        "track_index": track_index,
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
                "bipolar": row.get("bipolar"),
                "bypass": row.get("bypass"),
                "stereo": row.get("stereo"),
            }
            for row in (document.get("routes") or [])
            if row.get("destination")
        ]
        return {
            "chunk_digest": text_digest(chunk),
            "routes": routes,
        }

    def poll(label: str, before: dict[str, Any], *, until: float = 4.0) -> dict[str, Any]:
        report["steps"].setdefault(label, {}).setdefault("before", before)
        timeline = report["steps"][label].setdefault("timeline", [])
        started = time.perf_counter()
        for seconds in POLLS:
            if seconds > until:
                continue
            time.sleep(max(0.0, seconds - (time.perf_counter() - started)))
            timeline.append({"at_s": seconds, **observe()})
        return timeline[-1]

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
            "plan_summary": data.get("plan_summary"),
            "dependency_findings": data.get("dependency_findings"),
            "request": request,
        }

    def modulation(op: str, **extra: Any) -> dict[str, Any]:
        payload = C.call(server, "plugin_modulation", {"instance": instance, "op": op, **extra})
        outcome = ((payload.get("data") or {}).get("outcome")) or {}
        return {
            "ok": payload.get("ok"),
            "error": (payload.get("error") or {}).get("code"),
            "error_detail": (payload.get("error") or {}).get("detail"),
            "verdict": outcome.get("verdict"),
            "final_state": outcome.get("final_state"),
            "host": outcome.get("host"),
            "data": {key: value for key, value in (payload.get("data") or {}).items() if key in ("committed", "would_write", "verdict", "dedicated", "landed_slots")},
        }

    entry = observe()
    report["entry"] = entry
    slots = {row["slot"]: row for row in entry["routes"]}
    target_slot = next((slot for slot in (2, 3, 4) if slot not in slots), None)
    report["target_slot"] = target_slot

    try:
        # 1. does probe C's create still hold? (recorded, no write)
        report["steps"]["probe_c_create_still_holds"] = observe()

        if target_slot is not None:
            # 2. an update (a fill) into a free slot: can an update *create* a pair?
            before = observe()
            report["steps"]["update_fill"] = {"write": write({
                "routes_update": [
                    {
                        "slot": target_slot,
                        "source": "env_2",
                        "destination": "osc_1_transpose",
                        "amount": 0.02,
                        "bipolar": 0.0,
                        "power": 0.0,
                        "bypass": 0.0,
                        "stereo": 0.0,
                    }
                ]
            })}
            poll("update_fill", before)

            # 3. a tool remove of that (canonical, power 0) route
            before = observe()
            report["steps"]["remove_after_update"] = {"write": modulation("remove", selector={"slot": target_slot})}
            poll("remove_after_update", before)

            # 4. a pair clear expressed as an update
            before = observe()
            report["steps"]["update_clear"] = {"write": write({"routes_update": [{"slot": target_slot, "source": "", "destination": ""}]})}
            poll("update_clear", before)
    finally:
        # restore: re-assert the entry routes (fills stick; that is the measured
        # property this probe uses), then verify
        restore_writes = []
        for slot, row in slots.items():
            restore_writes.append(
                write(
                    {
                        "routes_update": [
                            {
                                "slot": slot,
                                "source": row["source"],
                                "destination": row["destination"],
                                "amount": row["amount"],
                                "bipolar": row.get("bipolar") or 0.0,
                                "power": row.get("power") or 0.0,
                                "bypass": row.get("bypass") or 0.0,
                                "stereo": row.get("stereo") or 0.0,
                            }
                        ]
                    }
                )
            )
        report["restore"] = {"writes": restore_writes, "before_poll": observe()}
        poll("restore", report["restore"]["before_poll"], until=1.5)
        report["restore"]["after"] = observe()

        path = H.write_run_json("p19_write_shapes", report, H.EVIDENCE_DIR)
        print(f"evidence: {path}")
        print(f"entry routes: {json.dumps(entry['routes'], default=str)}")
        for label in ("probe_c_create_still_holds", "update_fill", "remove_after_update", "update_clear"):
            step = report["steps"].get(label)
            if not step:
                continue
            if "write" in step:
                print(f"{label}: write ok={step['write'].get('ok')} verdict={step['write'].get('verdict')} error={step['write'].get('error')}")
            timeline = step.get("timeline") or ([step] if "chunk_digest" in step else [])
            for row in timeline:
                print(f"   +{row.get('at_s', 0)}s digest={row['chunk_digest'][:12]} routes={[(r['slot'], r['destination'], r['amount']) for r in row['routes']]}")
        print(f"final routes: {json.dumps(report['restore']['after']['routes'], default=str)}")

    assert report["restore"]["after"]["routes"] == entry["routes"], (
        "the instance was not restored to its entry route table; the difference is recorded in the artifact: "
        f"{report['restore']['after']['routes']!r} vs {entry['routes']!r}"
    )
