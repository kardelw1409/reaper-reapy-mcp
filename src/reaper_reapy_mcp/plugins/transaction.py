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

import copy
from dataclasses import dataclass, field
import time
from typing import Any, Callable, Mapping, Sequence

from . import dependencies as D
from . import logs as L
from . import plan as P
from . import request_value
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
        # P28: the declaration decides which channels this transaction owns. A
        # provider whose write order carries no state stage has no document
        # channel (a plugin reached through the providerless discovery path has
        # no codec), so the state reads/decode/rollback must not run at all - an
        # out-of-scope channel is REPORTED as such, never silently captured as
        # empty. And a provider may declare that its parameters are addressed by
        # their measured **index** (with a live name check inside the write hop)
        # instead of by an ident table read; that is a declared capability, and
        # the default keeps the ident path byte-identical.
        self.state_stage = "state" in self.write_order
        self.parameter_stage = "parameters" in self.write_order
        self.index_addressed = bool(getattr(provider, "index_addressed_parameters", False)) and (
            not self.state_stage
        )
        self.scope_channels: tuple[str, ...] = tuple(
            name for name, present in (("state", self.state_stage), ("parameters", self.parameter_stage)) if present
        )

        self.host = HostAccounting()
        self.stage: Stage = Stage.PREFLIGHT
        self.binding_report = B.BindingReport()
        self.version_gate: dict[str, Any] | None = None
        self._tracker = RevisionTracker()
        self._snapshot: StateSnapshot | None = None
        self._last_slice: Any = None
        self._last_document: Any = None
        self._last_vector: dict[str, float] = {}
        self._last_verified: dict[str, str] = {}
        self._attempted: dict[str, set[str]] = {"state": set(), "parameters": set()}
        self._rows: dict[str, list[dict[str, Any]]] = {"state": [], "parameters": []}
        self._mismatched: dict[str, list[dict[str, Any]]] = {"state": [], "parameters": []}
        self._named_gaps: list[dict[str, Any]] = []
        #: Paths this transaction has already WRITTEN, with the exact value it wrote.
        #:
        #: MEASURED (2026-10-18, live): the plugin re-encodes some payloads on every state load -
        #: a sample's PCM comes back delayed by `SAMPLE_RELOAD_DELAY` (4) samples. Every later unit
        #: re-sends the WHOLE state, and the document it sends comes from the read-back, so the
        #: delay compounded once per following write: a 12-unit apply left the file's sample
        #: 48 samples late, and the layer's own per-unit verification could not see the drift
        #: (each step was within the measured reload delay). Re-injecting the value we INTENDED -
        #: exactly what a writer that knows its target does - keeps the instance on the target and
        #: leaves the plugin's own one-round-trip delay as the only residue.
        self._intended: dict[str, Any] = {}
        #: Route operations the plugin accepted and then silently dropped on
        #: re-serialisation (an unknown pair name - measured behaviour, P38).
        #: Named in the outcome like any other gap, and reported as `partial`.
        self._dropped_routes: list[dict[str, Any]] = []
        self._resolved: ResolvedRequest | None = None
        #: The batch's unresolved-control policy: `refuse` (default, historical) or
        #: `skip` (explicit opt-in). Validated in `__init__`, before any host work.
        self.on_unresolved: str = request_value.normalize_request_mode(request)
        #: The controls a `skip` batch left out, each with its own typed problem.
        self.refused: list[dict[str, Any]] = []
        self._rollback = RollbackReport()
        #: The display-space cross-check of the index-addressed write path (see
        #: `_display_check`): a second channel beside the normalized comparison.
        self._display_rows: list[dict[str, Any]] = []
        #: How the parameter addresses of this request are verified. The ident
        #: path measures the whole ident table before planning; the index path
        #: verifies the live name at each index **inside the write hop**, so no
        #: separate binding pass exists to report.
        self.binding_method = (
            "index_name_guarded_in_write_hop" if self.index_addressed else "ident_table_verified"
        )
        #: The instance's index -> ident table and the parameter count it was read
        #: for, cached for the transaction (see `_parameter_idents`). P38 measured
        #: 2026-10-17: the full vector read used to pay an ident sweep (~3.0 s over
        #: 2858 parameters) twice per state unit, which dominated a preset apply.
        self._ident_table: list[str] | None = None
        self._ident_count: int | None = None
        #: The last facts Core read for the target (`_revalidate_target`), reused by
        #: the parameter-count lookups inside one transaction.
        self._facts: Any = None

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
                if self.on_unresolved == "skip" and _workload(resolved):
                    # Explicit opt-in: the resolved part is applied and every refused
                    # entry is reported with its own typed problem. The response says
                    # `partial: true` so a batch is never read as wholly applied.
                    self.refused = list(resolved.problems)
                else:
                    # Default (and the only choice when nothing resolved at all): the
                    # batch writes nothing and the refusal names every offender.
                    raise _problem_error(resolved.problems)

            self.stage = Stage.PLAN
            self._revalidate_target()
            live = self._read_live_state(resolved)
            plan = P.build_plan(
                profile=self.profile,
                ref=self.ref,
                resolved=resolved,
                live=live,
                label=self.label,
                parameter_key=self._parameter_key if self.index_addressed else None,
                # The plan states the order the engine will enforce, which is the
                # provider's own declaration (plus the verify stage).
                write_order=tuple(self.write_order) + (("verify",) if "verify" not in self.write_order else ()),
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
                        "refused": list(self.refused),
                        "partial": bool(self.refused),
                        "read_cost_next": {"needed": 0, "why": "the preview is already computed from live reads"},
                    },
                )

            units, parameter_pairs = self._build_units(live.document, resolved)
            # A long chain must be visible WHILE it runs (owner's ask, 2026-10-17): the
            # log carries one line per unit with its duration, and the tool line (written
            # by the registration closure) carries the total. Nothing here can fail the
            # call: the writer swallows its own errors.
            chain_started = time.perf_counter()
            L.event(
                "transaction.start",
                label=self.label,
                units=len(units),
                parameters=len(parameter_pairs),
                tool=(self.request or {}).get("__tool") if isinstance(self.request, dict) else None,
            )
            for position, unit in enumerate(units, start=1):
                unit_started = time.perf_counter()
                balloon = (unit.payload or {}).get("balloon")
                shape = {
                    "label": self.label,
                    "index": position,
                    "of": len(units),
                    "kind": unit.kind,
                    "paths": len((unit.payload or {}).get("paths") or {}),
                    # P39: a wavetable too big to land in one write rides in two, and the
                    # first one is a balloon - named here so a long chain says which unit
                    # is opening room rather than looking like a stray state write.
                    "balloon": (
                        {"path": balloon.get("path"), "frames": balloon.get("frames")}
                        if isinstance(balloon, dict)
                        else None
                    ),
                    "route": (unit.payload or {}).get("route_op", {}).get("op")
                    if isinstance((unit.payload or {}).get("route_op"), dict)
                    else None,
                }
                try:
                    self._apply_state_unit(unit)
                except PluginError as exc:
                    # a unit that fails must still leave its line: a silent failure is
                    # exactly what this log exists to make visible
                    L.event(
                        "unit.failed",
                        seconds=round(time.perf_counter() - unit_started, 3),
                        code=exc.code,
                        **shape,
                    )
                    raise
                L.event("unit", seconds=round(time.perf_counter() - unit_started, 3), **shape)
            if parameter_pairs:
                self._apply_parameter_batch(parameter_pairs)
            self.stage = Stage.COMMIT
            L.event(
                "transaction.end",
                label=self.label,
                ok=True,
                units=len(units),
                seconds=round(time.perf_counter() - chain_started, 3),
            )
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
                    "refused": list(self.refused),
                    "partial": bool(self.refused) or bool(self._dropped_routes),
                    # A pair the plugin's own load would drop too: named, never
                    # implied to have landed (`partial` covers it as well).
                    "dropped_routes": list(self._dropped_routes),
                    "read_cost_next": {
                        "needed": 0,
                        "why": "this response already contains the read-back of everything it changed",
                    },
                },
            )
        except PluginError as exc:
            L.event("transaction.error", label=self.label, code=exc.code, stage=str(self.stage.value))
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
        if self.index_addressed:
            # The index path does not read the ident table: the artifact measured
            # the index, and the write hop re-reads the live *name* at that index
            # before calling the setter, so a stale index refuses there instead of
            # being written. Report what was (not) measured rather than an empty
            # binding pass that would read as "nothing verified".
            self.binding_report = B.BindingReport(controls_indexed=len(indexed))
            return
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
        self, resolved: ResolvedRequest, pairs: list[tuple[Any, ...]]
    ) -> None:
        """A resolved parameter control must have a verified address (defence in depth).

        `parameter_pairs` skips a control with no address, which would make the
        plan claim a change the batched write silently omits. That must never
        be reachable, so it is refused here loudly for **any** provider.
        """
        if self.index_addressed:
            addressed = {int(pair[0]) for pair in pairs}
            for control_id, row in resolved.controls.items():
                if row.get("channel") != "parameter":
                    continue
                binding = row.get("binding") or {}
                index = binding.get("index")
                if isinstance(index, bool) or not isinstance(index, int) or index not in addressed:
                    raise UnsupportedControl(
                        "a parameter control has no usable index address; the write is refused "
                        "instead of being silently omitted from the batch",
                        detail={
                            "control": control_id,
                            "reason": "index_missing",
                            "binding": dict(binding),
                        },
                    )
            return
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
            "binding": {**self.binding_report.as_dict(), "method": self.binding_method},
            "version_gate": dict(self.version_gate or {"checked": False}),
            "channels_in_scope": list(self.scope_channels),
        }

    # ------------------------------------------------------------------
    # Parameter addressing (ident table, or measured index + live name)
    # ------------------------------------------------------------------

    def _parameter_key(self, binding: Mapping[str, Any]) -> str | None:
        """The key a parameter is tracked under in vectors and verification rows."""
        if self.index_addressed:
            index = binding.get("index")
            if isinstance(index, bool) or not isinstance(index, int):
                return None
            return f"#{int(index)}"
        ident = binding.get("ident")
        return None if ident is None else str(ident)

    def _parameter_pairs(self, resolved: ResolvedRequest) -> list[tuple[Any, ...]]:
        """The one batched parameter write, in plan order, in this path's address space.

        Ident path: ``(ident, normalized)`` - unchanged. Index path:
        ``(index, expected_live_name, normalized)``, where the name is the one the
        artifact measured at that index; the hop re-reads it and refuses a
        mismatch before the setter runs.
        """
        pairs: list[tuple[Any, ...]] = []
        for row in resolved.controls.values():
            if row.get("channel") != "parameter":
                continue
            binding = dict(row.get("binding") or {})
            if self.index_addressed:
                index = binding.get("index")
                if isinstance(index, bool) or not isinstance(index, int):
                    continue
                pairs.append((int(index), str(binding.get("live_name") or ""), float(row["normalized"])))
            else:
                ident = binding.get("ident")
                if ident is None:
                    continue
                pairs.append((str(ident), float(row["normalized"])))
        return pairs

    def _planned_indices(self) -> list[int]:
        """The indices the resolved request names, in plan order (index path)."""
        resolved = self._resolved
        if resolved is None:
            raise SnapshotUnavailable(
                "the planned parameter set is unknown before the request is resolved",
                detail={"reason": "plan_not_resolved"},
            )
        indices: list[int] = []
        for row in resolved.controls.values():
            if row.get("channel") != "parameter":
                continue
            index = (row.get("binding") or {}).get("index")
            if isinstance(index, bool) or not isinstance(index, int):
                raise SnapshotUnavailable(
                    "a planned control declares no integer index, so the planned vector cannot "
                    "be captured",
                    detail={"control": (row.get("control") or {}).get("id"), "index": index},
                )
            indices.append(int(index))
        return indices

    def _read_planned_vector(self) -> dict[str, float]:
        """The current values of exactly the planned indices, keyed ``#<index>``.

        This is the capture scope of the index path: the parameters the request
        touches, read in ONE hop, instead of the whole parameter table twice.
        """
        indices = self._planned_indices()
        if not indices:
            return {}
        values = self._read(self.bridge.read_params, self.ref, indices)
        if len(values) != len(indices):
            raise SnapshotUnavailable(
                "the parameter read returned fewer values than requested; a short read is not evidence",
                detail={"expected": len(indices), "read": len(values)},
            )
        return {f"#{index}": float(value) for index, value in zip(indices, values)}

    # ------------------------------------------------------------------
    # Stages
    # ------------------------------------------------------------------

    def _read_live_state(self, resolved: ResolvedRequest) -> P.LiveState:
        if not self.state_stage:
            # No document channel is in scope (the provider declares none): the
            # state chunk is not read, not decoded and not claimed. `read_cost`
            # records the reason so a cheaper plan cannot read as a fuller one.
            readback = self._read_planned_parameters(resolved)
            return P.LiveState(
                document=None,
                routes=[],
                slice=None,
                revision=None,
                parameters=readback,
                parameter_readback=readback,
            )
        slice_, document = self._read_state()
        self._last_slice = slice_
        self._last_document = document
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
        document_snapshot = None
        if "document" in self.scope.channels or "routes" in self.scope.channels:
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
            state_channel=(live.slice.block_text if live.slice is not None else None),
            parameters=parameters,
            scope=list(self.scope.channels),
        )
        # the channels the transaction tracks for its own writes
        if live.slice is not None:
            self._last_verified["state"] = text_digest(live.slice.block_text)
        self._last_verified["parameters"] = _vector_digest(parameters)
        for channel in _OUTCOME_CHANNELS:
            if channel not in self.scope_channels:
                self._named_gaps.append(
                    {
                        "channel": channel,
                        "reason": "channel_not_in_scope",
                        "detail": (
                            "the provider declares no stage for this channel, so nothing of it was "
                            "captured, written or restored - the outcome does not cover it"
                        ),
                    }
                )
        return StateSnapshot(
            document=document_snapshot,
            routes=routes_part,
            parameters=parameters,
            base_revision=revision,
            manifest={"captured": captured, "not_captured": [], "out_of_scope": [
                channel for channel in _OUTCOME_CHANNELS if channel not in self.scope_channels
            ]},
        )

    def _build_units(
        self, document: Any, resolved: ResolvedRequest
    ) -> tuple[list[StateWriteUnit], list[tuple[Any, ...]]]:
        if not self.state_stage:
            # A state/routes request against a provider with no codec must refuse
            # loudly: `normalize_request` is expected to have refused it already,
            # and this is the defence in depth for a provider that did not.
            if resolved.state or resolved.routes_create or resolved.routes_update or resolved.routes_remove:
                raise WriteOrderViolation(
                    "this provider declares no state stage, so a state/routes request cannot be "
                    "planned against it",
                    detail={"write_order": list(self.write_order), "reason": "no_state_stage"},
                )
            state_units: list[StateWriteUnit] = []
        else:
            changes = P.state_changes(resolved)
            state_plan = self.codec.plan_state_writes(document, changes)
            state_units = list(state_plan.units)
            # A codec may decline a change it cannot land (P38: the sample block whose
            # growth exceeds the measured write budget). Declined changes are NAMED
            # here so the outcome says what was not done and why - never silently.
            for notice in getattr(state_plan, "notices", []) or []:
                self._named_gaps.append(
                    {
                        "channel": "state",
                        "reason": str(notice.get("reason") or "codec_notice"),
                        "path": notice.get("path"),
                        "detail": notice.get("message"),
                        "evidence": {
                            key: value
                            for key, value in notice.items()
                            if key not in {"reason", "path", "message"}
                        },
                    }
                )
        pairs = self._parameter_pairs(resolved)
        self._require_bound_parameters(resolved, pairs)
        declared = tuple(stage for stage in self.write_order if stage != "verify")
        if declared not in (("state",), ("state", "parameters"), ("parameters",)):
            raise WriteOrderViolation(
                "the provider's declared write order is not one the engine enforces",
                detail={
                    "declared": list(self.write_order),
                    "enforced": ["state", "parameters", "verify"],
                },
            )
        if any(unit.kind == "parameters" for unit in state_units):
            raise WriteOrderViolation("state units may not carry the parameter channel")
        self._prevalidate_units(document, state_units)
        return state_units, pairs

    def _carry_intended(self, document: Any, unit: StateWriteUnit) -> Any:
        """The document a unit's write should be built from: the live one, with OUR values.

        Every path this transaction has already written is put back exactly as it was written, and
        the unit's own paths are left alone (the encoder is about to set them). Paths the document
        no longer has are skipped: a rollback or an external removal must not make the carry a
        failure, and the unit's own verification still guards what it writes.
        """
        if not self._intended:
            return document
        own = {str(path) for path in ((unit.payload or {}).get("paths") or {})}
        carry = {
            path: value for path, value in self._intended.items() if path not in own
        }
        if not carry:
            return document
        hook = getattr(self.codec, "apply_intended_values", None)
        if not callable(hook):
            return document
        hook(document, carry)
        return document

    def _record_intended(self, unit: StateWriteUnit) -> None:
        """Remember what this unit wrote, so later units keep the instance on that value."""
        for path, value in ((unit.payload or {}).get("paths") or {}).items():
            self._intended[str(path)] = copy.deepcopy(value)

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
        # The revalidation just read (and decoded) this unit's base state, and the
        # write below is guarded against a chunk that moved since: a second read of
        # the same chunk would buy nothing and cost one host round trip per unit.
        current_slice = self._last_slice
        current_document = self._carry_intended(self._last_document, unit)
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
            if _route_dropped_by_plugin(unit, report):
                # Measured plugin behaviour (P38, 2026-10-17): a modulation PAIR whose
                # source/destination the running build no longer carries (a name from
                # an older preset format) is accepted by the host and then SILENTLY
                # dropped by the plugin on re-serialisation - the slot comes back
                # empty. That is exactly what the plugin's own GUI load does with such
                # a preset, so the layer mirrors it: the write is verified (the drop
                # IS the finding), the pair is named as a gap, and the rest of the
                # transaction continues. Every other non-landing unit still refuses.
                self._named_gaps.append(
                    {
                        "channel": "state",
                        "reason": "route_dropped_by_plugin",
                        "route": (unit.payload or {}).get("route_op"),
                        "detail": (
                            "the write was accepted and the plugin dropped the pair on "
                            "re-serialisation (an unknown source/destination name is skipped "
                            "silently - measured), so the slot is unchanged; this is the same "
                            "outcome the plugin's own preset load produces"
                        ),
                        "mismatched": report.mismatched,
                    }
                )
                self._dropped_routes.append(dict(unit.payload.get("route_op") or {}))
            else:
                raise WriteNotApplied(
                    "the state write was accepted by the host but the read-back does not agree",
                    detail={"unit": unit.order_index, "mismatched": report.mismatched, "space": report.space},
                )
        self._last_slice = readback_slice
        self._last_verified["state"] = text_digest(readback_slice.block_text)
        self._record_intended(unit)
        # a document write can reset ordinary parameters (measured behaviour),
        # so the parameter channel is re-read and its expected digest advanced
        fresh = self._read_full_parameter_vector()
        self._last_vector = fresh
        self._last_verified["parameters"] = _vector_digest(fresh)
        self._tracker.advance_from_verified_readback(
            StateRevision.compute(state_channel=readback_slice.block_text, parameters=fresh)
        )

    def _apply_parameter_batch(self, pairs: list[tuple[Any, ...]]) -> None:
        self.stage = Stage.PARAMETER_WRITE
        self._check_cancel()
        self._revalidate()
        if self.index_addressed:
            sent = {f"#{int(pair[0])}": float(pair[2]) for pair in pairs}
        else:
            sent = {str(ident): float(value) for ident, value in pairs}
        self._attempted["parameters"].add(_vector_digest({**self._last_vector, **sent}))

        if self.index_addressed:
            # ONE hop: the live name at each index is re-read and compared before
            # that parameter's setter runs (the artifact measured the index; the
            # hop proves the index still addresses the same control).
            attempt = self._issue_write(lambda: self.bridge.write_params_by_index(self.ref, pairs))
            self._display_rows = self._display_check(attempt, pairs, sent)
        else:
            self._issue_write(lambda: self.bridge.write_params(self.ref, pairs))
        self._check_cancel(write_issued=True)

        self.stage = Stage.VERIFY
        # ONE fresh post-write read serves BOTH the read-back comparison and the
        # revision basis. A separate second read would observe the same state
        # (no write happens between them) while opening a window where an
        # external change lands after the comparison but before the digest --
        # merged, the row the caller verifies IS the state the tracker advances
        # from, so the two can never describe different moments.
        readback = (
            self._read_planned_vector()
            if self.index_addressed
            else self._read_full_parameter_vector()
        )
        rows, mismatched = self._verify_parameters(sent, readback)
        self._rows["parameters"].extend(rows)
        self._mismatched["parameters"].extend(mismatched)
        self._establish_effect()
        if mismatched:
            raise ReadbackMismatch(
                "the parameter read-back does not agree in the space that was written",
                detail={"mismatched": mismatched, "space": "normalized"},
            )
        full = readback
        self._last_vector = full
        self._last_verified["parameters"] = _vector_digest(full)
        if self._last_slice is not None:
            self._tracker.advance_from_verified_readback(
                StateRevision.compute(state_channel=self._last_slice.block_text, parameters=full)
            )
        elif self.index_addressed:
            self._tracker.advance_from_verified_readback(
                StateRevision.compute(
                    state_channel=None, parameters=full, scope=list(self.scope.channels)
                )
            )

    # ------------------------------------------------------------------
    # Validation / revalidation
    # ------------------------------------------------------------------

    def _display_check(
        self,
        attempt: Any,
        pairs: Sequence[tuple[Any, ...]],
        sent: Mapping[str, float],
    ) -> list[dict[str, Any]]:
        """Compare the plugin's own displayed text with the law's prediction.

        The authoritative comparison stays in normalized space (that is the space
        that was written). This is a **second, independent channel**: the host
        formatter prints what the plugin displays for the value it now holds, so
        a normalised agreement whose *display* disagrees means the law and the
        plugin disagree about the value between the sampled grid points - a
        finding the normalized comparison cannot see (the P19 lesson: a read-back
        through the same law can never detect a law that is wrong by half a
        printed step). It is reported, never silently absorbed, and it does not
        fail the write: `parameter.normalized` is the contract.
        """
        returned = getattr(attempt, "returned", None) or {}
        rows_by_index = {
            int(row.get("index")): row for row in (returned.get("rows") or []) if isinstance(row, Mapping)
        }
        requested = self._requested_display()
        checks: list[dict[str, Any]] = []
        for index, _expect_name, _value in pairs:
            key = f"#{int(index)}"
            row = rows_by_index.get(int(index)) or {}
            control = self._control_for_index(int(index)) or {}
            law_kind = str((control.get("law") or {}).get("kind") or "none")
            predicted: Any = None
            if law_kind not in ("none", "unmeasured"):
                try:
                    native = V.normalized_to_native(control, sent[key])
                    predicted = V.from_native(control, native)
                except PluginError:
                    predicted = None
            observed = row.get("read_back_display")
            agrees = None
            if observed is not None and predicted is not None:
                agrees = _display_texts_agree(predicted, observed, unit=control.get("unit"))
            # What the CALLER asked for, when the request stated a display value: it
            # is the claim a human made, and the plugin's own text is what decides
            # whether it was honoured. Measured live (P29): a labelled state of a
            # numeric control resolves to a *position* whose law predicts a
            # different number (Surge's "-inf dB" sits where the table law says
            # -72), so the law prediction alone would report a disagreement on a
            # write that landed exactly as requested.
            wanted = requested.get(str(control.get("id")))
            agrees_with_request = None
            if observed is not None and wanted is not None:
                agrees_with_request = _display_texts_agree(wanted, observed, unit=control.get("unit"))
            if agrees_with_request is not None:
                agrees = agrees_with_request
            checks.append(
                {
                    "control": control.get("id"),
                    "index": int(index),
                    "channel": "display",
                    "written_normalized": sent[key],
                    "requested_display": wanted,
                    "law_predicted_display": predicted,
                    "host_display_read_back": observed,
                    "agrees": agrees,
                    "agrees_with_request": agrees_with_request,
                    "agrees_with_law": None
                    if predicted is None or observed is None
                    else _display_texts_agree(predicted, observed, unit=control.get("unit")),
                    "display_claim": (
                        "request_and_host_text_compared"
                        if agrees_with_request is not None
                        else (
                            "law_and_host_text_compared"
                            if predicted is not None and observed is not None
                            else ("host_text_only" if observed is not None else "unavailable")
                        )
                    ),
                    "note": (
                        "`agrees` compares the plugin's own text with what the CALLER asked for when "
                        "the request stated a display value; `agrees_with_law` is the law's opinion, "
                        "which can differ inside the printed resolution of a sampled display"
                        if agrees_with_request is not None
                        else (
                            "the law and the host formatter can differ inside the printed "
                            "resolution of a sampled display; the normalized comparison is what "
                            "the write is verified against"
                            if predicted is not None
                            else (
                                f"this control declares law {law_kind!r}, so no display value could "
                                "be predicted; the text above is the plugin's OWN reading of the "
                                "value it now holds, reported without a comparison"
                            )
                        )
                    ),
                }
            )
        return checks

    def _requested_display(self) -> dict[str, Any]:
        """The display value each resolved control was asked for, by control id.

        Only controls whose request was stated in **display** space carry one: an
        explicit `{"normalized": p}` request names no display value, so there is
        nothing to compare the plugin's text against (its text is still reported).
        """
        wanted: dict[str, Any] = {}
        for row in (self._resolved.controls.values() if self._resolved else []):
            if row.get("channel") != "parameter" or row.get("value_space") != "display":
                continue
            display = row.get("display")
            if display is None:
                continue
            control_id = (row.get("control") or {}).get("id")
            if control_id is not None:
                wanted[str(control_id)] = display
        return wanted
    def _fresh_facts(self):
        return self._read(self.bridge.fx_facts, self.ref.location["track_index"], self.ref.location["fx_index"])

    def _revalidate_target(self) -> None:
        facts = self._fresh_facts()
        self.registry.revalidate(self.token.id, facts)  # TargetChanged, never re-targets
        if self._ident_count is not None and facts.param_count != self._ident_count:
            # a different parameter count means a different table: drop the cache
            # rather than pair cached idents with live values
            self._ident_table = None
            self._ident_count = None
        self._facts = facts

    def _revalidate(self) -> None:
        """Token + expected revision before every write unit / parameter batch."""
        self._revalidate_target()
        if not self.state_stage:
            vector = self._read_planned_vector()
            self._last_vector = vector
            observed = StateRevision.compute(
                state_channel=None, parameters=vector, scope=list(self.scope.channels)
            )
            self._tracker.check(observed)  # StateConflict before that unit's write
            return
        slice_, document = self._read_state()
        vector = self._read_full_parameter_vector()
        self._last_slice = slice_
        self._last_document = document
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

        **One exception, and only one**: a verb that certifies it issued no
        setter call (`detail['writes_issued'] == 0` - the index-addressed verb
        checks every name before its first setter) has a KNOWN effect: nothing
        changed. Counting it as an attempt whose effect is unknown would make the
        layer refuse to say what it knows, and would trigger a restore of
        parameters that were never touched, so the counters are rolled back and
        the refusal is reported as "before any write".
        """
        self.host.write_calls_attempted += 1
        self.host.write_effects_unknown += 1
        try:
            return call()
        except PluginError as exc:
            if exc.detail.get("writes_issued") == 0:
                self.host.write_calls_attempted -= 1
                self.host.write_effects_unknown -= 1
            raise

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
        facts = self._facts
        if facts is None:
            facts = self._read(
                self.bridge.fx_facts, self.ref.location["track_index"], self.ref.location["fx_index"]
            )
            self._facts = facts
        return facts.param_count

    def _parameter_idents(self) -> list[str]:
        """The instance's index -> ident table, measured ONCE per transaction.

        Measured live 2026-10-17 (P38): one ident sweep of the 2858-parameter
        instance costs **3.03 s** (`TrackFX_GetParamIdent` is ~1.06 ms per index)
        while the value read of the same size costs 0.46 s. The transaction reads
        the full vector around every state write, so the ident sweep - not the
        values - dominated a preset apply (~6 s of ~8 s per unit).

        The table is a property of the loaded instance, and the cache is bounded by
        the same guard the rest of the engine uses: the target is re-validated
        before every unit (`TargetChanged` aborts), and a host-reported parameter
        count that differs from the cached one drops the table instead of pairing
        stale idents with live values.
        """
        count = self._param_count()
        if count is None:
            raise SnapshotUnavailable(
                "the host does not report a parameter count", detail={"field": "param_count"}
            )
        if self._ident_table is not None and self._ident_count == count:
            return self._ident_table
        indices = list(range(count))
        idents = self._read(self.bridge.param_idents, self.ref, indices)
        if any(ident is None for ident in idents):
            raise SnapshotUnavailable(
                "a parameter has no durable ident; the vector cannot be captured",
                detail={
                    "missing": [index for index, ident in zip(indices, idents) if ident is None]
                },
            )
        self._ident_table = [str(ident) for ident in idents]
        self._ident_count = count
        return self._ident_table

    def _read_full_parameter_vector(self) -> dict[str, float]:
        if self.index_addressed:
            # The index path's capture scope is the planned set (see `capture_scope`),
            # so "the vector" means that set - read in one hop.
            return self._read_planned_vector()
        idents = self._parameter_idents()
        indices = list(range(len(idents)))
        values = self._read(self.bridge.read_params, self.ref, indices)
        if len(values) != len(indices):
            raise SnapshotUnavailable(
                "the parameter read returned fewer values than requested; a short read is not evidence",
                detail={"expected": len(indices), "read": len(values)},
            )
        return {str(ident): float(value) for ident, value in zip(idents, values)}

    def _indices_for(self, idents: Sequence[str]) -> list[int]:
        """Index lookup resolved **at use time**, one host call per ident.

        The host answers the ident's current index directly (the same rule the
        write hop uses inside its own hop), instead of reading the whole ident
        table to build a map first: measured live 2026-10-13, one ident sweep
        costs ~1 ms per index (~2.8 s over a 2858-parameter table) and every
        lookup paid for it; the direct answer cannot disagree with the write
        hop's own resolution either. An absent ident refuses exactly as the
        sweep-based lookup did.
        """
        if not idents:
            return []
        resolved = self._read(
            self.bridge.param_index_from_idents, self.ref, [str(ident) for ident in idents]
        )
        if len(resolved) != len(idents):
            # the same rule every sibling read enforces: a short answer is not
            # evidence, and a truncated map would silently drop planned params
            raise SnapshotUnavailable(
                "the ident-to-index resolution returned fewer answers than asked; "
                "a short read is not evidence",
                detail={"expected": len(idents), "read": len(resolved)},
            )
        missing = [str(ident) for ident, index in zip(idents, resolved) if index is None]
        if missing:
            raise SnapshotUnavailable(
                "the parameter ident is not present in the live parameter table", detail={"missing": missing}
            )
        return [int(index) for index in resolved]

    def _read_planned_parameters(self, resolved: ResolvedRequest) -> dict[str, float]:
        if self.index_addressed:
            indices = self._planned_indices()
            if not indices:
                return {}
            values = self._read(self.bridge.read_params, self.ref, indices)
            return {f"#{index}": float(value) for index, value in zip(indices, values)}
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
        by_key: dict[str, dict[str, Any]] = {}
        for row in self._resolved.controls.values() if self._resolved else []:
            if row.get("channel") != "parameter":
                continue
            key = self._parameter_key(dict(row.get("binding") or {}))
            if key is not None:
                by_key[key] = row["control"]
        rows: list[dict[str, Any]] = []
        mismatched: list[dict[str, Any]] = []
        for key, value in sent.items():
            control = by_key.get(key)
            actual = readback.get(key)
            row: dict[str, Any] = {
                # `ident` is the historical field name of "the address this value
                # was written to"; the index path writes `#<index>` there and adds
                # the index as its own field, so neither reading is ambiguous.
                "ident": key,
                "control": (control or {}).get("id"),
                "written": value,
                "read_back": actual,
                "channel": "parameter",
            }
            if self.index_addressed:
                row["address"] = {"index": int(key.lstrip("#")), "space": "index"}
            if actual is None:
                row.update({"agrees": False, "space": "normalized", "reason": "unreadable"})
            else:
                # compare in the space that was written; display is re-resolved
                # through the same conversion the write used
                if control is not None and V.has_display_law(control):
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
                    row["display_claim"] = {"available": True, "reason": None}
                else:
                    # No verified law: the value is only meaningful in the host's own
                    # space. Saying nothing about a display is the honest report - a
                    # number here would be the normalized value wearing a display's
                    # field name (the P29 normalized-write case).
                    row["agrees"] = abs(float(value) - float(actual)) <= tolerance
                    row["space"] = "normalized"
                    row["display_claim"] = {
                        "available": False,
                        "reason": (
                            "no_control"
                            if control is None
                            else f"no_verified_law ({str((control.get('law') or {}).get('kind') or 'none')})"
                        ),
                        "note": (
                            "the normalized comparison is the contract; the plugin's own text for "
                            "the value it now holds is available from plugin_read as "
                            "`host_display`"
                        ),
                    }
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
        # A rollback is a chain of writes of its own and can run for a minute: the log
        # carries its boundaries so a failed call's timeline is readable end to end.
        rollback_started = time.perf_counter()
        L.event("rollback.start", label=self.label, cause=exc.code, stage=str(failure_stage.value))
        failed_loudly = False
        state_ok = False
        parameters_ok = False
        # Restorability is decided for BOTH channels BEFORE any rollback write
        # (Decision 3): the transaction's own subsequent verified writes are
        # then attributed to itself, while a channel that was already foreign is
        # never laundered into "mine" by a sibling restore.
        allowed: dict[str, bool] = {}
        for channel in self.scope_channels:
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
            state_ok = self._restore_state_channel(allowed=bool(allowed.get("state")))
        except _TRANSPORT_LOSSES as restore_exc:
            failed_loudly = True
            self._named_gaps.append(_gap("state", f"{restore_exc.code}: {restore_exc.message}"))
        except PluginError as restore_exc:
            failed_loudly = True
            self._named_gaps.append(_gap("state", f"{restore_exc.code}: {restore_exc.message}"))
        try:
            parameters_ok = self._restore_parameter_channel(allowed=bool(allowed.get("parameters")))
        except _TRANSPORT_LOSSES as restore_exc:
            failed_loudly = True
            self._named_gaps.append(_gap("parameters", f"{restore_exc.code}: {restore_exc.message}"))
        except PluginError as restore_exc:
            failed_loudly = True
            self._named_gaps.append(_gap("parameters", f"{restore_exc.code}: {restore_exc.message}"))

        restored = {"state": state_ok, "parameters": parameters_ok}
        L.event(
            "rollback.end",
            label=self.label,
            seconds=round(time.perf_counter() - rollback_started, 3),
            channels=restored,
            loud=failed_loudly,
        )
        # Only the channels this provider declared are judged: a channel that was
        # never in scope contributes neither to `verified` nor to the verdict -
        # and it is named in the outcome instead (see `_assemble_snapshot`).
        every_ok = all(restored[name] for name in self.scope_channels)
        self._rollback.channels = {name: restored[name] for name in self.scope_channels}
        self._rollback.verified = every_ok
        # `verified` says the restore's own writes landed; `complete` says it
        # covered everything that was captured. A codec-declared derived leaf is
        # excluded on purpose, so the two must not be conflated (Decision 2/5).
        self._rollback.complete = self._rollback.verified and not self._rollback.excluded
        if failed_loudly:
            verdict = Verdict.ROLLBACK_FAILED
        elif self._rollback.complete:
            verdict = Verdict.FAILED_AND_RESTORED
        elif every_ok:
            # every channel this restore wrote is verified, but the captured set
            # was not fully restorable: reporting `FAILED_AND_RESTORED` here would
            # claim `unchanged_verified` for state that is not unchanged
            verdict = Verdict.FAILED_AND_PARTIALLY_RESTORED
        elif any(restored[name] for name in self.scope_channels):
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
            observed = _vector_digest(self._read_planned_vector() if self.index_addressed else self._read_full_parameter_vector())
        if observed == self._last_verified.get(channel):
            return True
        return observed in self._attempted.get(channel, set())

    def _restore_state_channel(self, *, allowed: bool) -> bool:
        if not self.state_stage:
            # The channel was never captured (no codec on this provider): there is
            # nothing to restore and nothing to claim about it.
            return False
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
            live = self._read_planned_vector() if self.index_addressed else self._read_full_parameter_vector()
            return not live
        if not allowed:
            return False
        self._revalidate_target()
        tolerance = float(self.policy.tolerances.get("parameter.normalized", 1e-03))
        for start in range(0, len(captured), 256):
            batch = list(captured.items())[start : start + 256]
            if self.index_addressed:
                pairs: list[tuple[Any, ...]] = []
                for key, value in batch:
                    index = int(str(key).lstrip("#"))
                    pairs.append((index, self._live_name_for(index), float(value)))
                write = lambda: self.bridge.write_params_by_index(self.ref, pairs)  # noqa: E731
            else:
                pairs = [(str(key), float(value)) for key, value in batch]
                write = lambda: self.bridge.write_params(self.ref, pairs)  # noqa: E731
            self._attempted["parameters"].add(
                _vector_digest({**self._last_vector, **{key: value for key, value in batch}})
            )
            self._issue_write(write)
            readback = self._read_planned_vector() if self.index_addressed else self._read_full_parameter_vector()
            mismatched: list[dict[str, Any]] = []
            restore_rows: list[dict[str, Any]] = []
            for key, value in batch:
                actual = readback.get(key)
                agrees = actual is not None and abs(float(actual) - float(value)) <= tolerance
                restore_rows.append(
                    {
                        "ident": key,
                        "written": value,
                        "read_back": actual,
                        "channel": "parameter",
                        "restore": True,
                        "agrees": agrees,
                    }
                )
                if not agrees:
                    mismatched.append({"ident": key, "written": value, "read_back": actual})
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

    def _live_name_for(self, index: int) -> str:
        """The name this transaction expects at an index (the name it verified).

        The write hop refuses a mismatch, so a restore is guarded by the same
        check as the write that preceded it - a name that changed since the
        capture stops the restore instead of writing to whatever moved there.
        """
        control = self._control_for_index(index)
        binding = dict(((control or {}).get("binding") or {}))
        return str(binding.get("live_name") or binding.get("measured_name") or "")

    def _control_for_index(self, index: int) -> dict[str, Any] | None:
        for control in self.profile.get("controls") or []:
            if int((control.get("binding") or {}).get("index") or -1) == int(index):
                return dict(control)
        return None

    def _stored_display_for(self, index: int, normalized: float) -> Any:
        """What the plugin will hold for a normalized target, through the law.

        A grid law snaps, so the value a caller asks for and the value the plugin
        stores can differ (e.g. 3.4 -> 3 on an integer ladder). The preview says
        so; the verification row carries the same number after the write.
        """
        control = self._control_for_index(index)
        if control is None:
            return None
        law = str((control.get("law") or {}).get("kind") or "none")
        if law in ("none", "unmeasured"):
            return None
        try:
            return V.from_native(control, V.normalized_to_native(control, float(normalized)))
        except PluginError:
            return None

    def _value_space_for(self, index: int) -> str:
        """The space the request named for this index (what the preview is stated in)."""
        for row in (self._resolved.controls.values() if self._resolved else []):
            binding = dict(row.get("binding") or {})
            if int(binding.get("index") or -1) == int(index):
                return str(row.get("value_space") or "display")
        control = self._control_for_index(index)
        return "display" if control is not None and V.has_display_law(control) else "normalized"

    # ------------------------------------------------------------------
    # Result assembly
    # ------------------------------------------------------------------

    def _preview(self, live: P.LiveState, resolved: ResolvedRequest) -> dict[str, Any]:
        changes = (
            P.state_changes(resolved)
            if self.state_stage
            else {"paths": {}, "routes_create": [], "routes_update": [], "routes_remove": []}
        )
        return {
            "state_paths": changes["paths"],
            "routes_create": changes["routes_create"],
            "routes_update": changes["routes_update"],
            "routes_remove": changes["routes_remove"],
            "wavetable": changes.get("wavetable") or [],
            # The ident path's shape is untouched; the index path writes an
            # address that is a *pair* (index + the name the hop must find there),
            # so it is reported as rows instead of a bare pair.
            "parameters": (
                [
                    {
                        "index": int(index),
                        "expect_name": name,
                        "normalized": value,
                        # what the plugin will actually HOLD: a grid law snaps a
                        # requested value, and the preview must show that, not only
                        # what was asked for. `None` with a reason when no verified
                        # law exists - never the normalized number dressed as a
                        # display value.
                        "stored_display": self._stored_display_for(int(index), value),
                        "value_space": self._value_space_for(int(index)),
                    }
                    for index, name, value in self._parameter_pairs(resolved)
                ]
                if self.index_addressed
                else P.parameter_pairs(resolved)
            ),
            "parameter_address_space": "index" if self.index_addressed else "ident",
        }

    def _plan_summary(self, plan: Plan | None, resolved: ResolvedRequest) -> dict[str, Any]:
        return {
            "label": plan.label if plan else self.label,
            "n_controls": len(resolved.controls),
            "n_routes_create": len(resolved.routes_create),
            "n_routes_update": len(resolved.routes_update),
            "n_routes_remove": len(resolved.routes_remove),
            "n_wavetables": len(resolved.wavetable),
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
                for name in self.scope_channels
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
        # Which space each resolved control was written in (P29): a batch may mix a
        # display request (a confirmed conversion) with an explicit normalized one
        # (no law needed), and the caller must be able to see which is which.
        spaces: dict[str, int] = {}
        for row in (self._resolved.controls.values() if self._resolved else []):
            space = str(row.get("value_space") or "display")
            spaces[space] = spaces.get(space, 0) + 1
        if spaces:
            payload.setdefault("value_spaces", spaces)
        if self.refused:
            # `applied` must come from what was VERIFIED, never from the resolved set: on
            # a batch that failed and rolled back, the resolved count would report
            # controls as applied that no longer hold their value (an independent review
            # of P29 caught exactly that). The verified count is read off the parameter
            # channel's own rows.
            verified = sum(
                1
                for row in self._rows.get("parameters") or []
                if row.get("agrees") is True and not row.get("restore")
            )
            payload.setdefault(
                "partial_application",
                {
                    "on_unresolved": self.on_unresolved,
                    "resolved": len(self._resolved.controls) if self._resolved else 0,
                    "verified": verified,
                    "refused": len(self.refused),
                    "note": (
                        "`on_unresolved: 'skip'` was requested: the controls below were refused "
                        "before any host work and are untouched; `resolved` were planned and "
                        "`verified` of them read back as written"
                    ),
                },
            )
        if self._display_rows:
            # A second, independent channel beside the normalized comparison: what
            # the plugin *displays* for the values it now holds, next to what the
            # law predicted. Reported on every write that produced them.
            disagreements = [row for row in self._display_rows if row.get("agrees") is False]
            law_disagreements = [
                row for row in self._display_rows if row.get("agrees_with_law") is False
            ]
            request_rows = [row for row in self._display_rows if row.get("agrees_with_request") is not None]
            payload.setdefault(
                "display_checks",
                {
                    "rows": list(self._display_rows),
                    "n_rows": len(self._display_rows),
                    "n_disagreements": len(disagreements),
                    # A request-space agreement can now override the law's opinion (the
                    # labelled-state case), so the law's OWN tally is reported separately
                    # - a headline that hid it read as "the law agreed everywhere" while
                    # rows said otherwise (found by the P29 review).
                    "n_law_disagreements": len(law_disagreements),
                    "n_compared_with_request": len(request_rows),
                    "note": (
                        "display agreement is informative, not the contract: the write is "
                        "verified in the space it was written (normalized). `n_disagreements` "
                        "counts rows whose `agrees` is false (the plugin's text vs what the "
                        "caller asked for where a display value was requested, else vs the "
                        "law's prediction); `n_law_disagreements` counts the rows where the "
                        "LAW's prediction differs from the plugin's text, which is a different "
                        "statement and can be non-zero while every request was honoured"
                    ),
                },
            )
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


def _display_texts_agree(predicted: Any, observed: Any, *, unit: Any = None) -> bool:
    """Whether a law's predicted display and the host's text are the same value.

    The two sides are not the same *kind* of thing: the law predicts a number (or
    an enum member id) while the host answers a formatted string ("100.00 Hz").
    So both sides go through the same display parser the discovery engine uses:
    two numeric texts agree when their numbers agree to 1e-4 relative **and** the
    unit the host printed is the unit the control declares (a text with no unit,
    or a declaration with none, cannot disagree about it). Anything else must be
    string-equal.
    """
    from .discovery.text import parse_display

    left, right = parse_display(predicted), parse_display(observed)
    if left.numeric and right.numeric and left.value is not None and right.value is not None:
        host_unit = (right.unit_token or "").strip().casefold()
        declared = ("" if unit is None else str(unit)).strip().casefold()
        if host_unit and declared and host_unit != declared:
            return False
        if left.value == right.value:
            return True
        scale = max(abs(left.value), abs(right.value), 1e-9)
        return abs(left.value - right.value) / scale <= 1e-4
    return str(predicted) == str(observed)


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


def _route_dropped_by_plugin(unit: Any, report: Any) -> bool:
    """True when a route operation failed verification because the PLUGIN skipped it.

    Measured behaviour (P38, 2026-10-17): a modulation pair whose source or
    destination the running build no longer knows is accepted by the host and then
    dropped on re-serialisation, so the slot reads back **completely empty** - no
    source, no destination, and the op's own fields back at their defaults. The
    plugin's own preset load does exactly the same with such a preset, so the
    layer must mirror it rather than roll the whole transaction back.

    The shape is what makes this decidable, and it is deliberately narrow:

    * the unit is a route op (only a route unit's rows are pair rows);
    * every row belongs to a resolved slot AND every pair field reads back empty -
      a single field that landed is a REAL verification failure, not a drop;
    * the unresolved-slot row (no field at all) is not a drop either.

    Anything else keeps the historical refusal + rollback path.
    """
    route_op = (unit.payload or {}).get("route_op")
    if not isinstance(route_op, dict) or route_op.get("op") not in ("create", "update"):
        return False
    if not report.mismatched:
        return False
    pair_absent = False
    for row in report.rows:
        subject = row.get("subject")
        if not isinstance(subject, dict) or subject.get("route") is None:
            return False
        field = row.get("field")
        if field in ("source", "destination"):
            if row.get("read_back") not in ("", None):
                # part of the pair landed: the plugin did NOT drop it whole
                return False
            pair_absent = True
        elif field is None:
            # the "slot present" probe row - the slot itself is not addressable
            return False
    return pair_absent


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


def _workload(resolved: ResolvedRequest) -> int:
    """How much work a resolved request actually asks for, across all channels.

    Used by the `skip` policy to refuse a batch that resolves to *nothing*: a
    request whose every entry was refused is a refusal, not an empty success.
    """
    return (
        len(resolved.controls)
        + (1 if resolved.state else 0)
        + len(resolved.routes_create)
        + len(resolved.routes_update)
        + len(resolved.routes_remove)
    )


def _problem_error(problems: list[dict[str, Any]]) -> PluginError:
    first = problems[0]
    code = str(first.get("code") or "InvalidValue")
    message = str(first.get("message") or "request refused")
    error = error_from_code(code, message, {"problems": problems})
    if isinstance(error, InvalidValue) and len(problems) == 1:
        error.detail.setdefault("problem", first)
    return error
