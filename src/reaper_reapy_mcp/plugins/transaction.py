"""The transaction engine: preflight -> plan -> snapshot -> apply -> verify ->
commit | per-channel verified rollback (contract 11; the safety core).

Rules enforced here, not by convention:

- **complete capture or refusal** before the first write (AUD-002),
- **token + expected-revision revalidation** before every unit and the
  parameter batch (AUD-003/AUD-010), never a silent re-target,
- **verify in the space that was written**; "API success" is never an effect,
- **per-channel verified rollback** with the external-change rule (a divergent
  channel is left untouched and reported ``not_restored (external_change)``),
- **honest counters**: "zero host writes" is only ever claimed from
  ``write_calls_attempted == 0``.

The engine is provider-agnostic and host-agnostic: it talks to a
`PluginProvider`, a `StateCodec` and a `HostBridge`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from . import dependencies as D
from . import plan as P
from . import values as V
from . import binding as B
from .errors import (
    InvalidValue,
    NotConnected,
    PlanError,
    PluginError,
    ReadbackMismatch,
    RestoreIncomplete,
    RollbackFailed,
    SnapshotUnavailable,
    UnsupportedControl,
    VersionMismatch,
    Wedged,
    WriteNotApplied,
    WriteOrderViolation,
    error_from_code,
)
from .model import (
    ChannelReport,
    FinalState,
    HostAccounting,
    Outcome,
    Plan,
    PluginInstanceRef,
    ResolvedRequest,
    RevisionTracker,
    RollbackReport,
    Stage,
    StateRevision,
    StateSnapshot,
    StateWriteUnit,
    TimingProfile,
    Verdict,
    canonical_json,
    final_state_for,
    text_digest,
)
from .resolve import TargetToken, TargetTokenRegistry, version_refusal

__all__ = ["Transaction", "TransactionResult"]

#: Errors a write verb may raise that leave the effect unknowable.
_TRANSPORT_LOSSES = (NotConnected, Wedged)

#: How many channels the outcome reports (05 §2.8).
_OUTCOME_CHANNELS = ("state", "parameters")


@dataclass
class TransactionResult:
    ok: bool
    committed: bool
    outcome: Outcome
    plan: Plan | None = None
    error: PluginError | None = None
    data: dict[str, Any] = field(default_factory=dict)


class Transaction:
    """One request, one transaction. Construct, then ``run()``."""

    def __init__(
        self,
        *,
        provider: Any,
        codec: Any,
        bridge: Any,
        profile: dict[str, Any],
        ref: PluginInstanceRef,
        request: dict[str, Any],
        token: TargetToken,
        registry: TargetTokenRegistry,
        dry_run: bool = True,
        cancel: Callable[[], bool] | None = None,
        timing: TimingProfile | None = None,
        label: str = "plugin_write",
    ) -> None:
        self.provider = provider
        self.codec = codec
        self.bridge = bridge
        self.profile = profile
        self.ref = ref
        self.request = dict(request)
        self.token = token
        self.registry = registry
        # a non-boolean is refused rather than truthiness-tested: `0` from a
        # client means "do not apply", and `bool(0)` would apply for real
        self.dry_run = V.strict_bool(dry_run, "dry_run")
        self.cancel = cancel
        self.label = label

        self.policy = provider.verification_policy()
        self.scope = provider.capture_scope()
        self.timing = timing or self.policy.timing or TimingProfile()
        self.write_order: tuple[str, ...] = tuple(provider.write_order())

        self.host = HostAccounting()
        self.stage: Stage = Stage.PREFLIGHT
        self.binding_report = B.BindingReport()
        self.version_gate: dict[str, Any] | None = None
        self._tracker = RevisionTracker()
        self._snapshot: StateSnapshot | None = None
        self._last_slice: Any = None
        self._last_vector: dict[str, float] = {}
        self._last_verified: dict[str, str] = {}
        self._attempted: dict[str, set[str]] = {"state": set(), "parameters": set()}
        self._rows: dict[str, list[dict[str, Any]]] = {"state": [], "parameters": []}
        self._mismatched: dict[str, list[dict[str, Any]]] = {"state": [], "parameters": []}
        self._named_gaps: list[dict[str, Any]] = []
        self._resolved: ResolvedRequest | None = None
        self._rollback = RollbackReport()

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def run(self) -> TransactionResult:
        """Execute the transaction under the target-scoped in-process lock."""
        with self.registry.lock_for(self.token.id):
            return self._run_locked()

    def _run_locked(self) -> TransactionResult:
        plan: Plan | None = None
        try:
            # P8 (07 §3.6): the live ident binding is measured **before** the
            # request is normalized, because an unbound control must refuse
            # rather than resolve to an unaddressable write.
            self._bind_parameter_idents()
            resolved = self.provider.normalize_request(self.profile, self.request)
            self._resolved = resolved
            if resolved.problems:
                raise _problem_error(resolved.problems)

            self.stage = Stage.PLAN
            self._revalidate_target()
            live = self._read_live_state(resolved)
            plan = P.build_plan(
                profile=self.profile, ref=self.ref, resolved=resolved, live=live, label=self.label
            )
            plan.findings = D.evaluate(self.provider.dependencies(), P.project_world(live, resolved))
            D.raise_on(plan.findings)

            self.stage = Stage.SNAPSHOT
            snapshot = self._assemble_snapshot(live)
            self._snapshot = snapshot
            self._tracker.seed(snapshot.base_revision)

            if self.dry_run:
                # The preview is built through the SAME path a real run uses, so
                # a request the write path must refuse (a derived leaf, an
                # unbound ident) refuses here **identically**. A preview that
                # answers `would_write: false` instead of surfacing the refusal
                # is an unfaithful preview of exactly the requests that exist to
                # prevent an unverifiable write. Nothing is written while
                # building: `_prevalidate_units` only encodes in memory.
                units, parameter_pairs = self._build_units(live.document, resolved)
                return self._result(
                    ok=True,
                    committed=False,
                    verdict=Verdict.NO_WRITES,
                    stage=Stage.SNAPSHOT,
                    plan=plan,
                    data={
                        "dry_run": True,
                        "would_write": bool(units or parameter_pairs),
                        "plan_summary": self._plan_summary(plan, resolved),
                        "dependency_findings": [_finding_row(f) for f in plan.findings],
                        "preview": self._preview(live, resolved),
                        "preflight": self._preflight_evidence(),
                        "read_cost_next": {"needed": 0, "why": "the preview is already computed from live reads"},
                    },
                )

            units, parameter_pairs = self._build_units(live.document, resolved)
            for unit in units:
                self._apply_state_unit(unit)
            if parameter_pairs:
                self._apply_parameter_batch(parameter_pairs)
            self.stage = Stage.COMMIT
            return self._result(
                ok=True,
                committed=True,
                verdict=Verdict.ALL_VERIFIED,
                stage=Stage.COMMIT,
                plan=plan,
                data={
                    "committed": True,
                    "plan_summary": self._plan_summary(plan, resolved),
                    "dependency_findings": [_finding_row(f) for f in plan.findings],
                    "preflight": self._preflight_evidence(),
                    "read_cost_next": {
                        "needed": 0,
                        "why": "this response already contains the read-back of everything it changed",
                    },
                },
            )
        except PluginError as exc:
            return self._handle_error(exc, plan)

    # ------------------------------------------------------------------
    # Live binding and version preflight (P8)
    # ------------------------------------------------------------------

    def _bind_parameter_idents(self) -> None:
        """Measure the live ident binding for every declared control index.

        Two batched host reads (names, then idents) decide which declared
        controls are addressable on the build that is actually loaded. A
        control is bound only when the host's name at the declared index is one
        of the names the profile declares for it; everything else stays
        unbound and refuses later with its concrete reason (P8, 07 §3.6).

        A request that names **no** controls cannot write a parameter, so it
        pays for no binding reads; a request that names controls pays for the
        measurement before its names are resolved (an unresolvable name must not
        be able to skip the check that makes the resolvable ones addressable).
        """
        if not dict(self.request.get("controls") or {}):
            self.binding_report = B.BindingReport(controls_indexed=len(B.indexed_controls(self.profile)))
            return
        indexed = B.indexed_controls(self.profile)
        indices = [index for _control_id, index in indexed]
        names = self._read(self.bridge.param_names, self.ref, indices)
        idents = self._read(self.bridge.param_idents, self.ref, indices)
        if len(names) != len(indices) or len(idents) != len(indices):
            raise SnapshotUnavailable(
                "the host returned a short parameter table read; a short read is not evidence",
                detail={
                    "asked": len(indices),
                    "names": len(names),
                    "idents": len(idents),
                    "reason": "short_binding_read",
                },
            )
        self.profile, self.binding_report = B.bind_profile(
            self.profile, indexed=indexed, names=names, idents=idents
        )

    def _check_document_version(self, document: Any) -> None:
        """The live plugin version carried by the decoded document (07 §3.2).

        The host reports no plugin version on this path, so the version that a
        write is gated on comes from the document the provider decodes. The
        hook is **optional by value**: a provider that declares no
        ``document_version`` (or answers ``None``) simply has no document-side
        version to check, and that fact is recorded instead of being assumed.
        """
        hook = getattr(self.provider, "document_version", None)
        live_version = hook(document) if callable(hook) else None
        if live_version is None:
            self.version_gate = {
                "checked": False,
                "why": "the provider declares no document-derived version for this instance",
            }
            return
        live_version = str(live_version)
        self.version_gate = {"checked": True, "live_version": live_version, "source": "decoded_document"}
        reason = version_refusal(self.profile, live_version)
        if reason:
            identity = self.profile.get("identity") if isinstance(self.profile.get("identity"), dict) else {}
            raise VersionMismatch(
                reason,
                detail={
                    "live_version": live_version,
                    "compatible_versions": list(identity.get("compatible_versions") or []),
                    "override": None,
                    "source": "decoded_document",
                },
            )

    def _require_bound_parameters(
        self, resolved: ResolvedRequest, pairs: list[tuple[str, float]]
    ) -> None:
        """A resolved parameter control must have a verified ident (defence in depth).

        `parameter_pairs` skips a control with no ident, which would make the
        plan claim a change the batched write silently omits. That must never
        be reachable, so it is refused here loudly for **any** provider.
        """
        bound = {ident for ident, _value in pairs}
        for control_id, row in resolved.controls.items():
            if row.get("channel") != "parameter":
                continue
            binding = row.get("binding") or {}
            ident = binding.get("ident")
            if ident is None or str(ident) not in bound:
                unverified = binding.get("unverified") or {}
                raise UnsupportedControl(
                    "a parameter control has no verified ident binding; the write is refused "
                    "instead of being silently omitted from the batch",
                    detail={
                        "control": control_id,
                        "reason": str(unverified.get("reason") or "ident_not_bound"),
                        "binding": {
                            "index": binding.get("index"),
                            "observed_name": unverified.get("observed_name"),
                            "declared_names": unverified.get("declared_names"),
                        },
                    },
                )

    def _preflight_evidence(self) -> dict[str, Any]:
        """The measured preflight facts a caller can audit (P8)."""
        return {
            "binding": self.binding_report.as_dict(),
            "version_gate": dict(self.version_gate or {"checked": False}),
        }

    # ------------------------------------------------------------------
    # Stages
    # ------------------------------------------------------------------

    def _read_live_state(self, resolved: ResolvedRequest) -> P.LiveState:
        slice_, document = self._read_state()
        self._last_slice = slice_
        self._check_document_version(document)  # P8: the live version gates the write
        routes = list((document or {}).get("routes") or [])
        readback = self._read_planned_parameters(resolved)
        return P.LiveState(
            document=document,
            routes=routes,
            slice=slice_,
            revision=None,
            parameters=readback,
            parameter_readback=readback,
        )

    def _assemble_snapshot(self, live: P.LiveState) -> StateSnapshot:
        """Complete capture of every declared channel, or refuse (11 §2.3)."""
        document_snapshot = self.codec.snapshot_document(live.document)
        captured: list[str] = []
        if "document" in self.scope.channels:
            captured.append("document")
            if document_snapshot is None:
                raise SnapshotUnavailable(
                    "the document channel could not be captured", detail={"channel": "document"}
                )
        routes_part = None
        if "routes" in self.scope.channels:
            captured.append("routes")
            routes_part = (
                document_snapshot.get("routes") if isinstance(document_snapshot, dict) else None
            )
        parameters: dict[str, float] = {}
        if "parameters" in self.scope.channels:
            parameters = self._read_full_parameter_vector()
            if self.scope.parameters.get("scope") == "full_vector":
                expected = self._param_count()
                if expected is not None and len(parameters) != expected:
                    raise SnapshotUnavailable(
                        "the full parameter vector could not be captured",
                        detail={"expected": expected, "captured": len(parameters)},
                    )
            captured.append("parameters")
        not_captured = [channel for channel in self.scope.channels if channel not in captured]
        if not_captured:
            raise SnapshotUnavailable(
                "the capture scope is incomplete; the mutation is refused before the first write",
                detail={"not_captured": not_captured, "scope": list(self.scope.channels)},
            )
        revision = StateRevision.compute(
            state_channel=live.slice.block_text,
            parameters=parameters,
            scope=list(self.scope.channels),
        )
        # the channels the transaction tracks for its own writes
        self._last_verified["state"] = text_digest(live.slice.block_text)
        self._last_verified["parameters"] = _vector_digest(parameters)
        return StateSnapshot(
            document=document_snapshot,
            routes=routes_part,
            parameters=parameters,
            base_revision=revision,
            manifest={"captured": captured, "not_captured": []},
        )

    def _build_units(
        self, document: Any, resolved: ResolvedRequest
    ) -> tuple[list[StateWriteUnit], list[tuple[str, float]]]:
        changes = P.state_changes(resolved)
        state_units = list(self.codec.plan_state_writes(document, changes).units)
        pairs = P.parameter_pairs(resolved)
        self._require_bound_parameters(resolved, pairs)
        declared = tuple(stage for stage in self.write_order if stage != "verify")
        if declared not in (("state",), ("state", "parameters")):
            raise WriteOrderViolation(
                "the provider's declared write order is not the engine's enforced order",
                detail={"declared": list(self.write_order), "enforced": ["state", "parameters", "verify"]},
            )
        if any(unit.kind == "parameters" for unit in state_units):
            raise WriteOrderViolation("state units may not carry the parameter channel")
        self._prevalidate_units(document, state_units)
        return state_units, pairs

    def _prevalidate_units(self, document: Any, units: Sequence[StateWriteUnit]) -> None:
        """F3: a plan that cannot be built against the live state refuses
        BEFORE the first write (nothing is written while simulating)."""
        simulated = document
        current = self._last_slice
        for unit in units:
            new_block = self.codec.encode_slice(simulated, unit, current)
            simulated = self.codec.decode(new_block)

    # ------------------------------------------------------------------
    # Apply
    # ------------------------------------------------------------------

    def _apply_state_unit(self, unit: StateWriteUnit) -> None:
        self.stage = Stage.STATE_WRITE
        self._check_cancel()
        self._revalidate()
        current_slice, current_document = self._read_state()
        new_block = self.codec.encode_slice(current_document, unit, current_slice)
        self._attempted["state"].add(text_digest(new_block.block_text))

        self._issue_write(
            lambda: self.bridge.write_state_slice(self.ref, current_slice, new_block, timing=self.timing)
        )
        self._check_cancel(write_issued=True)

        self.stage = Stage.VERIFY
        readback_slice, readback_document = self._read_state()
        report = self.codec.verify(readback_document, unit, self.policy)
        self._rows["state"].extend(report.rows)
        self._mismatched["state"].extend(report.mismatched)
        self._establish_effect()
        if not report.ok:
            raise WriteNotApplied(
                "the state write was accepted by the host but the read-back does not agree",
                detail={"unit": unit.order_index, "mismatched": report.mismatched, "space": report.space},
            )
        self._last_slice = readback_slice
        self._last_verified["state"] = text_digest(readback_slice.block_text)
        # a document write can reset ordinary parameters (measured behaviour),
        # so the parameter channel is re-read and its expected digest advanced
        fresh = self._read_full_parameter_vector()
        self._last_vector = fresh
        self._last_verified["parameters"] = _vector_digest(fresh)
        self._tracker.advance_from_verified_readback(
            StateRevision.compute(state_channel=readback_slice.block_text, parameters=fresh)
        )

    def _apply_parameter_batch(self, pairs: list[tuple[str, float]]) -> None:
        self.stage = Stage.PARAMETER_WRITE
        self._check_cancel()
        self._revalidate()
        sent = {ident: value for ident, value in pairs}
        self._attempted["parameters"].add(_vector_digest({**self._last_vector, **sent}))

        self._issue_write(lambda: self.bridge.write_params(self.ref, pairs))
        self._check_cancel(write_issued=True)

        self.stage = Stage.VERIFY
        readback = self._read_full_parameter_vector(ident_filter=[ident for ident, _ in pairs])
        rows, mismatched = self._verify_parameters(sent, readback)
        self._rows["parameters"].extend(rows)
        self._mismatched["parameters"].extend(mismatched)
        self._establish_effect()
        if mismatched:
            raise ReadbackMismatch(
                "the parameter read-back does not agree in the space that was written",
                detail={"mismatched": mismatched, "space": "normalized"},
            )
        full = self._read_full_parameter_vector()
        self._last_vector = full
        self._last_verified["parameters"] = _vector_digest(full)
        if self._last_slice is not None:
            self._tracker.advance_from_verified_readback(
                StateRevision.compute(state_channel=self._last_slice.block_text, parameters=full)
            )

    # ------------------------------------------------------------------
    # Validation / revalidation
    # ------------------------------------------------------------------

    def _fresh_facts(self):
        return self._read(self.bridge.fx_facts, self.ref.location["track_index"], self.ref.location["fx_index"])

    def _revalidate_target(self) -> None:
        facts = self._fresh_facts()
        self.registry.revalidate(self.token.id, facts)  # TargetChanged, never re-targets

    def _revalidate(self) -> None:
        """Token + expected revision before every write unit / parameter batch."""
        self._revalidate_target()
        slice_, _ = self._read_state()
        vector = self._read_full_parameter_vector()
        self._last_slice = slice_
        self._last_vector = vector
        observed = StateRevision.compute(state_channel=slice_.block_text, parameters=vector)
        self._tracker.check(observed)  # StateConflict before that unit's write

    def _check_cancel(self, *, write_issued: bool = False) -> None:
        if self.cancel is None or not self.cancel():
            return
        detail = {"cancelled": True, "stage": str(self.stage.value)}
        if self.host.write_calls_attempted == 0:
            raise PlanError("cancelled before any write was issued", detail=detail)
        # a write is in flight or unverified: its effect stays provisionally
        # unknown (counted by _issue_write) unless a read-back establishes it
        raise PlanError(
            "cancelled after a write was issued; the effect is reported truthfully", detail=detail
        )

    # ------------------------------------------------------------------
    # Host reads (counted)
    # ------------------------------------------------------------------

    def _read(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        self.host.reads += 1
        return fn(*args, **kwargs)

    def _issue_write(self, call: Callable[[], Any]) -> Any:
        """Count one write verb BEFORE it is issued and mark its effect
        provisionally unknown.

        The effect is established only by a later read-back
        (`_establish_effect`), so an exception anywhere between the verb's
        return and that read-back - a dead transport during the verify read, a
        cancellation, a wedge - leaves the write accounted as unknown instead of
        silently uncounted. That keeps `attempted == verified + unknown` true in
        every terminal state (B-46).
        """
        self.host.write_calls_attempted += 1
        self.host.write_effects_unknown += 1
        return call()

    def _establish_effect(self) -> None:
        """Called only once a read-back established the effect beyond doubt."""
        self.host.write_effects_unknown -= 1
        self.host.write_effects_verified += 1

    def _read_state(self) -> tuple[Any, Any]:
        chunk = self._read(self.bridge.read_state_chunk, self.ref)
        facts = self._read(self.bridge.fx_facts, self.ref.location["track_index"], self.ref.location["fx_index"])
        slice_ = self._read(self.bridge.locate_state_slice, self.ref, facts, chunk)
        document = self.codec.decode(slice_)
        return slice_, document

    def _param_count(self) -> int | None:
        facts = self._read(
            self.bridge.fx_facts, self.ref.location["track_index"], self.ref.location["fx_index"]
        )
        return facts.param_count

    def _read_full_parameter_vector(self, *, ident_filter: Sequence[str] | None = None) -> dict[str, float]:
        if ident_filter is not None:
            indices = self._indices_for(ident_filter)
        else:
            count = self._param_count()
            if count is None:
                raise SnapshotUnavailable(
                    "the host does not report a parameter count", detail={"field": "param_count"}
                )
            indices = list(range(count))
        idents = self._read(self.bridge.param_idents, self.ref, indices)
        if any(ident is None for ident in idents):
            raise SnapshotUnavailable(
                "a parameter has no durable ident; the vector cannot be captured",
                detail={"missing": [index for index, ident in zip(indices, idents) if ident is None]},
            )
        values = self._read(self.bridge.read_params, self.ref, indices)
        if len(values) != len(indices):
            raise SnapshotUnavailable(
                "the parameter read returned fewer values than requested; a short read is not evidence",
                detail={"expected": len(indices), "read": len(values)},
            )
        return {str(ident): float(value) for ident, value in zip(idents, values)}

    def _indices_for(self, idents: Sequence[str]) -> list[int]:
        """Index lookup through one ident read (idents are the durable binding)."""
        count = self._param_count()
        if count is None:
            raise SnapshotUnavailable("the host does not report a parameter count", detail={})
        all_idents = self._read(self.bridge.param_idents, self.ref, list(range(count)))
        index_by_ident = {ident: index for index, ident in enumerate(all_idents) if ident}
        missing = [ident for ident in idents if ident not in index_by_ident]
        if missing:
            raise SnapshotUnavailable(
                "the parameter ident is not present in the live parameter table", detail={"missing": missing}
            )
        return [index_by_ident[ident] for ident in idents]

    def _read_planned_parameters(self, resolved: ResolvedRequest) -> dict[str, float]:
        idents = [
            str((row.get("binding") or {}).get("ident"))
            for row in resolved.controls.values()
            if row.get("channel") == "parameter" and (row.get("binding") or {}).get("ident") is not None
        ]
        if not idents:
            return {}
        indices = self._indices_for(idents)
        values = self._read(self.bridge.read_params, self.ref, indices)
        return {ident: float(value) for ident, value in zip(idents, values)}

    # ------------------------------------------------------------------
    # Verification helpers
    # ------------------------------------------------------------------

    def _verify_parameters(
        self, sent: dict[str, float], readback: dict[str, float]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        tolerance = float(self.policy.tolerances.get("parameter.normalized", 1e-03))
        by_ident = {
            str((row.get("binding") or {}).get("ident")): row["control"]
            for row in (self._resolved.controls.values() if self._resolved else [])
            if row.get("channel") == "parameter"
        }
        rows: list[dict[str, Any]] = []
        mismatched: list[dict[str, Any]] = []
        for ident, value in sent.items():
            control = by_ident.get(ident)
            actual = readback.get(ident)
            row: dict[str, Any] = {
                "ident": ident,
                "control": (control or {}).get("id"),
                "written": value,
                "read_back": actual,
                "channel": "parameter",
            }
            if actual is None:
                row.update({"agrees": False, "space": "normalized", "reason": "unreadable"})
            else:
                # compare in the space that was written; display is re-resolved
                # through the same conversion the write used
                if control is not None:
                    expected_native = V.normalized_to_native(control, value)
                    actual_native = V.normalized_to_native(control, actual)
                    row["display_expected"] = V.from_native(control, expected_native)
                    row["display_read_back"] = V.from_native(control, actual_native)
                    if control.get("type") in ("enum", "bool", "state_ref"):
                        row["agrees"] = row["display_expected"] == row["display_read_back"]
                        row["space"] = "display"
                    else:
                        row["agrees"] = abs(float(value) - float(actual)) <= tolerance
                        row["space"] = "normalized"
                else:
                    row["agrees"] = abs(float(value) - float(actual)) <= tolerance
                    row["space"] = "normalized"
            rows.append(row)
            if not row["agrees"]:
                mismatched.append(row)
        return rows, mismatched

    # ------------------------------------------------------------------
    # Rollback
    # ------------------------------------------------------------------

    def _handle_error(self, exc: PluginError, plan: Plan | None) -> TransactionResult:
        if self.host.write_calls_attempted == 0:
            return self._result(
                ok=False,
                committed=False,
                verdict=Verdict.REFUSED_BEFORE_ANY_WRITE,
                stage=self.stage,
                plan=plan,
                error=exc,
                data={"refused": True, "problems": exc.detail.get("findings", [])},
            )
        return self._rollback_and_report(exc, plan)

    def _rollback_and_report(self, exc: PluginError, plan: Plan | None) -> TransactionResult:
        failure_stage = self.stage
        self.stage = Stage.ROLLBACK
        self._rollback = RollbackReport(attempted=True, why=f"{exc.code}: {exc.message}")
        failed_loudly = False
        state_ok = False
        parameters_ok = False
        # Restorability is decided for BOTH channels BEFORE any rollback write
        # (Decision 3): the transaction's own subsequent verified writes are
        # then attributed to itself, while a channel that was already foreign is
        # never laundered into "mine" by a sibling restore.
        allowed: dict[str, bool] = {}
        for channel in _OUTCOME_CHANNELS:
            try:
                allowed[channel] = self._channel_restorable(channel)
            except PluginError as probe_exc:
                failed_loudly = True
                allowed[channel] = False
                self._named_gaps.append(_gap(channel, f"restorability probe failed: {probe_exc.code}"))
                continue
            if not allowed[channel]:
                self._named_gaps.append(_gap(channel, "not_restored (external_change)"))
        try:
            state_ok = self._restore_state_channel(allowed=allowed["state"])
        except _TRANSPORT_LOSSES as restore_exc:
            failed_loudly = True
            self._named_gaps.append(_gap("state", f"{restore_exc.code}: {restore_exc.message}"))
        except PluginError as restore_exc:
            failed_loudly = True
            self._named_gaps.append(_gap("state", f"{restore_exc.code}: {restore_exc.message}"))
        try:
            parameters_ok = self._restore_parameter_channel(allowed=allowed["parameters"])
        except _TRANSPORT_LOSSES as restore_exc:
            failed_loudly = True
            self._named_gaps.append(_gap("parameters", f"{restore_exc.code}: {restore_exc.message}"))
        except PluginError as restore_exc:
            failed_loudly = True
            self._named_gaps.append(_gap("parameters", f"{restore_exc.code}: {restore_exc.message}"))

        self._rollback.channels = {"state": state_ok, "parameters": parameters_ok}
        self._rollback.verified = state_ok and parameters_ok
        # `verified` says the restore's own writes landed; `complete` says it
        # covered everything that was captured. A codec-declared derived leaf is
        # excluded on purpose, so the two must not be conflated (Decision 2/5).
        self._rollback.complete = self._rollback.verified and not self._rollback.excluded
        if failed_loudly:
            verdict = Verdict.ROLLBACK_FAILED
        elif self._rollback.complete:
            verdict = Verdict.FAILED_AND_RESTORED
        elif state_ok and parameters_ok:
            # every channel this restore wrote is verified, but the captured set
            # was not fully restorable: reporting `FAILED_AND_RESTORED` here would
            # claim `unchanged_verified` for state that is not unchanged
            verdict = Verdict.FAILED_AND_PARTIALLY_RESTORED
        elif state_ok or parameters_ok:
            verdict = Verdict.FAILED_AND_PARTIALLY_RESTORED
        else:
            verdict = Verdict.FAILED_AND_NOT_RESTORED
        error: PluginError = exc
        if verdict is Verdict.ROLLBACK_FAILED:
            error = RollbackFailed(
                "the restore attempt raised or produced an unverifiable read-back",
                detail={"cause": exc.to_dict(), "named_gaps": self._named_gaps},
            )
        return self._result(
            ok=False,
            committed=False,
            verdict=verdict,
            # where the operation ended: the failing stage, or ROLLBACK when the
            # compensation itself failed (the loud case)
            stage=Stage.ROLLBACK if verdict is Verdict.ROLLBACK_FAILED else failure_stage,
            plan=plan,
            error=error,
            data={"rollback": {"attempted": True, "verified": self._rollback.verified}},
        )

    def _channel_restorable(self, channel: str) -> bool:
        """Restore only a channel the transaction still owns (Decision 3).

        A channel is restorable when it holds the transaction's own last
        **verified** state, or the state the transaction itself attempted to
        write (recognised from the bytes it sent, never from an unverified
        read-back). Anything else is foreign state: left untouched.
        """
        if channel == "state":
            slice_, _ = self._read_state()
            observed = text_digest(slice_.block_text)
        else:
            observed = _vector_digest(self._read_full_parameter_vector())
        if observed == self._last_verified.get(channel):
            return True
        return observed in self._attempted.get(channel, set())

    def _restore_state_channel(self, *, allowed: bool) -> bool:
        if not allowed:
            return False
        self._revalidate_target()  # a stale token at rollback time is ROLLBACK_FAILED
        slice_, document = self._read_state()
        snapshot = self._snapshot
        changes = self.codec.restore_changes(document, snapshot.document)
        # A codec may declare parts of the captured document it cannot restore because
        # the **plugin** re-derives them (measured live in P8: a sample blob the plugin
        # re-serializes from its own buffer).
        # They are named here, so an outcome that says "restored" says which captured
        # leaves were excluded and why - never silently fewer. (05/11 named gaps.)
        for path in changes.get("derived_excluded") or []:
            self._named_gaps.append(
                {
                    "channel": "state",
                    "reason": "derived_leaf_not_restored",
                    "path": str(path),
                    "detail": "the provider declares this leaf as plugin-derived; a document write cannot set it",
                }
            )
            if str(path) not in self._rollback.excluded:
                self._rollback.excluded.append(str(path))
        units = list(self.codec.plan_state_writes(document, changes).units)
        if not units:
            # nothing to write: prove the captured document is already in place
            # rather than assuming it (an empty change set is the comparison)
            return _changes_are_empty(self.codec.restore_changes(document, snapshot.document))
        current_slice, current_document = slice_, document
        last_slice = slice_
        for unit in units:
            self._revalidate_target()
            new_block = self.codec.encode_slice(current_document, unit, current_slice)
            self._issue_write(
                lambda: self.bridge.write_state_slice(
                    self.ref, current_slice, new_block, timing=self.timing
                )
            )
            readback_slice, readback_document = self._read_state()
            report = self.codec.verify(readback_document, unit, self.policy)
            self._rows["state"].extend(report.rows)
            self._establish_effect()
            if not report.ok:
                raise RestoreIncomplete(
                    "the document restore did not land",
                    detail={"mismatched": report.mismatched},
                )
            current_slice, current_document = readback_slice, readback_document
            last_slice = readback_slice
            self._last_verified["state"] = text_digest(readback_slice.block_text)
        # the document restore may have reset parameters: re-read and advance
        fresh = self._read_full_parameter_vector()
        self._last_vector = fresh
        self._last_verified["parameters"] = _vector_digest(fresh)
        self._tracker.advance_from_verified_readback(
            StateRevision.compute(state_channel=last_slice.block_text, parameters=fresh)
        )
        return True

    def _restore_parameter_channel(self, *, allowed: bool) -> bool:
        snapshot = self._snapshot
        captured = dict(snapshot.parameters)
        if not captured:
            # no captured values: prove the live vector is empty too, instead of
            # reporting a channel "restored" that was never compared
            live = self._read_full_parameter_vector()
            return not live
        if not allowed:
            return False
        self._revalidate_target()
        pairs = [(ident, value) for ident, value in captured.items()]
        for start in range(0, len(pairs), 256):
            batch = pairs[start : start + 256]
            self._attempted["parameters"].add(
                _vector_digest({**self._last_vector, **{ident: value for ident, value in batch}})
            )
            self._issue_write(lambda: self.bridge.write_params(self.ref, batch))
            readback = self._read_full_parameter_vector()
            tolerance = float(self.policy.tolerances.get("parameter.normalized", 1e-03))
            mismatched: list[dict[str, Any]] = []
            restore_rows: list[dict[str, Any]] = []
            for ident, value in batch:
                actual = readback.get(ident)
                agrees = actual is not None and abs(float(actual) - float(value)) <= tolerance
                restore_rows.append(
                    {
                        "ident": ident,
                        "written": value,
                        "read_back": actual,
                        "channel": "parameter",
                        "restore": True,
                        "agrees": agrees,
                    }
                )
                if not agrees:
                    mismatched.append({"ident": ident, "written": value, "read_back": actual})
            self._rows["parameters"].extend(restore_rows)
            self._establish_effect()
            if mismatched:
                self._mismatched["parameters"].extend(mismatched)
                raise RestoreIncomplete(
                    "the parameter restore did not land", detail={"mismatched": mismatched}
                )
            self._last_vector = readback
            self._last_verified["parameters"] = _vector_digest(readback)
        return True

    # ------------------------------------------------------------------
    # Result assembly
    # ------------------------------------------------------------------

    def _preview(self, live: P.LiveState, resolved: ResolvedRequest) -> dict[str, Any]:
        changes = P.state_changes(resolved)
        return {
            "state_paths": changes["paths"],
            "routes_create": changes["routes_create"],
            "routes_update": changes["routes_update"],
            "routes_remove": changes["routes_remove"],
            "parameters": P.parameter_pairs(resolved),
        }

    def _plan_summary(self, plan: Plan | None, resolved: ResolvedRequest) -> dict[str, Any]:
        return {
            "label": plan.label if plan else self.label,
            "n_controls": len(resolved.controls),
            "n_routes_create": len(resolved.routes_create),
            "n_routes_update": len(resolved.routes_update),
            "n_routes_remove": len(resolved.routes_remove),
            "n_state_paths": len(resolved.state) + sum(
                1 for row in resolved.controls.values() if row.get("channel") == "state"
            ),
            "write_order": list(self.write_order),
        }

    def _result(
        self,
        *,
        ok: bool,
        committed: bool,
        verdict: Verdict,
        stage: Stage,
        plan: Plan | None,
        error: PluginError | None = None,
        data: dict[str, Any] | None = None,
    ) -> TransactionResult:
        outcome = Outcome(
            stage=stage,
            verdict=verdict,
            final_state=final_state_for(verdict),
            rollback=self._rollback,
            channels={
                name: ChannelReport(rows=self._rows[name], mismatched=self._mismatched[name])
                for name in _OUTCOME_CHANNELS
            },
            host=HostAccounting(
                reads=self.host.reads,
                write_calls_attempted=self.host.write_calls_attempted,
                write_effects_verified=self.host.write_effects_verified,
                write_effects_unknown=self.host.write_effects_unknown,
            ),
            named_gaps=list(self._named_gaps),
        )
        payload = dict(data or {})
        payload.setdefault("outcome", outcome.to_dict())
        # Honest disclosure on every result, refusals and dry-runs included: the
        # rules this provider declares but does not decide are named, so an empty
        # findings list is never read as "everything was checked" (Decision 1).
        payload.setdefault("dependency_rules_not_evaluated", self._unevaluated_rules())
        return TransactionResult(
            ok=ok, committed=committed, outcome=outcome, plan=plan, error=error, data=payload
        )

    def _unevaluated_rules(self) -> list[dict[str, Any]]:
        """The provider's honest list of rules it does not evaluate (optional hook)."""
        hook = getattr(self.provider, "unevaluated_rules", None)
        if not callable(hook):
            return []
        try:
            return list(hook())
        except PluginError:
            return [
                {
                    "evaluated": False,
                    "reason": "unevaluated_rule_report_unavailable",
                    "detail": "the provider's unevaluated-rule report raised",
                }
            ]


# ---------------------------------------------------------------------------
# Module helpers
# ---------------------------------------------------------------------------


def _vector_digest(values: dict[str, float]) -> str:
    return text_digest(canonical_json({str(k): float(v) for k, v in sorted(values.items())}))


def _changes_are_empty(changes: Any) -> bool:
    """True when a codec change set asks for nothing (a real comparison, not a
    statement about an assumed state)."""
    if not isinstance(changes, dict):
        return not changes
    for key in ("paths", "routes_create", "routes_update", "routes_remove"):
        if changes.get(key):
            return False
    return True


def _gap(channel: str, reason: str) -> dict[str, Any]:
    return {"channel": channel, "reason": reason}


def _finding_row(finding: Any) -> dict[str, Any]:
    """One finding as data, including whether the rule was actually decided.

    A preview that lists a rule without saying `evaluated: false` would present
    an unevaluated rule as checked (Decision 1).
    """
    detail = dict(getattr(finding, "detail", {}) or {})
    return {
        "rule": finding.rule,
        "kind": finding.kind,
        "message": finding.message,
        "subject": finding.subject,
        "informational": finding.informational,
        "evaluated": detail.get("evaluated", True),
        "reason": detail.get("reason"),
        "missing_input": detail.get("missing_input"),
        "detail": detail,
    }


def _problem_error(problems: list[dict[str, Any]]) -> PluginError:
    first = problems[0]
    code = str(first.get("code") or "InvalidValue")
    message = str(first.get("message") or "request refused")
    error = error_from_code(code, message, {"problems": problems})
    if isinstance(error, InvalidValue) and len(problems) == 1:
        error.detail.setdefault("problem", first)
    return error
