"""P10: the [00 §6] acceptance scenario, end to end, through the tool surface.

This is the acceptance run: the **registered** 7 tools (a real `FastMCP`, called
through `mcp.call_tool`, exactly as a client would) against the live REAPER
instance and the real provider:

`plugin_instances` -> `plugin_describe` -> `plugin_find` -> `plugin_read` ->
dry-run `plugin_write` (**the default**) -> a **refused** route create (the rule
`route_into_off_filter` is declared but not evaluated, so creating a pair is
refused before any write - owner safety-gate correction, 2026-10-11) -> committed
`plugin_write` (`dry_run: false`; a control) -> `plugin_state(inspect)` ->
`plugin_modulation(list)` -> full-channel restore with every captured channel
compared.

Structure, learned from the P8 runs:

- the complete recovery set is captured **before** the first committed call;
- the write phase records, it does not assert: an assertion there would skip the
  restore (an earlier P8 run did exactly that, which is why the restore is
  unconditional and the assertions come last);
- the restore is the codec's own change set (the writable captured leaves,
  plugin-derived leaves excluded and named), followed by an independent
  per-channel comparison;
- one evidence artifact per run, plus a summary.
"""

from __future__ import annotations

import json

import pytest

import live_harness as H

from reaper_reapy_mcp.plugins import api as A
from reaper_reapy_mcp.plugins import tools as T
from reaper_reapy_mcp.plugins import values as V
from reaper_reapy_mcp.plugins.errors import NotConnected, PluginError
from reaper_reapy_mcp.plugins.host import reaper as R
from reaper_reapy_mcp.plugins.model import text_digest
from reaper_reapy_mcp.plugins.providers.vital import VitalProvider

pytestmark = pytest.mark.live_write

CONTROL = "filter_1_cutoff"
ROUTE_SOURCE = "lfo_1"
ROUTE_DESTINATION = "filter_1_cutoff"
ROUTE_DEPTH = 8


def call(mcp, name: str, arguments: dict | None = None) -> dict:
    import asyncio

    blocks = asyncio.run(mcp.call_tool(name, arguments or {}))
    text = next(block.text for block in blocks if getattr(block, "text", None))
    return json.loads(text)


def test_p10_acceptance_scenario_end_to_end():
    from mcp.server.fastmcp import FastMCP

    bridge = R.ReaperHostBridge(timeout=60.0)
    try:
        bridge.list_tracks()
    except NotConnected as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"REAPER is not reachable, so the acceptance scenario did not run: {exc.message}")
    except PluginError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"the host answered with an unreadable contract: {exc.code}: {exc.message}")

    provider = VitalProvider()
    try:
        target = H.resolve_target(bridge, provider)
    except PluginError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"the acceptance harness refused to test: {exc.code}: {exc.message}")

    baseline = H.capture_baseline(bridge, target)
    assert baseline.complete, f"the recovery set is incomplete: {baseline.not_captured}"

    config = A.PluginConfig(provider_ids=("vital",), bridge_timeout=60.0, env={})
    mcp = FastMCP("acceptance")
    T.setup_plugin_tools(mcp, config=config)
    A._BRIDGE = bridge  # the acceptance run uses the bridge the harness validated
    A._REGISTRY = None

    control = H.control_in(baseline.bound_profile, CONTROL)
    ident = control["binding"]["ident"]
    original = float(baseline.parameters[str(ident)])
    display_now = V.from_native(control, V.normalized_to_native(control, original))
    new_display = float(display_now) + 1.0
    low, high = (control.get("range") or {}).get("min"), (control.get("range") or {}).get("max")
    if low is not None and high is not None and new_display > float(high):
        new_display = float(display_now) - 1.0

    report: dict = {
        "probe": "p10_acceptance",
        "utc": H.utc_now(),
        "live_write": True,
        "target": {"fx_name": target.facts.fx_name, "location": dict(target.ref.location)},
        "baseline": {
            "chunk_digest": baseline.chunk_digest,
            "block_digest": baseline.block_digest,
            "parameters": len(baseline.parameters),
            "settings_leaves": len(H._settings_leaves(baseline.document)),
            "derived_leaves": list(baseline.derived_leaves),
            "binding": baseline.binding_report,
            "seconds": baseline.seconds,
        },
        "control": {"id": CONTROL, "ident": ident, "display_before": display_now, "display_after": new_display},
        "steps": [],
        "restore": {"attempted": False, "verified": False},
    }

    def step(name: str, envelope: dict, *, extra: dict | None = None) -> dict:
        record = {
            "step": name,
            "ok": bool(envelope.get("ok")),
            "error": (envelope.get("error") or {}).get("code"),
            "verdict": (envelope.get("outcome") or {}).get("verdict"),
            "final_state": (envelope.get("outcome") or {}).get("final_state"),
            "host": (envelope.get("outcome") or {}).get("host"),
            "meta_keys": sorted((envelope.get("meta") or {}).keys()),
        }
        if extra:
            record.update(extra)
        report["steps"].append(record)
        return record

    try:
        # ---- 1. instances -------------------------------------------------
        instances = call(mcp, "plugin_instances", {})
        token = (instances.get("meta") or {}).get("target_token")
        step("plugin_instances", instances, extra={"n_instances": len((instances.get("data") or {}).get("instances") or [])})
        instance = (instances.get("data") or {}).get("instances", [{}])[0]

        # ---- 2. describe --------------------------------------------------
        described = call(mcp, "plugin_describe", {"instance": instance})
        step(
            "plugin_describe",
            described,
            extra={
                "counts": (described.get("data") or {}).get("counts"),
                "profile_source": ((described.get("data") or {}).get("profile") or {}).get("source"),
                "derived_leaves": ((described.get("data") or {}).get("state") or {}).get("derived_leaves"),
            },
        )

        # ---- 3. find ------------------------------------------------------
        found = call(mcp, "plugin_find", {"instance": instance, "query": "cutoff", "limit": 5})
        step("plugin_find", found, extra={"n_matches": (found.get("data") or {}).get("n_matches")})

        # ---- 4. read ------------------------------------------------------
        read = call(mcp, "plugin_read", {"instance": instance, "controls": [CONTROL]})
        step("plugin_read", read, extra={"values": (read.get("data") or {}).get("values")})

        # ---- 5. dry run (the default) --------------------------------------
        preview = call(
            mcp,
            "plugin_write",
            {
                "instance": instance,
                "request": {"controls": {CONTROL: new_display}},
            },
        )
        step("plugin_write(dry_run default)", preview, extra={"would_write": (preview.get("data") or {}).get("would_write")})

        # ---- 5b. a route create is refused (the rule gate, before any write) --
        refused = call(
            mcp,
            "plugin_write",
            {
                "instance": instance,
                "request": {
                    "routes_create": [
                        {"source": ROUTE_SOURCE, "destination": ROUTE_DESTINATION, "depth": ROUTE_DEPTH}
                    ]
                },
                "dry_run": False,
            },
        )
        step("plugin_write(route create)", refused)

        # ---- 6. committed write -------------------------------------------
        committed = call(
            mcp,
            "plugin_write",
            {
                "instance": instance,
                "request": {"controls": {CONTROL: new_display}},
                "dry_run": False,
            },
        )
        step("plugin_write(dry_run=false)", committed)
        if committed.get("ok"):
            chunk_after = bridge.read_state_chunk(target.ref)
            facts = bridge.fx_facts(target.ref.location["track_index"], target.ref.location["fx_index"])
            slice_after = bridge.locate_state_slice(target.ref, facts, chunk_after)
            document_after = target.codec.decode(slice_after)
            ident_count = int(target.facts.param_count or 0)
            idents_now = bridge.param_idents(target.ref, list(range(ident_count)))
            values_now = bridge.read_params(target.ref, list(range(ident_count)))
            observed_now = {str(i): float(v) for i, v in zip(idents_now, values_now) if i}
            report["after_write"] = {
                "outside_span": H.outside_span_is_intact(baseline.chunk, chunk_after, baseline.slice, slice_after),
                "active_routes": [row for row in document_after["routes"] if row.get("destination")],
                "independent_parameter_read": float(
                    bridge.read_params(target.ref, [control["binding"]["index"]])[0]
                ),
                "expected_normalized": float(
                    V.to_normalized(control, V.to_native(control, new_display))
                ),
                # what the document write did to the parameter channel (measured, never
                # assumed away: a state write may move parameters, which is why the full
                # vector is part of the recovery set)
                "parameters_vs_baseline": H.compare_parameters(baseline.parameters, observed_now, tolerance=0.0),
            }

            # ---- 7. state inspect -----------------------------------------
            inspected = call(mcp, "plugin_state", {"instance": instance, "op": "inspect"})
            step(
                "plugin_state(inspect)",
                inspected,
                extra={"slice_hex": ((inspected.get("data") or {}).get("slice") or {}).get("class_hex"),
                       "counts": (inspected.get("data") or {}).get("counts")},
            )

            # ---- 8. modulation list ---------------------------------------
            listed = call(mcp, "plugin_modulation", {"instance": instance, "op": "list"})
            step("plugin_modulation(list)", listed, extra={"n_slots_total": (listed.get("data") or {}).get("n_slots_total")})
    except Exception as exc:  # noqa: BLE001 - recorded; the restore below still runs
        report["write_exception"] = f"{type(exc).__name__}: {exc}"
    finally:
        # ---- 9. full-channel restore through the same tool surface ---------
        report["restore"]["attempted"] = True
        restore_changes = target.codec.restore_changes(baseline.document, baseline.document_snapshot)
        report["restore"]["derived_excluded"] = list(restore_changes.get("derived_excluded") or [])
        instance_arg = {"location": dict(target.ref.location)}
        restored = call(
            mcp,
            "plugin_write",
            {"instance": instance_arg, "request": {"state": dict(restore_changes["paths"])}, "dry_run": False},
        )
        step("plugin_write(restore)", restored)
        report["restore"]["verified_flag"] = bool(restored.get("ok"))

        chunk_final = bridge.read_state_chunk(target.ref)
        facts_final = bridge.fx_facts(target.ref.location["track_index"], target.ref.location["fx_index"])
        slice_final = bridge.locate_state_slice(target.ref, facts_final, chunk_final)
        document_final = target.codec.decode(slice_final)
        leaves = H.document_leaves_diff(
            document_final, baseline.document_snapshot, declared_derived=baseline.derived_leaves
        )
        count = int(target.facts.param_count or 0)
        idents = bridge.param_idents(target.ref, list(range(count)))
        values = bridge.read_params(target.ref, list(range(count)))
        observed = {str(i): float(v) for i, v in zip(idents, values) if i}
        parameters = H.compare_parameters(baseline.parameters, observed, tolerance=0.0)
        report["restore"]["final"] = {
            "writable_leaves": {k: v for k, v in leaves.items() if k not in ("mismatched", "new_paths_sample", "derived_mismatched")},
            "derived_leaf_profiles": [
                {"path": row["path"], **H.describe_difference(row["captured"], row["observed"])}
                for row in leaves["derived_mismatched"]
            ],
            "parameters": {k: v for k, v in parameters.items() if k != "mismatched"},
            "active_routes": [row for row in document_final["routes"] if row.get("destination")],
            "chunk_digest": text_digest(chunk_final),
            "outside_span": H.outside_span_is_intact(baseline.chunk, chunk_final, baseline.slice, slice_final),
        }
        report["restore"]["verified"] = bool(
            restored.get("ok")
            and leaves["all_equal"]
            and parameters["all_equal"]
            and not report["restore"]["final"]["active_routes"]
        )
        # The same fact in one representation (Decision 2/5): the exclusion is
        # named as a gap, and completeness is reported apart from verification,
        # so "the remaining channels are back" is never read as "everything is".
        report["restore"]["completeness"] = H.restore_completeness(
            channels_verified=report["restore"]["verified"],
            declared_derived=report["restore"].get("derived_excluded") or [],
        )
        path = H.write_run_json("p10_acceptance", report, H.EVIDENCE_DIR)
        report["evidence"] = str(path)
        print(f"\n[p10] evidence: {path}")

    # ---- assertions last: the project is restored at this point -----------
    assert not report.get("write_exception"), report["write_exception"]
    steps = {row["step"]: row for row in report["steps"]}
    assert token and token.startswith("tok_")
    assert steps["plugin_instances"]["ok"] and steps["plugin_describe"]["ok"]
    assert steps["plugin_find"]["ok"] and steps["plugin_read"]["ok"]
    assert steps["plugin_write(dry_run default)"]["verdict"] == "NO_WRITES"
    assert steps["plugin_write(dry_run default)"]["host"]["write_calls_attempted"] == 0
    # the route create is refused before the first write (the rule is unevaluable)
    route_step = steps["plugin_write(route create)"]
    assert route_step["ok"] is False, route_step
    assert route_step["error"] == "DependencyViolation", route_step
    assert route_step["host"]["write_calls_attempted"] == 0, route_step
    assert route_step["final_state"] == "unchanged_verified", route_step
    assert steps["plugin_write(dry_run=false)"]["verdict"] == "ALL_VERIFIED"
    assert steps["plugin_state(inspect)"]["ok"] and steps["plugin_modulation(list)"]["ok"]
    assert report["after_write"]["outside_span"]["intact"]
    # no route was created: the refusal held, and the route table is unchanged
    assert report["after_write"]["active_routes"] == [], report["after_write"]["active_routes"]
    assert abs(
        report["after_write"]["independent_parameter_read"]
        - report["after_write"]["expected_normalized"]
    ) <= 1e-6
    assert report["restore"]["verified"], (
        "the acceptance restore was not confirmed per channel; no further live mutation may "
        f"be performed - see {report.get('evidence')}"
    )
    # the declared limits must have shown up in the run (06 §10/P10 item 3)
    described_limits = steps["plugin_describe"].get("counts") or {}
    assert described_limits.get("controls", 0) > 0
