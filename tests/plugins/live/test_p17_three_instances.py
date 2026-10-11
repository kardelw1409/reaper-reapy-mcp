"""P17 setup: three NEW role tracks, one Vital instance each, identities proven.

The practical-acceptance brief asks for exactly three new tracks, each hosting
its own Vital instance, added through the **existing registered tools** - no
direct reapy poking around the plugin layer, no second MCP server, no touching
of any other track.

This probe is **idempotent by name**: it looks for the three role tracks, creates
only the missing ones, adds Vital only where a Vital is not already present, and
then proves from the live `plugin_instances` answer that:

- every role track hosts exactly one Vital;
- the three instances resolve to *distinct* locations (track+fx);
- the pre-existing instance (if any) is still present with identical build facts;
- each new instance answers a targeted `plugin_read` (so addressing works per
  instance, not just for the first one found).

It records every default it read (pitch bend range included - the P14 flat-pitch
lesson) into the run artifact, and the project is never saved, closed or switched.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import pytest

import live_harness as H

pytestmark = pytest.mark.live_write

#: The three role tracks: name -> the role the track name must speak for itself.
ROLES = (
    ("V3-A Deep Glide 808", "deep clean sub with readable pitch slides"),
    ("V3-B Distorted Rap 808", "saturated rap 808: harmonics, attack, slide"),
    ("V3-C Dubstep Glide Bass", "modern dubstep glide bass with mid character"),
)

#: Candidate add_fx names, tried in order until the live inventory sees a Vital.
#: The exact VST3i name goes first: measured 2026-10-11 that the fuzzy names
#: ("Vital", "Vital (Vital Audio)") resolve to the **VST2** build, which the V3
#: provider does not match - a fuzzy-first loop stacked three builds per track.
FX_NAME_CANDIDATES = ("VST3i: Vital (Vital Audio)", "Vital (Vital Audio)", "Vital")

#: Defaults worth recording per instance before any patch write (P14 lesson:
#: a factory pitch bend range + a stray wheel is exactly how a patch ends up
#: two semitones flat without any written parameter lying).
RECORDED_DEFAULTS = ("pitch_bend_range", "portamento_time", "portamento_force", "polyphony", "legato")


def call(mcp: Any, name: str, arguments: dict | None = None) -> dict:
    result = asyncio.run(mcp.call_tool(name, arguments or {}))
    blocks = result[0] if isinstance(result, tuple) else result
    text = next((block.text for block in blocks if getattr(block, "text", None)), None)
    if text is not None:
        return json.loads(text)
    if isinstance(result, tuple) and len(result) > 1 and isinstance(result[1], dict):
        return result[1]
    raise RuntimeError(f"the tool {name!r} answered no readable payload")


def _tracks(mcp: Any) -> list[dict[str, Any]]:
    payload = call(mcp, "get_track_list")
    assert payload.get("status") == "success", payload
    return list(payload.get("tracks") or [])


def _instances(mcp: Any) -> list[dict[str, Any]]:
    payload = call(mcp, "plugin_instances", {})
    assert payload.get("ok") is True, payload
    return list((payload.get("data") or {}).get("instances") or [])


def _location(instance: dict[str, Any]) -> tuple[int, int]:
    where = instance.get("location") or {}
    return int(where.get("track_index", -1)), int(where.get("fx_index", -1))


def test_three_role_tracks_each_host_their_own_vital():
    try:
        import reapy

        reapy.Project().id  # noqa: B018 - reachability probe
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"REAPER is not reachable, so the setup probe did not run: {exc}")

    os.environ.setdefault("REAPER_MCP_PLUGIN_PROVIDERS", "vital")
    from reaper_reapy_mcp.server import create_server

    server = create_server(debug=False)

    report: dict[str, Any] = {
        "probe": "p17_three_instances",
        "utc": H.utc_now(),
        "brief": "create/find the three role tracks, one Vital each, prove distinct identities",
        "initial": {},
        "created_tracks": [],
        "add_fx": [],
        "final": {},
        "defaults": {},
        "evidence": None,
    }

    # ---- 1. the initial state, before anything is created -----------------------
    tracks_before = _tracks(server)
    instances_before = _instances(server)
    report["initial"] = {
        "n_tracks": len(tracks_before),
        "track_names": [str(row.get("name")) for row in tracks_before],
        "instances": [
            {"location": _location(instance), "identity": instance.get("identity")}
            for instance in instances_before
        ],
    }

    # ---- 2. the three role tracks (create only what is missing) ------------------
    for role_name, role_purpose in ROLES:
        tracks = _tracks(server)
        found = next((row for row in tracks if str(row.get("name")) == role_name), None)
        if found is not None:
            report["created_tracks"].append(
                {"role": role_name, "purpose": role_purpose, "track_index": int(found["index"]), "created": False}
            )
            continue
        created = call(server, "create_track", {"name": role_name})
        assert created.get("status") == "success", created
        # verified from live state, not from the call's own word
        tracks = _tracks(server)
        found = next((row for row in tracks if str(row.get("name")) == role_name), None)
        assert found is not None, f"the track {role_name!r} was not found after creation: {tracks}"
        report["created_tracks"].append(
            {
                "role": role_name,
                "purpose": role_purpose,
                "track_index": int(found["index"]),
                "created": True,
                "call": created,
            }
        )

    role_track_index = {row["role"]: row["track_index"] for row in report["created_tracks"]}
    assert len(set(role_track_index.values())) == 3, role_track_index

    # ---- 3. one Vital per role track (add only where none is present) ------------
    for role_name, _purpose in ROLES:
        track_index = role_track_index[role_name]
        existing = [inst for inst in _instances(server) if _location(inst)[0] == track_index]
        if existing:
            report["add_fx"].append(
                {"role": role_name, "track_index": track_index, "added": False, "present": [_location(i) for i in existing]}
            )
            continue
        attempt_log: list[dict[str, Any]] = []
        added_at: tuple[int, int] | None = None
        for fx_name in FX_NAME_CANDIDATES:
            answer = call(server, "add_fx", {"track_index": track_index, "fx_name": fx_name})
            fx_list = (call(server, "get_fx_list", {"track_index": track_index}).get("fx_list")) or []
            present = [inst for inst in _instances(server) if _location(inst)[0] == track_index]
            attempt_log.append({"fx_name": fx_name, "answer": answer, "fx_count": len(fx_list)})
            if present:
                added_at = _location(present[0])
                break
            if fx_list:
                # an FX landed but it is not the build the provider matches (a fuzzy
                # name resolves to the VST2 build): remove it before trying the next
                # name, so the track never keeps a duplicate
                removed = call(server, "remove_fx", {"track_index": track_index, "fx_index": len(fx_list) - 1})
                attempt_log.append({"removed_not_matching": len(fx_list) - 1, "status": removed.get("status")})
        report["add_fx"].append(
            {"role": role_name, "track_index": track_index, "added": added_at is not None,
             "added_at": added_at, "attempts": attempt_log}
        )
        assert added_at is not None, (
            f"no Vital appeared on track {track_index} ({role_name}) after all candidate names; "
            f"attempts: {attempt_log}"
        )

    # ---- 4. the final inventory, and what it proves ------------------------------
    instances_after = _instances(server)
    by_track: dict[int, list[dict[str, Any]]] = {}
    for instance in instances_after:
        by_track.setdefault(_location(instance)[0], []).append(instance)

    role_instances = {role_name: by_track.get(role_track_index[role_name], []) for role_name, _ in ROLES}
    report["final"] = {
        "n_instances": len(instances_after),
        "instances": [
            {"location": _location(instance), "identity": instance.get("identity")} for instance in instances_after
        ],
        "per_role": {role: [{"location": _location(i), "identity": i.get("identity")} for i in found] for role, found in role_instances.items()},
    }

    # every role track hosts exactly one Vital, and the three locations are distinct
    for role_name, found in role_instances.items():
        assert len(found) == 1, f"{role_name} must host exactly one Vital, found {len(found)}: {report['final']}"
    locations = [tuple(_location(found[0])) for found in role_instances.values()]
    assert len(set(locations)) == 3, f"the three instances must live at distinct locations: {locations}"

    # the pre-existing instance survives with identical build facts
    if instances_before:
        before_locations = {tuple(_location(inst)) for inst in instances_before}
        after_locations = {tuple(_location(inst)) for inst in instances_after}
        assert before_locations <= after_locations, (before_locations, after_locations)
        for instance in instances_before:
            location = tuple(_location(instance))
            same = next(inst for inst in instances_after if tuple(_location(inst)) == location)
            assert same.get("identity") == instance.get("identity"), (
                f"the pre-existing instance at {location} changed identity: "
                f"{instance.get('identity')} -> {same.get('identity')}"
            )

    # ---- 5. each instance answers a targeted read (addressing works per instance)
    for role_name, found in role_instances.items():
        location = {"track_index": _location(found[0])[0], "fx_index": _location(found[0])[1]}
        read = call(server, "plugin_read", {"instance": location, "controls": list(RECORDED_DEFAULTS) + ["osc_1_level"]})
        assert read.get("ok") is True, f"a targeted read failed for {role_name}: {read}"
        values = (read.get("data") or {}).get("values") or {}
        report["defaults"][role_name] = {
            "location": location,
            "values": {key: {k: row.get(k) for k in ("native", "display", "normalized", "unit")} for key, row in values.items()},
        }

    path = H.write_run_json("p17_three_instances", report, H.EVIDENCE_DIR)
    report["evidence"] = str(path)
    print(f"\n[p17 setup] evidence: {path}")
    for role_name, _purpose in ROLES:
        entry = report["defaults"].get(role_name) or {}
        bend = ((entry.get("values") or {}).get("pitch_bend_range") or {})
        print(f"[p17 setup] {role_name}: track {role_track_index[role_name]}, pitch_bend_range={bend.get('display')!r}")
