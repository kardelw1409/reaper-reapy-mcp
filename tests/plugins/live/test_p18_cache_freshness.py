"""P18 cache freshness: an external change is never masked, and the plan is live.

The brief's mandatory freshness test, live, on the P18 target (track 1, fx 0,
`filter_1_cutoff` - an affine control with a stable law):

1. read the control through the registered V3 `plugin_read`;
2. change it through the **independent classic tool** `set_fx_param` (a
   host-normalized write that bypasses the V3 layer entirely);
3. read it again through V3 and require the new value - a value cache, a TTL or a
   stale baseline would fail here;
4. run a `plugin_write` dry run and require its plan item to carry
   ``from == the externally set value`` (the plan is built against the live
   read inside that same call; `plan.items[].from` is exposed for exactly this);
5. restore the exact host-normalized value through the classic tool and verify
   the V3 read is back to where it started.

No value of this control is cached anywhere by the layer (audit of 2026-10-11:
the only caches are the capability profile and metadata), and this probe is the
measurement that says so instead of the promise.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import pytest

import live_harness as H

pytestmark = pytest.mark.live_write

TRACK = 1
FX = 0
CONTROL = "filter_1_cutoff"
RANGE_LOW, RANGE_HIGH = 8.0, 136.0  # the declared affine range
EXTERNAL_NORM = 0.5
EXPECTED_EXTERNAL_NATIVE = RANGE_LOW + EXTERNAL_NORM * (RANGE_HIGH - RANGE_LOW)  # 72.0


def call(mcp: Any, name: str, arguments: dict | None = None) -> dict:
    result = asyncio.run(mcp.call_tool(name, arguments or {}))
    blocks = result[0] if isinstance(result, tuple) else result
    text = next((block.text for block in blocks if getattr(block, "text", None)), None)
    if text is not None:
        return json.loads(text)
    if isinstance(result, tuple) and len(result) > 1 and isinstance(result[1], dict):
        return result[1]
    raise RuntimeError(f"the tool {name!r} answered no readable payload")


def _num(value: Any) -> float:
    for item in (value if isinstance(value, (tuple, list)) else [value]):
        try:
            return float(item)
        except (TypeError, ValueError):
            continue
    raise AssertionError(f"no numeric element in {value!r}")


def test_an_external_change_is_seen_and_the_dry_run_plans_from_live_state():
    try:
        import reapy
        from reapy import reascript_api as RPR

        project = reapy.Project()
        track_id = project.tracks[TRACK].id
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"REAPER is not reachable, so the freshness test did not run: {exc}")

    os.environ.setdefault("REAPER_MCP_PLUGIN_PROVIDERS", "vital")
    from reaper_reapy_mcp.server import create_server

    server = create_server(debug=False)
    instance = {"track_index": TRACK, "fx_index": FX}

    def _seq(value: Any) -> list:
        return list(value) if isinstance(value, (tuple, list)) else [value]

    def param_name(index: int) -> str:
        return str(_seq(RPR.TrackFX_GetParamName(track_id, FX, index, "", 256))[4])

    # the plugin's raw parameter index for the control, via REAPER's own name table
    param_index = next(
        (index for index in range(int(RPR.TrackFX_GetNumParams(track_id, FX)))
         if param_name(index).strip().lower() == "filter 1 cutoff"),
        None,
    )
    assert param_index is not None, "the plugin must expose a 'Filter 1 Cutoff' parameter"

    report: dict[str, Any] = {
        "probe": "p18_cache_freshness",
        "utc": H.utc_now(),
        "brief": "V3 read -> classic set_fx_param change -> V3 read -> dry-run plan.from -> restore",
        "target": instance,
        "control": CONTROL,
        "steps": {},
        "evidence": None,
    }

    def v3_read() -> dict:
        payload = call(server, "plugin_read", {"instance": instance, "controls": [CONTROL]})
        assert payload.get("ok") is True, payload
        return dict(((payload.get("data") or {}).get("values") or {}).get(CONTROL) or {})

    def host_norm() -> float:
        return _num(RPR.TrackFX_GetParamNormalized(track_id, FX, param_index))

    # ---- 1. the first V3 read ---------------------------------------------------
    host_norm_before = host_norm()
    first = v3_read()
    report["steps"]["v3_read_before"] = first
    assert first.get("native") == pytest.approx(
        RANGE_LOW + host_norm_before * (RANGE_HIGH - RANGE_LOW), abs=1e-3
    ), (first, host_norm_before)

    # ---- 2. the independent, non-V3 change -------------------------------------
    external = call(server, "set_fx_param", {
        "track_index": TRACK, "fx_index": FX, "param_name": "Filter 1 Cutoff", "value": EXTERNAL_NORM,
    })
    assert external.get("status") == "success", external
    report["steps"]["external_change"] = {
        "tool": "set_fx_param (classic host tool)",
        "host_normalized_set": EXTERNAL_NORM,
        "host_normalized_after": host_norm(),
    }
    assert host_norm() == pytest.approx(EXTERNAL_NORM, abs=1e-6), "the classic write must have landed"

    # ---- 3. the second V3 read must show the external change --------------------
    second = v3_read()
    report["steps"]["v3_read_after_external"] = second
    assert second.get("native") == pytest.approx(EXPECTED_EXTERNAL_NATIVE, abs=1e-3), (
        f"the layer reported {second.get('native')!r}; a cached or TTL'd value would have kept "
        f"{first.get('native')!r} and this test would fail - which is the point"
    )
    assert second.get("native") != first.get("native"), "the change must be visible in the read"

    # ---- 4. a dry run plans FROM the live value, not from anything older --------
    dry = call(server, "plugin_write", {
        "instance": instance,
        "request": {"controls": {CONTROL: first.get("native")}},  # write the ORIGINAL value back
        "dry_run": True,
    })
    assert dry.get("ok") is True, dry
    plan = (dry.get("data") or {}).get("plan") or {}
    item = next((row for row in plan.get("items") or [] if (row.get("subject") or {}).get("control") == CONTROL), None)
    assert item is not None, plan
    report["steps"]["dry_run_plan_item"] = item
    assert item.get("from") == pytest.approx(EXPECTED_EXTERNAL_NATIVE, abs=1e-3), (
        f"the plan's `from` is {item.get('from')!r}; it must be the externally set value "
        f"{EXPECTED_EXTERNAL_NATIVE} - the plan is built against the live read"
    )

    # ---- 5. restore the exact host-normalized value and verify -------------------
    restore = call(server, "set_fx_param", {
        "track_index": TRACK, "fx_index": FX, "param_name": "Filter 1 Cutoff", "value": host_norm_before,
    })
    third = v3_read()
    report["steps"]["restore"] = {
        "tool": "set_fx_param",
        "host_normalized_set": host_norm_before,
        "host_normalized_after": host_norm(),
        "v3_read": third,
    }
    assert restore.get("status") == "success", restore
    assert host_norm() == pytest.approx(host_norm_before, abs=1e-6), "the restore must land exactly"
    assert third.get("native") == pytest.approx(first.get("native"), abs=1e-3), (
        f"the restore must be visible through V3: {third.get('native')!r} != {first.get('native')!r}"
    )

    path = H.write_run_json("p18_cache_freshness", report, H.EVIDENCE_DIR)
    report["evidence"] = str(path)
    print(f"\n[p18 freshness] evidence: {path}")
    print(
        f"[p18 freshness] {CONTROL}: {first.get('native')} -> external norm {EXTERNAL_NORM} -> "
        f"{second.get('native')} (seen by V3), plan.from {item.get('from')}, restored {third.get('native')}"
    )
