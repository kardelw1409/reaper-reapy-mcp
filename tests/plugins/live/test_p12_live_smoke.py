"""P12 live smoke test: **one** parameter write and its verified restore.

Owner's brief (2026-10-11): after the safety-gate correction, confirm that the
current code can perform one safe Vital parameter write **through the registered
MCP tool**, verify the effect by a real read, and restore the original state. This
is not a repeat of P8-P10: it is exactly one committed write plus its restore.

What this file does **not** do: no FX is added, removed or replaced; no route,
state chunk, modulation or other parameter is touched; the caller supplies no
`target_token` (a token-carrying mutation is refused by design - Decision 6b); no
write verb is called directly, and the transaction is never bypassed.

Discipline taken from the P8/P10 runs:

- the complete recovery set is captured **before** anything is written, and an
  incomplete capture is a refusal, not a smaller test;
- the write phase **records**, it does not assert: an assertion there would skip
  the restore, and a mutation test that leaves the project changed because a check
  failed is worse than the failure it reports;
- the restore runs in `finally` through the same registered tool, and it is
  verified by an independent read-back (not by the write's own status);
- the two host write verbs are counted at the bridge boundary by a wrapper that
  **delegates** every call, so "zero writes in the dry run" and "exactly one write
  per committed call" are measured at the host, not taken from the envelope.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

import live_harness as H

from reaper_reapy_mcp.plugins import api as A
from reaper_reapy_mcp.plugins import binding as B
from reaper_reapy_mcp.plugins import tools as T
from reaper_reapy_mcp.plugins import values as V
from reaper_reapy_mcp.plugins.errors import NotConnected, PluginError
from reaper_reapy_mcp.plugins.host import reaper as R
from reaper_reapy_mcp.plugins.model import StateRevision, text_digest
from reaper_reapy_mcp.plugins.providers.vital import VitalProvider

pytestmark = pytest.mark.live_write

#: The control the smoke test writes. It is a **candidate**: the live run re-derives
#: its index, re-reads the host's parameter name at that index, re-reads the ident
#: and re-reads the value, and reports whether those agree with the previously
#: recorded `filter_1_cutoff` / `104:48629` / `0.4140625` (P8 evidence). It never
#: addresses the parameter by the recorded ident.
CONTROL = "filter_1_cutoff"

#: The delta applied to the control's display value; the restore writes back the
#: display value that corresponds to the captured normalized value.
DELTA = 1.0

#: The contract's tolerance for a restored normalized parameter
#: (`profile.verification.tolerances["parameter.normalized"]`).
TOLERANCE_NORMALIZED = 1e-3

PRIOR = {"ident": "104:48629", "normalized": 0.4140625, "source": "P8/P10 evidence, 2026-10-10"}


def call(mcp: Any, name: str, arguments: dict | None = None) -> dict:
    """Call a registered tool exactly as a client would, and read its envelope."""
    blocks = asyncio.run(mcp.call_tool(name, arguments or {}))
    text = next(block.text for block in blocks if getattr(block, "text", None))
    return json.loads(text)


class VerbCounter:
    """Counts the bridge's two write verbs; every call is delegated unchanged.

    It is a *measurement* at the host boundary, not a stub: the real verb still
    runs. The counts are the evidence that a dry run wrote nothing and that a
    committed call issued exactly one write.
    """

    VERBS = ("write_params", "write_state_slice")

    def __init__(self, bridge: Any) -> None:
        self.calls: list[dict[str, Any]] = []
        for verb in self.VERBS:
            original = getattr(bridge, verb)

            def wrapper(*args: Any, _verb: str = verb, _fn: Any = original, **kwargs: Any) -> Any:
                self.calls.append({"verb": _verb, "kwargs": sorted(kwargs)})
                return _fn(*args, **kwargs)

            setattr(bridge, verb, wrapper)

    def count(self, verb: str | None = None) -> int:
        return len([row for row in self.calls if verb is None or row["verb"] == verb])

    def mark(self) -> int:
        return len(self.calls)

    def since(self, mark: int, verb: str | None = None) -> list[dict[str, Any]]:
        return [
            row
            for row in self.calls[mark:]
            if verb is None or row["verb"] == verb
        ]


def _live_vectors(bridge: Any, target: Any) -> tuple[dict[str, float], Any, dict[str, Any]]:
    """The full parameter vector, the located slice and the decoded document now."""
    chunk = bridge.read_state_chunk(target.ref)
    facts = bridge.fx_facts(target.ref.location["track_index"], target.ref.location["fx_index"])
    slice_ = bridge.locate_state_slice(target.ref, facts, chunk)
    document = target.codec.decode(slice_)
    count = int(facts.param_count or 0)
    idents = bridge.param_idents(target.ref, list(range(count)))
    values = bridge.read_params(target.ref, list(range(count)))
    vector = {str(i): float(v) for i, v in zip(idents, values) if i}
    return vector, slice_, document


def test_one_live_parameter_write_is_verified_and_restored():
    bridge = R.ReaperHostBridge(timeout=60.0)
    try:
        bridge.list_tracks()
    except NotConnected as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"REAPER is not reachable, so the smoke test did not run: {exc.message}")
    except PluginError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"the host answered with an unreadable contract: {exc.code}: {exc.message}")

    provider = VitalProvider()
    # ---- section 3: addressing by the contract's own means (exactly one target) --
    try:
        target = H.resolve_target(bridge, provider)
    except PluginError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"the smoke test refused to pick a target: {exc.code}: {exc.message}")

    baseline = H.capture_baseline(bridge, target)
    assert baseline.complete, f"the recovery set is incomplete: {baseline.not_captured}"

    counter = VerbCounter(bridge)
    config = A.PluginConfig(provider_ids=("vital",), bridge_timeout=60.0, env={})
    # the registered surface, exactly as `create_server()` builds it
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("p12-smoke")
    T.setup_plugin_tools(server, config=config)
    A._BRIDGE = bridge  # the bridge this probe validated (the accepted pattern)
    A._REGISTRY = None

    # ---- the control, re-derived live (never addressed by the recorded ident) ----
    control = H.control_in(baseline.bound_profile, CONTROL)
    binding = dict(control.get("binding") or {})
    index = binding.get("index")
    assert isinstance(index, int) and not isinstance(index, bool), binding
    host_name = bridge.param_names(target.ref, [int(index)])[0]
    ident_now = bridge.param_idents(target.ref, [int(index)])[0]
    normalized_now = float(bridge.read_params(target.ref, [int(index)])[0])

    identity_now = provider.identify(target.facts)
    declared = B.declared_names(control)
    report: dict[str, Any] = {
        "probe": "p12_live_smoke",
        "utc": H.utc_now(),
        "live_write": True,
        "brief": "one parameter write + verified restore, through the registered tools",
        "target": {
            "fx_name": target.facts.fx_name,
            "location": dict(target.ref.location),
            "identity": identity_now,
            "class_id": target.facts.class_id,
            "module": target.facts.module,
            "version": target.facts.version,
            "param_count": target.facts.param_count,
            "session_handle": target.facts.session_handle,
            "ordinal": int(getattr(target.facts, "ordinal", 0) or 0),
        },
        "control": {
            "id": CONTROL,
            "declared_index": index,
            "declared_names": declared,
            "host_name_at_index": host_name,
            "host_name_matches_declared": bool(
                host_name and B.normalise_name(host_name) in declared
            ),
            "ident_readable": ident_now is not None,
            "ident_now": ident_now,
            "ident_source": binding.get("ident_source"),
            "ident_agrees_with_prior": ident_now == PRIOR["ident"],
            "normalized_now": normalized_now,
            "prior": dict(PRIOR),
            "prior_normalized_agrees": abs(normalized_now - float(PRIOR["normalized"])) <= 1e-12,
            "law": control.get("law"),
            "range": control.get("range"),
            "unit": control.get("unit"),
        },
        "baseline": {
            "chunk_digest": baseline.chunk_digest,
            "block_digest": baseline.block_digest,
            "revision": baseline.revision.digest,
            "parameters": len(baseline.parameters),
            "settings_leaves": len(H._settings_leaves(baseline.document)),
            "derived_leaves": list(baseline.derived_leaves),
            "binding": baseline.binding_report,
            "not_captured": list(baseline.not_captured),
            "seconds": baseline.seconds,
            "parameter_value": baseline.parameters.get(str(ident_now)),
        },
        "steps": [],
        "blocker": None,
        "refusals": [],
        "restore": {"attempted": False, "verified": False},
        "writes": {"dry_run": None, "commit": None, "restore": None, "total": None},
        "evidence": None,
    }

    def step(name: str, envelope: dict, *, extra: dict | None = None) -> dict:
        outcome = envelope.get("outcome") or {}
        record = {
            "step": name,
            "ok": bool(envelope.get("ok")),
            "error": (envelope.get("error") or {}).get("code"),
            "verdict": outcome.get("verdict"),
            "final_state": outcome.get("final_state"),
            "host": outcome.get("host"),
            "meta_keys": sorted((envelope.get("meta") or {}).keys()),
        }
        if extra:
            record.update(extra)
        report["steps"].append(record)
        return record

    instance_from_tool: dict[str, Any] = {}
    new_display: Any = None
    expected_normalized: Any = None
    restore_changes: dict[str, Any] = {}
    original_display: Any = None
    written = False
    document_drift = False

    try:
        # ---- 0. the correspondence gate: refuse *before* touching the plugin ---
        if not report["control"]["host_name_matches_declared"] or not report["control"]["ident_readable"]:
            report["blocker"] = "control_correspondence_not_verified"
            report["refusals"].append(
                {
                    "reason": "control_correspondence_not_verified",
                    "detail": (
                        "the host's parameter name at the profile's declared index is not this "
                        "control, or its ident is unreadable; the recorded ident was not used"
                    ),
                    "observed": {
                        "index": index,
                        "host_name": host_name,
                        "declared": declared,
                        "ident": ident_now,
                    },
                }
            )
            raise PluginError(
                "the live correspondence check for the control failed; no write was issued"
            )

        # ---- 1. plugin_instances: the contract's addressing object ------------
        instances = call(server, "plugin_instances", {})
        data = instances.get("data") or {}
        rows = data.get("instances") or []
        instance_from_tool = dict(rows[0]) if rows else {}
        step(
            "plugin_instances",
            instances,
            extra={
                "n_instances": len(rows),
                "instance_from_tool": instance_from_tool,
                "token_present": bool((instances.get("meta") or {}).get("target_token")),
                "identity_basis": (instances.get("meta") or {}).get("identity_basis"),
                "location_matches_target": (instance_from_tool.get("location") or {})
                == dict(target.ref.location),
            },
        )

        # ---- 2. plugin_describe: version, profile, limits ---------------------
        described = call(server, "plugin_describe", {"instance": instance_from_tool})
        described_data = described.get("data") or {}
        step(
            "plugin_describe",
            described,
            extra={
                "counts": described_data.get("counts"),
                "profile_source": (described_data.get("profile") or {}).get("source"),
                "provider_revision": (described_data.get("profile") or {}).get("provider_revision"),
                "compatible_versions": (described_data.get("profile") or {}).get("compatible_versions"),
                "identity": described_data.get("identity"),
                "state": described_data.get("state"),
                "limits_head": (described_data.get("limits") or [])[:3],
            },
        )

        # ---- 3. plugin_read: the value as the contract reports it -------------
        before = call(server, "plugin_read", {"instance": instance_from_tool, "controls": [CONTROL]})
        value_before = ((before.get("data") or {}).get("values") or {}).get(CONTROL) or {}
        step("plugin_read(before)", before, extra={"value": value_before})

        original_display = value_before.get("display")
        new_display = float(original_display) + DELTA
        low, high = (control.get("range") or {}).get("min"), (control.get("range") or {}).get("max")
        if low is not None and high is not None and not (float(low) <= new_display <= float(high)):
            new_display = float(original_display) - DELTA
        expected_normalized = float(V.to_normalized(control, V.to_native(control, new_display)))
        report["control"].update(
            {
                "display_before": original_display,
                "display_target": new_display,
                "expected_normalized_after": expected_normalized,
                "target_differs_from_original": abs(new_display - float(original_display)) > 0.0,
                # `range_respected` and `delta_in_range` are the same fact under the
                # name the assertions use (the first run asserted a key the report did
                # not carry, which is a probe defect - recorded, not hidden)
                "range_respected": bool(
                    low is None or high is None or (float(low) <= new_display <= float(high))
                ),
                "delta_in_range": bool(
                    low is None or high is None or (float(low) <= new_display <= float(high))
                ),
            }
        )
        assert abs(new_display - float(original_display)) > 0, "the target must differ from the original"

        # ---- 4. dry run (explicit, no write may happen) -----------------------
        mark = counter.mark()
        preview = call(
            server,
            "plugin_write",
            {"instance": instance_from_tool, "request": {"controls": {CONTROL: new_display}}, "dry_run": True},
        )
        dry_calls = counter.since(mark)
        report["writes"]["dry_run"] = dry_calls
        step(
            "plugin_write(dry_run=true)",
            preview,
            extra={"would_write": (preview.get("data") or {}).get("would_write"), "verb_calls": dry_calls},
        )

        # ---- 5. exactly one committed write, by location (no token) -----------
        # the dry run must have authorised it: a preview that refuses, or that cannot
        # decide, ends the test here (the brief's rule, and the fail-closed default)
        if not (preview.get("ok") and (preview.get("data") or {}).get("would_write") is True):
            report["blocker"] = "dry_run_did_not_authorise_the_write"
            report["refusals"].append(
                {
                    "reason": "dry_run_did_not_authorise_the_write",
                    "detail": "the preview did not report would_write=true; no committed write was issued",
                    "envelope": preview,
                }
            )
            raise PluginError("the dry run did not authorise the write; none was issued")
        mark = counter.mark()
        committed = call(
            server,
            "plugin_write",
            {
                "instance": {"location": dict(target.ref.location)},
                "request": {"controls": {CONTROL: new_display}},
                "dry_run": False,
            },
        )
        commit_calls = counter.since(mark)
        report["writes"]["commit"] = commit_calls
        step(
            "plugin_write(dry_run=false)",
            committed,
            extra={
                "verb_calls": commit_calls,
                "plan": (committed.get("data") or {}).get("plan"),
                "outcome_data": (committed.get("data") or {}).get("outcome"),
            },
        )
        written = bool(committed.get("ok"))

        # ---- 6. independent read-back (not the write's own status) -----------
        normalized_after = float(bridge.read_params(target.ref, [int(index)])[0])
        read_after = call(server, "plugin_read", {"instance": instance_from_tool, "controls": [CONTROL]})
        tool_value_after = ((read_after.get("data") or {}).get("values") or {}).get(CONTROL) or {}
        vector_after, slice_after, document_after = _live_vectors(bridge, target)
        post_write_revision = StateRevision.compute(
            state_channel=slice_after.block_text, parameters=vector_after
        )
        leaves_after = H.document_leaves_diff(
            document_after, baseline.document_snapshot, declared_derived=baseline.derived_leaves
        )
        #: a parameter write must not touch the document; if it does, the prescribed
        #: recovery is the codec's own change set (step 8), never an improvisation
        document_drift = not bool(leaves_after["all_equal"])
        report["after_write"] = {
            "normalized": normalized_after,
            "expected_normalized": expected_normalized,
            "normalized_delta": normalized_after - float(expected_normalized),
            "tool_reported": tool_value_after,
            "chunk_digest": text_digest(slice_after.block_text),
            "revision": post_write_revision.digest,
            "outside_span": H.outside_span_is_intact(
                baseline.chunk, bridge.read_state_chunk(target.ref), baseline.slice, slice_after
            ),
            "document_leaves": {
                key: value
                for key, value in leaves_after.items()
                if key not in ("mismatched", "new_paths_sample", "derived_mismatched")
            },
            # the rows themselves, not only the count: the first run recorded
            # `n_mismatched: 1` and dropped the path, which made the measured drift
            # impossible to name afterwards (probe reporting defect, 2026-10-11)
            "document_leaves_mismatched": leaves_after["mismatched"],
            "document_leaves_derived": leaves_after["derived_mismatched"],
            "document_drifted": document_drift,
            "active_routes": [row for row in document_after["routes"] if row.get("destination")],
            "parameters_vs_baseline": {
                key: value
                for key, value in H.compare_parameters(
                    baseline.parameters, vector_after, tolerance=0.0
                ).items()
                if key != "mismatched"
            },
            "only_the_control_moved": sorted(
                row["ident"]
                for row in H.compare_parameters(baseline.parameters, vector_after, tolerance=0.0)[
                    "mismatched"
                ]
            ),
        }

        # ---- 7. pre-restore revalidation: identity, revision, no surprise -----
        facts_now = bridge.fx_facts(target.ref.location["track_index"], target.ref.location["fx_index"])
        vector_pre, slice_pre, document_pre = _live_vectors(bridge, target)
        pre_restore_revision = StateRevision.compute(
            state_channel=slice_pre.block_text, parameters=vector_pre
        )
        pre_restore_compare = H.compare_parameters(vector_after, vector_pre, tolerance=0.0)
        # the question at restore time is "did anything change **since my write**",
        # not "does the document match the baseline": a difference that our own write
        # produced is already known (and handled in step 8), while a difference that
        # appears *between* the two reads is an external change and a refusal.
        snapshot_after = target.codec.snapshot_document(document_after)
        pre_restore_leaves = H.document_leaves_diff(
            document_pre, snapshot_after, declared_derived=baseline.derived_leaves
        )
        # a comparison of an empty set equals itself: `compared == 0` or a missing
        # snapshot must NOT be read as "the document is unchanged"
        document_comparable = bool(snapshot_after is not None and pre_restore_leaves["compared"] > 0)
        report["pre_restore"] = {
            "identity_now": provider.identify(facts_now),
            "identity_matches_baseline": provider.identify(facts_now) == identity_now,
            "session_handle": facts_now.session_handle,
            "session_handle_matches_baseline": facts_now.session_handle == target.facts.session_handle,
            "location": dict(target.ref.location),
            "revision_now": pre_restore_revision.digest,
            "revision_matches_post_write": pre_restore_revision.digest == post_write_revision.digest,
            "parameters_unchanged_since_write": {
                key: value for key, value in pre_restore_compare.items() if key != "mismatched"
            },
            "document_unchanged_since_write": {
                key: value
                for key, value in pre_restore_leaves.items()
                if key not in ("mismatched", "new_paths_sample", "derived_mismatched")
            },
            "document_comparison_is_not_vacuous": document_comparable,
            "target_is_unambiguous": True,
            "safe_to_restore": bool(
                pre_restore_revision.digest
                == post_write_revision.digest
                and pre_restore_compare["all_equal"]
                and pre_restore_leaves["all_equal"]
                and document_comparable
                and provider.identify(facts_now) == identity_now
            ),
        }
    except Exception as exc:  # noqa: BLE001 - recorded; the restore below still runs
        report["write_exception"] = f"{type(exc).__name__}: {exc}"
    finally:
        # ---- 8. restore through the same registered tool ----------------------
        if written and original_display is not None and report.get("pre_restore", {}).get("safe_to_restore"):
            report["restore"]["attempted"] = True
            restore_changes = target.codec.restore_changes(baseline.document, baseline.document_snapshot)
            report["restore"]["document_changes_needed"] = len(restore_changes["paths"])
            report["restore"]["derived_excluded"] = list(restore_changes.get("derived_excluded") or [])
            mark = counter.mark()
            restored = call(
                server,
                "plugin_write",
                {
                    "instance": {"location": dict(target.ref.location)},
                    "request": {"controls": {CONTROL: original_display}},
                    "dry_run": False,
                },
            )
            restore_calls = counter.since(mark)
            report["writes"]["restore"] = restore_calls
            step(
                "plugin_write(restore)",
                restored,
                extra={"verb_calls": restore_calls, "wrote_display": original_display},
            )
            report["restore"]["verified_flag"] = bool(restored.get("ok"))

            # the transactional recovery policy for the other captured channel: only
            # if the write actually moved a captured document leaf, and only through
            # the codec's own change set (derived leaves excluded and named)
            if document_drift:
                mark = counter.mark()
                document_restored = call(
                    server,
                    "plugin_write",
                    {
                        "instance": {"location": dict(target.ref.location)},
                        "request": {"state": dict(restore_changes["paths"])},
                        "dry_run": False,
                    },
                )
                report["writes"]["document_restore"] = counter.since(mark)
                step("plugin_write(restore document)", document_restored)
                report["restore"]["document_write_ok"] = bool(document_restored.get("ok"))
            else:
                report["restore"]["document_write_ok"] = None
                report["restore"]["document_restore_not_needed"] = (
                    "the parameter write left every captured document leaf unchanged, "
                    "so the prescribed recovery set had nothing to re-apply"
                )
        elif written:
            report["restore"]["refused_because"] = (
                "the pre-restore revalidation did not confirm an unchanged instance/revision; "
                "no write was issued over an unverified state"
            )
        else:
            report["restore"]["not_needed"] = "no committed write landed, so nothing was restored"

        # ---- 9. independent verification of the restore -----------------------
        if report["restore"]["attempted"]:
            normalized_final = float(bridge.read_params(target.ref, [int(index)])[0])
            read_final = call(
                server, "plugin_read", {"instance": instance_from_tool, "controls": [CONTROL]}
            )
            tool_value_final = ((read_final.get("data") or {}).get("values") or {}).get(CONTROL) or {}
            vector_final, slice_final, document_final = _live_vectors(bridge, target)
            chunk_final = bridge.read_state_chunk(target.ref)
            final_leaves = H.document_leaves_diff(
                document_final, baseline.document_snapshot, declared_derived=baseline.derived_leaves
            )
            final_parameters = H.compare_parameters(baseline.parameters, vector_final, tolerance=0.0)
            within_tolerance = H.compare_parameters(
                baseline.parameters, vector_final, tolerance=TOLERANCE_NORMALIZED
            )
            report["restore"]["final"] = {
                "normalized": normalized_final,
                "baseline_normalized": baseline.parameters.get(str(ident_now)),
                "exact_restore_of_control": normalized_final
                == baseline.parameters.get(str(ident_now)),
                "delta": normalized_final - float(baseline.parameters.get(str(ident_now), 0.0)),
                "tool_reported": tool_value_final,
                "tool_reported_display": tool_value_final.get("display"),
                "display_baseline": original_display,
                # the whole state chunk and the located block are **two different
                # digests**: the first run labelled the block digest as the chunk
                # digest and then compared it against the chunk's, which read as a
                # failed restore (probe defect, 2026-10-11)
                "chunk_digest": text_digest(chunk_final),
                "chunk_matches_baseline": text_digest(chunk_final) == baseline.chunk_digest,
                "block_digest": slice_final.block_digest,
                "block_matches_baseline": slice_final.block_digest == baseline.block_digest,
                "outside_span": H.outside_span_is_intact(
                    baseline.chunk, chunk_final, baseline.slice, slice_final
                ),
                "document_leaves": {
                    key: value
                    for key, value in final_leaves.items()
                    if key not in ("mismatched", "new_paths_sample", "derived_mismatched")
                },
                "document_leaves_mismatched": final_leaves["mismatched"],
                "document_restored": bool(final_leaves["all_equal"]),
                "parameters_exact": {
                    key: value for key, value in final_parameters.items() if key != "mismatched"
                },
                "parameters_within_contract_tolerance": {
                    key: value for key, value in within_tolerance.items() if key != "mismatched"
                },
                "remaining_mismatches": sorted(
                    row["ident"] for row in final_parameters["mismatched"]
                ),
                "active_routes": [row for row in document_final["routes"] if row.get("destination")],
            }
            final = report["restore"]["final"]
            # what the state *is*, not which call produced it: the control restore is
            # required, the document write is the prescribed recovery and may be
            # refused by a safety gate - in which case the **document comparison** is
            # what decides, and a document that still differs keeps `verified` false
            report["restore"]["verified"] = bool(
                report["restore"]["verified_flag"]
                and final["document_restored"]
                and final["parameters_within_contract_tolerance"]["all_equal"]
                and final["block_matches_baseline"]
                and not final["active_routes"]
            )
            # recorded, not hidden: the prescribed whole-document restore is refused by
            # the route gate when its change set carries route-table leaves
            report["restore"]["document_write_was_required"] = bool(document_drift)
            report["restore"]["document_write_outcome"] = (
                None
                if not document_drift
                else ("accepted" if report["restore"].get("document_write_ok") else "refused")
            )
            report["restore"]["state_confirmed_by_measurement"] = bool(
                final["document_restored"] and final["parameters_exact"]["all_equal"]
            )
            report["writes"]["total"] = counter.count()

        report["restore"]["completeness"] = H.restore_completeness(
            channels_verified=bool(report["restore"]["verified"]),
            declared_derived=report["restore"].get("derived_excluded") or [],
        )
        path = H.write_run_json("p12_live_smoke", report, H.EVIDENCE_DIR)
        report["evidence"] = str(path)
        print(f"\n[p12 smoke] evidence: {path}")

    # ---- assertions last: the project is restored at this point --------------
    assert not report.get("write_exception"), f"{report['write_exception']} (blocker={report['blocker']})"
    assert report["control"]["host_name_matches_declared"], report["control"]
    assert report["control"]["ident_readable"], report["control"]
    assert report["control"]["target_differs_from_original"], report["control"]
    assert report["after_write"]["document_leaves"]["compared"] > 0, (
        "the document comparison must compare real leaves, or it proves nothing"
    )
    assert report["pre_restore"]["document_comparison_is_not_vacuous"], report["pre_restore"]
    assert report["baseline"]["parameters"] > 0, "the recovery set must carry the parameter vector"
    assert report["restore"]["final"]["document_leaves"]["compared"] > 0, report["restore"]["final"]
    assert report["restore"]["final"]["parameters_exact"]["compared"] == report["baseline"]["parameters"], (
        "the restored vector must be compared ident by ident against the captured one"
    )
    # NOTE: `prior_normalized_agrees`/`ident_agrees_with_prior` are **recorded, not
    # asserted**: the previously recorded value is a candidate, and a disagreement
    # (the project moved on since 2026-10-10) is an observation for the report, not a
    # failure of this run - the run re-derives its own baseline either way.
    steps = {row["step"]: row for row in report["steps"]}
    assert steps["plugin_instances"]["ok"], steps["plugin_instances"]
    assert steps["plugin_instances"]["n_instances"] == 1, steps["plugin_instances"]
    assert steps["plugin_instances"]["location_matches_target"], steps["plugin_instances"]
    assert steps["plugin_instances"]["token_present"], "the read token is still issued"
    assert steps["plugin_describe"]["ok"] and steps["plugin_read(before)"]["ok"]

    # dry run: zero writes, measured at the host boundary
    dry = steps["plugin_write(dry_run=true)"]
    assert dry["ok"] is True, dry
    assert dry["verdict"] == "NO_WRITES", dry
    assert dry["host"]["write_calls_attempted"] == 0, dry
    assert dry["would_write"] is True, dry
    assert report["writes"]["dry_run"] == [], report["writes"]["dry_run"]

    # the committed write: exactly one parameter write, verified
    commit = steps["plugin_write(dry_run=false)"]
    assert commit["ok"] is True, commit
    assert commit["verdict"] == "ALL_VERIFIED", commit
    assert commit["host"]["write_calls_attempted"] >= 1, commit
    assert [row["verb"] for row in report["writes"]["commit"]] == ["write_params"], (
        "exactly one parameter write verb, and no state write"
    )
    assert abs(report["after_write"]["normalized_delta"]) <= TOLERANCE_NORMALIZED, report["after_write"]

    # the restore: one parameter write back, verified by an independent read
    assert steps["plugin_write(restore)"]["ok"] is True, steps["plugin_write(restore)"]
    assert [row["verb"] for row in report["writes"]["restore"]] == ["write_params"], (
        report["writes"]["restore"]
    )
    expected_writes = 3 if report["writes"]["document_restore"] else 2
    assert report["writes"]["total"] == expected_writes, report["writes"]
    assert report["restore"]["verified"], (
        "the restore was not confirmed per channel; no further live mutation may be performed - "
        f"see {report.get('evidence')}"
    )
