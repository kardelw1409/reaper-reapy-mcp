"""P8.3/P8.4: the first live writes - one parameter, one state slice, each restored.

Every test here:

- carries the `live_write` marker, so the suite's autouse guard (which refuses a
  real-bridge write) is lifted **only** for this module;
- resolves **exactly one** instance and captures the **complete** recovery set
  (block text + full parameter vector) before anything is written; an incomplete
  capture is a refusal, never a smaller test;
- writes through the real transaction engine (binding -> preflight -> plan ->
  snapshot -> ordered units -> verify), never through a write verb directly;
- restores in the approved order (document, parameters, verify document, verify
  parameters), re-reads, and compares per channel;
- records one evidence artifact per run (per-run name + content hash), including
  what it could not compare.

A failure between the write and the restore would leave the project changed:
that is why the restore runs in `finally`, and why the restore itself is
verified rather than assumed. If the restore cannot be confirmed, the test fails
loudly and names the channel - it never reports "restored" without the read-back
that proves it (AUD-015).
"""

from __future__ import annotations

import time

import pytest

import live_harness as H

from reaper_reapy_mcp.plugins import values as V
from reaper_reapy_mcp.plugins.errors import NotConnected, PluginError
from reaper_reapy_mcp.plugins.host import reaper as R
from reaper_reapy_mcp.plugins.model import Verdict, text_digest
from reaper_reapy_mcp.plugins.providers.vital import VitalProvider
from reaper_reapy_mcp.plugins.transaction import Transaction

pytestmark = pytest.mark.live_write

#: The parameter the P8.3 case writes: declared by the registry with an affine
#: law and an exact display/normalized correspondence (measured range 8..136).
PARAMETER_CONTROL = "filter_1_cutoff"

#: The P8.4 case's document change: **one state slice, no route**. Route creation
#: is refused by the dependency gate (`route_into_off_filter` cannot be evaluated -
#: owner safety-gate correction, 2026-10-11), so the live state-write case uses a
#: settings leaf that is stored in the document and is **not** bound to a declared
#: control. The leaf is chosen at runtime from the live profile + document
#: (`H.document_only_leaves`); if none qualifies the case skips with a named reason
#: instead of hard-coding a path this file cannot verify.
#: Leaves the **plugin** re-serializes from its own internal state are declared by
#: the provider (`profile.state.derived_leaves`, measured live: the sample blob);
#: the tests read the declaration instead of hard-coding a name, compare those
#: leaves separately, and never ask the layer to write one - a write whose landing
#: cannot be verified must not be attempted (the layer refuses it too).
ROUTE_SOURCE = "lfo_1"
ROUTE_DESTINATION = "filter_1_cutoff"
ROUTE_DEPTH = 8


def _bridge() -> R.ReaperHostBridge:
    return R.ReaperHostBridge(timeout=60.0)


def _connect(bridge: R.ReaperHostBridge):
    """Skip - never write - when the host or the instance is not testable."""
    try:
        bridge.list_tracks()
    except NotConnected as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"REAPER is not reachable, so no live write was attempted: {exc.message}")
    except PluginError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"the live host answered with an unreadable contract: {exc.code}: {exc.message}")
    provider = VitalProvider()
    try:
        return H.resolve_target(bridge, provider)
    except PluginError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"the harness refused to test: {exc.code}: {exc.message}")


def _transaction(bridge, target: H.Target, request: dict, *, dry_run: bool = False) -> Transaction:
    return Transaction(
        provider=target.provider,
        codec=target.codec,
        bridge=bridge,
        profile=target.profile,
        ref=target.ref,
        request=request,
        token=target.token,
        registry=target.registry,
        dry_run=dry_run,
    )


def _read_document(bridge, target: H.Target) -> tuple[str, dict]:
    chunk, _slice, document = _read_chunk_and_document(bridge, target)
    return chunk, document


def _read_chunk_and_document(bridge, target: H.Target) -> tuple[str, object, dict]:
    """The chunk, the slice **located in that chunk**, and the decoded document."""
    chunk = bridge.read_state_chunk(target.ref)
    facts = bridge.fx_facts(target.ref.location["track_index"], target.ref.location["fx_index"])
    slice_ = bridge.locate_state_slice(target.ref, facts, chunk)
    return chunk, slice_, target.codec.decode(slice_)


def _leaf_value(document: dict, path: str) -> object:
    """The value at a dotted settings path inside a decoded document view."""
    from reaper_reapy_mcp.plugins.providers.vital import codec as VC

    current: object = {"settings": (document or {}).get("settings") or {}}
    for segment in str(path).split("."):
        if isinstance(current, dict):
            current = current.get(segment)
        elif isinstance(current, list):
            try:
                current = current[int(segment)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return current


def _different_scalar(value: object) -> object | None:
    """A safe alternative value for a live document leaf, or ``None``.

    The case must change the document in a way that is (a) type-preserving, so the
    patch is a value change and not a shape change, and (b) trivially reversible by
    the restore. ``None`` means "this leaf cannot be used" and the caller skips with
    a reason rather than inventing a value.
    """
    if isinstance(value, bool):
        return not value
    if isinstance(value, (int, float)):
        candidate = float(value) + 1.0 if float(value) != 0.0 else 1.0
        return type(value)(candidate) if isinstance(value, int) else candidate
    if isinstance(value, str):
        candidate = value + "_p8_4" if len(value) < 64 else value[:-4]
        return candidate if candidate != value else None
    return None


def _captured_leaves(document: dict) -> dict:
    """The decoded document's settings leaves, as the report counts them."""
    from reaper_reapy_mcp.plugins.providers.vital import codec as VC

    return VC.flat_paths({"settings": document["settings"]})


# ---------------------------------------------------------------------------
# P8.3 - one parameter, live
# ---------------------------------------------------------------------------


def test_p8_3_one_live_parameter_write_is_verified_and_restored():
    bridge = _bridge()
    target = _connect(bridge)
    baseline = H.capture_baseline(bridge, target)
    assert baseline.complete, f"the recovery set is incomplete: {baseline.not_captured}"

    control = H.control_in(baseline.bound_profile, PARAMETER_CONTROL)
    index = control["binding"]["index"]
    ident = control["binding"]["ident"]
    assert ident, "the harness refuses to write a control with no verified live ident"
    original = float(baseline.parameters[str(ident)])
    native_now = V.normalized_to_native(control, original)
    display_now = V.from_native(control, native_now)
    low, high = (control.get("range") or {}).get("min"), (control.get("range") or {}).get("max")
    step = 1.0
    new_display = float(display_now) + step
    if low is not None and high is not None and new_display > float(high):
        new_display = float(display_now) - step
    new_normalized = float(V.to_normalized(control, V.to_native(control, new_display)))
    assert abs(new_normalized - original) > 1e-6, "the case must actually change the parameter"

    report: dict = {
        "probe": "p8_3_parameter_write",
        "utc": H.utc_now(),
        "live_write": True,
        "target": {
            "fx_name": target.facts.fx_name,
            "location": dict(target.ref.location),
            "param_count": target.facts.param_count,
        },
        "control": {
            "id": control["id"],
            "index": index,
            "ident": ident,
            "law": (control.get("law") or {}).get("kind"),
            "range": control.get("range"),
            "display_before": display_now,
            "display_after": new_display,
            "normalized_before": original,
            "normalized_after": new_normalized,
        },
        "baseline": {
            "chunk_digest": baseline.chunk_digest,
            "block_digest": baseline.block_digest,
            "revision": baseline.revision.digest,
            "parameters_captured": len(baseline.parameters),
            "seconds": baseline.seconds,
            "binding": baseline.binding_report,
        },
        "restore": {"attempted": False, "verified": False},
    }
    evidence_path = None
    try:
        started = time.perf_counter()
        transaction = _transaction(bridge, target, {"controls": {PARAMETER_CONTROL: new_display}})
        result = transaction.run()
        report["transaction"] = {
            "seconds": round(time.perf_counter() - started, 3),
            "ok": result.ok,
            "committed": result.committed,
            "verdict": str(result.outcome.verdict.value),
            "final_state": str(result.outcome.final_state.value),
            "stage": str(result.outcome.stage.value),
            "host": result.outcome.host.as_dict(),
            "preflight": result.data.get("preflight"),
            "channels": {
                name: {
                    "rows": channel.rows[:12],
                    "n_rows": len(channel.rows),
                    "n_mismatched": len(channel.mismatched),
                }
                for name, channel in result.outcome.channels.items()
            },
        }
        if not result.ok:
            report["error"] = result.error.to_dict() if result.error else None
            raise AssertionError(f"the live parameter write failed: {report['error']}")

        assert result.outcome.verdict is Verdict.ALL_VERIFIED
        assert result.outcome.host.write_calls_attempted == 1
        assert result.outcome.host.write_effects_verified == 1
        assert result.outcome.host.write_effects_unknown == 0
        rows = result.outcome.channels["parameters"].rows
        assert rows and all(row["agrees"] for row in rows), rows
        assert rows[0]["space"] in ("normalized", "display")
        assert rows[0]["read_back"] is not None

        # independent read: the ident's value straight from the host
        independent = bridge.read_params(target.ref, [index])[0]
        report["independent_read_back"] = {
            "index": index,
            "expected": new_normalized,
            "observed": float(independent),
            "agrees": abs(float(independent) - new_normalized) <= 1e-6,
        }
        assert report["independent_read_back"]["agrees"], report["independent_read_back"]

        chunk_after_write, _document_after = _read_document(bridge, target)
        report["document_after_parameter_write"] = {
            "chunk_digest": text_digest(chunk_after_write),
            "changed_by_the_plugin": text_digest(chunk_after_write) != baseline.chunk_digest,
        }
    finally:
        # restore the captured value through the transport, then prove it
        report["restore"]["attempted"] = True
        try:
            restore = H.restore_parameters(bridge, target.ref, {str(ident): original})
            report["restore"].update(restore)
            chunk_restored, document_restored = _read_document(bridge, target)
            report["restore"]["chunk_digest_after_restore"] = text_digest(chunk_restored)
            report["restore"]["chunk_returned_to_baseline"] = (
                text_digest(chunk_restored) == baseline.chunk_digest
            )
            left = H.document_leaves_diff(document_restored, baseline.document_snapshot)
            report["restore"]["document_leaves"] = left
            full_after = bridge.param_idents(target.ref, list(range(int(target.facts.param_count))))
            values_after = bridge.read_params(
                target.ref, list(range(int(target.facts.param_count)))
            )
            observed = {str(i): float(v) for i, v in zip(full_after, values_after) if i}
            report["restore"]["full_vector"] = H.compare_parameters(
                baseline.parameters, observed, tolerance=0.0
            )
            report["restore"]["verified"] = bool(
                report["restore"]["restored"]
                and report["restore"]["full_vector"]["all_equal"]
                and report["restore"]["document_leaves"]["all_equal"]
            )
        finally:
            evidence_path = H.write_run_json("p8_3_parameter_write", report, H.EVIDENCE_DIR)
            print(f"\n[p8.3] evidence: {evidence_path}")

    assert report["restore"]["verified"], (
        "the live parameter restore was not confirmed by comparison; no further live "
        f"mutation may be performed - see {evidence_path}"
    )


# ---------------------------------------------------------------------------
# P8.4 - one state slice, live
# ---------------------------------------------------------------------------


def test_p8_4_one_live_state_write_is_verified_and_restored():
    bridge = _bridge()
    target = _connect(bridge)
    baseline = H.capture_baseline(bridge, target)
    assert baseline.complete, f"the recovery set is incomplete: {baseline.not_captured}"

    # The case: a document-only settings leaf, chosen from the live state (never a
    # route - the rule gate refuses those - and never a parameter-mirroring key).
    candidates = H.document_only_leaves(target.profile, baseline.document)
    chosen = sorted(candidates)[0] if candidates else None
    if chosen is None:
        pytest.skip("the live document exposes no unbound scalar settings leaf; case not run")
    original = candidates[chosen]
    assert not H.addresses_route_table(chosen), (
        "the chosen leaf is inside the route table: the dependency gate refuses such a write, "
        "so the case would fail for a reason unrelated to the write path under test"
    )
    new_value = _different_scalar(original)
    if new_value is None:
        pytest.skip(f"the chosen leaf {chosen!r} has no safe alternative value; case not run")
    case = {chosen: new_value}

    report: dict = {
        "probe": "p8_4_state_write",
        "utc": H.utc_now(),
        "live_write": True,
        "target": {
            "fx_name": target.facts.fx_name,
            "location": dict(target.ref.location),
        },
        "case": {
            "patch": case,
            "leaf_was": original,
            "chosen_from": len(candidates),
            "unbound_scalar_leaves": len(candidates),
            "routes_before": [
                row for row in baseline.document["routes"] if row.get("destination")
            ],
            "route_table_leaves_present": len(H.route_table_leaves(baseline.document)),
            "chosen_addresses_route_table": bool(H.addresses_route_table(chosen)),
            "why_not_a_route": (
                "route creation is refused before the first write: route_into_off_filter is "
                "declared but not evaluated (owner safety-gate correction, 2026-10-11), and the "
                "harness excludes route-table leaves from the candidate set for the same reason"
            ),
        },
        "baseline": {
            "chunk_digest": baseline.chunk_digest,
            "block_digest": baseline.block_digest,
            "revision": baseline.revision.digest,
            "slice_lines": [baseline.slice.line_start, baseline.slice.line_end],
            "settings_leaves": len(_captured_leaves(baseline.document)),
            "seconds": baseline.seconds,
        },
        "restore": {"attempted": False, "verified": False},
    }
    evidence_path = None
    try:
        # ---- write phase: record everything, assert nothing -------------------
        # (an assertion here would skip the restore below, and a mutation test
        # that can leave the project changed because a check failed is worse than
        # the failure it reports)
        try:
            transaction = _transaction(
                bridge,
                target,
                {"state": dict(case)},
            )
            started = time.perf_counter()
            result = transaction.run()
            report["transaction"] = {
                "seconds": round(time.perf_counter() - started, 3),
                "ok": result.ok,
                "committed": result.committed,
                "verdict": str(result.outcome.verdict.value),
                "final_state": str(result.outcome.final_state.value),
                "host": result.outcome.host.as_dict(),
                "preflight": result.data.get("preflight"),
                "state_rows": result.outcome.channels["state"].rows[:16],
                "n_state_rows": len(result.outcome.channels["state"].rows),
                "n_state_mismatched": len(result.outcome.channels["state"].mismatched),
                "error": result.error.to_dict() if result.error else None,
            }
            if result.ok:
                chunk_after, slice_after, document_after = _read_chunk_and_document(bridge, target)
                active_after = [
                    row for row in document_after["routes"] if row.get("destination")
                ]
                changed = H.document_leaves_diff(document_after, baseline.document_snapshot)
                report["after"] = {
                    "chunk_digest": text_digest(chunk_after),
                    "block_digest": slice_after.block_digest,
                    "active_routes": active_after,
                    "n_active_routes": len(active_after),
                    "outside_span": H.outside_span_is_intact(
                        baseline.chunk, chunk_after, baseline.slice, slice_after
                    ),
                    "document_leaves_changed": {
                        "compared": changed["compared"],
                        "n_mismatched": changed["n_mismatched"],
                        "paths": [row["path"] for row in changed["mismatched"]],
                        "new_paths": changed["new_paths_not_captured"],
                    },
                    "synth_version": document_after.get("synth_version"),
                }
                started = time.perf_counter()
                idents_after = bridge.param_idents(
                    target.ref, list(range(int(target.facts.param_count)))
                )
                values_after = bridge.read_params(
                    target.ref, list(range(int(target.facts.param_count)))
                )
                observed = {str(i): float(v) for i, v in zip(idents_after, values_after) if i}
                report["after"]["parameters_vs_baseline"] = H.compare_parameters(
                    baseline.parameters, observed, tolerance=0.0
                )
                report["after"]["parameter_reread_seconds"] = round(
                    time.perf_counter() - started, 3
                )
        except Exception as exc:  # noqa: BLE001 - recorded, never fatal before the restore
            report["write_exception"] = f"{type(exc).__name__}: {exc}"

        # ---- restore in the approved order: state, parameters, verify both ----
        report["restore"]["attempted"] = True
        restore_changes = target.codec.restore_changes(baseline.document, baseline.document_snapshot)
        leaves = dict(restore_changes.get("paths") or {})
        report["restore"]["derived_excluded"] = list(restore_changes.get("derived_excluded") or [])
        report["restore"]["leaves_written"] = len(leaves)
        restore_tx = _transaction(bridge, target, {"state": leaves})
        started = time.perf_counter()
        restore_result = restore_tx.run()
        report["restore"]["state_write"] = {
            "seconds": round(time.perf_counter() - started, 3),
            "ok": restore_result.ok,
            "verdict": str(restore_result.outcome.verdict.value),
            "host": restore_result.outcome.host.as_dict(),
            "n_rows": len(restore_result.outcome.channels["state"].rows),
            "n_mismatched": len(restore_result.outcome.channels["state"].mismatched),
            "error": restore_result.error.to_dict() if restore_result.error else None,
        }

        # verify the state channel: the captured document must be back, leaf by leaf
        chunk_back, slice_back, document_back = _read_chunk_and_document(bridge, target)
        report["restore"]["document_leaves_after_state_restore"] = H.document_leaves_diff(
            document_back, baseline.document_snapshot, declared_derived=baseline.derived_leaves
        )

        # verify/repair the parameter channel (the state reload may move values)
        count = int(target.facts.param_count or 0)
        idents_back = bridge.param_idents(target.ref, list(range(count)))
        values_back = bridge.read_params(target.ref, list(range(count)))
        observed_back = {str(i): float(v) for i, v in zip(idents_back, values_back) if i}
        first_compare = H.compare_parameters(baseline.parameters, observed_back, tolerance=0.0)
        report["restore"]["parameters_after_state_restore"] = first_compare
        if not first_compare["all_equal"]:
            to_fix = {
                str(row["ident"]): float(row["expected"]) for row in first_compare["mismatched"]
            }
            report["restore"]["parameter_repair"] = H.restore_parameters(
                bridge, target.ref, to_fix
            )
            idents_final = bridge.param_idents(target.ref, list(range(count)))
            values_final = bridge.read_params(target.ref, list(range(count)))
            observed_final = {str(i): float(v) for i, v in zip(idents_final, values_final) if i}
            report["restore"]["parameters_final"] = H.compare_parameters(
                baseline.parameters, observed_final, tolerance=0.0
            )
        else:
            report["restore"]["parameters_final"] = first_compare

        chunk_final, slice_final, document_final = _read_chunk_and_document(bridge, target)
        final_left = H.document_leaves_diff(
            document_final, baseline.document_snapshot, declared_derived=baseline.derived_leaves
        )
        residual = [row["path"] for row in final_left["mismatched"]]
        report["restore"]["final"] = {
            "chunk_digest": text_digest(chunk_final),
            "chunk_returned_to_baseline": text_digest(chunk_final) == baseline.chunk_digest,
            "document_leaves": final_left,
            "residual_leaf_paths": residual,
            "residual_leaf_profiles": [
                {
                    "path": path,
                    **H.describe_difference(
                        _leaf_value(baseline.document, path), _leaf_value(document_final, path)
                    ),
                }
                for path in residual
            ],
            "derived_leaf_profiles": [
                {
                    "path": row["path"],
                    **H.describe_difference(row["captured"], row["observed"]),
                }
                for row in final_left["derived_mismatched"]
            ],
            "active_routes": [row for row in document_final["routes"] if row.get("destination")],
            "outside_span": H.outside_span_is_intact(
                baseline.chunk, chunk_final, baseline.slice, slice_final
            ),
        }
        unexpected = [path for path in residual if path not in set(baseline.derived_leaves)]
        report["restore"]["unexpected_residual_leaves"] = unexpected
        report["restore"]["verified"] = bool(
            not unexpected
            and report["restore"]["parameters_final"]["all_equal"]
            and not report["restore"]["final"]["active_routes"]
        )
    finally:
        evidence_path = H.write_run_json("p8_4_state_write", report, H.EVIDENCE_DIR)
        print(f"\n[p8.4] evidence: {evidence_path}")

    # ---- assertions last: the project is already restored at this point -------
    assert not report.get("write_exception"), report["write_exception"]
    assert report["transaction"]["verdict"] == "ALL_VERIFIED", report["transaction"]
    assert report["transaction"]["host"]["write_effects_unknown"] == 0
    # no route was created: the case is a document leaf, and the route table must
    # be exactly what it was
    assert report["after"]["n_active_routes"] == len(report["case"]["routes_before"])
    assert report["after"]["outside_span"]["intact"], report["after"]["outside_span"]
    changed_paths = report["after"]["document_leaves_changed"]["paths"]
    assert list(report["case"]["patch"]) == changed_paths, (
        "exactly the patched leaf must differ after the write, and nothing else"
    )
    assert report["restore"]["state_write"]["verdict"] == "ALL_VERIFIED", report["restore"][
        "state_write"
    ]
    assert report["restore"]["document_leaves_after_state_restore"]["all_equal"], (
        "the restored document must equal every captured **writable** leaf"
    )
    assert report["restore"]["verified"], (
        "the live state restore was not confirmed per channel "
        f"(unexpected residual leaves: {report['restore']['unexpected_residual_leaves']}); "
        f"no further live mutation may be performed - see {evidence_path}"
    )
