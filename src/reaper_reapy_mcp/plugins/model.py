"""Domain objects of the Universal Core (contract 05 §2, §2.8, §2.9).

P0 status: DECLARATIVE. Every object below is a plain dataclass carrying the
fields of the normative tables, with the names of the documents kept
identical. Behaviour (validation, invariant helpers, serialization, revision
advance rules) is added in P1 per docs/universal-plugin-mcp-v3/14-implementation-plan.md.

Boundary rule (08 §8): this module contains no provider identifiers and imports
no host module.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Sequence

__all__ = [
    "Stage",
    "Verdict",
    "FinalState",
    "ChannelName",
    "VERDICT_FINAL_STATE",
    "REFUSAL_STAGES",
    "utc_now_iso",
    "text_digest",
    "canonical_json",
    "final_state_for",
    "PluginInstanceRef",
    "RangeSpec",
    "Authority",
    "Law",
    "Evidence",
    "ControlCapability",
    "ModuleDescriptor",
    "Value",
    "RouteRef",
    "RouteSpec",
    "Finding",
    "PlanItem",
    "Plan",
    "RollbackReport",
    "ChannelReport",
    "HostAccounting",
    "Outcome",
    "TrackView",
    "FxHostFacts",
    "TargetedStateSlice",
    "StateRevision",
    "StateSnapshot",
    "StateWriteUnit",
    "StateWritePlan",
    "WriteAttempt",
    "VerificationReport",
    "VerificationPolicy",
    "CaptureScope",
    "TimingProfile",
    "ResolvedRequest",
    "RevisionTracker",
]


def utc_now_iso() -> str:
    """UTC timestamp used on revisions and write attempts (never local time)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def text_digest(text: str | bytes) -> str:
    """sha256 hex over text/bytes; the binding between a slice and its chunk."""
    payload = text.encode("utf-8") if isinstance(text, str) else bytes(text)
    return hashlib.sha256(payload).hexdigest()


def canonical_json(value: Any) -> str:
    """Deterministic JSON used for every digest in this layer."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def final_state_for(verdict: Verdict) -> FinalState:
    """The single final_state a verdict may carry (11 §3)."""
    return VERDICT_FINAL_STATE[verdict]


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------


class Stage(str, Enum):
    """Where an operation ended (05 §2.8)."""

    PREFLIGHT = "preflight"
    PLAN = "plan"
    SNAPSHOT = "snapshot"
    STATE_WRITE = "state_write"
    PARAMETER_WRITE = "parameter_write"
    VERIFY = "verify"
    COMMIT = "commit"
    ROLLBACK = "rollback"


class Verdict(str, Enum):
    """The eight terminal verdicts (05 §2.8, 11 §3)."""

    ALL_VERIFIED = "ALL_VERIFIED"
    REFUSED_BEFORE_ANY_WRITE = "REFUSED_BEFORE_ANY_WRITE"
    REFUSED_AFTER_A_WRITE = "REFUSED_AFTER_A_WRITE"
    FAILED_AND_RESTORED = "FAILED_AND_RESTORED"
    FAILED_AND_PARTIALLY_RESTORED = "FAILED_AND_PARTIALLY_RESTORED"
    FAILED_AND_NOT_RESTORED = "FAILED_AND_NOT_RESTORED"
    ROLLBACK_FAILED = "ROLLBACK_FAILED"
    NO_WRITES = "NO_WRITES"


class FinalState(str, Enum):
    """The four honest aggregate states (05 §2.8, 11 §3)."""

    COMMITTED = "committed"
    UNCHANGED_VERIFIED = "unchanged_verified"
    PARTIALLY_RESTORED = "partially_restored"
    UNKNOWN = "unknown"


#: Normative verdict -> final_state mapping (11 §3). Total and single-valued;
#: the mapping is test-enforced. ``REFUSED_AFTER_A_WRITE`` is declared for
#: op-stream compatibility and is NOT reachable in V1 single-request
#: transactions (its mapping covers the "rollback verified" case; a failed
#: compensation is reported through the FAILED_*/ROLLBACK_FAILED verdicts).
VERDICT_FINAL_STATE: dict[Verdict, FinalState] = {
    Verdict.ALL_VERIFIED: FinalState.COMMITTED,
    Verdict.REFUSED_BEFORE_ANY_WRITE: FinalState.UNCHANGED_VERIFIED,
    Verdict.REFUSED_AFTER_A_WRITE: FinalState.UNCHANGED_VERIFIED,
    Verdict.FAILED_AND_RESTORED: FinalState.UNCHANGED_VERIFIED,
    Verdict.FAILED_AND_PARTIALLY_RESTORED: FinalState.PARTIALLY_RESTORED,
    Verdict.FAILED_AND_NOT_RESTORED: FinalState.UNKNOWN,
    Verdict.ROLLBACK_FAILED: FinalState.UNKNOWN,
    Verdict.NO_WRITES: FinalState.UNCHANGED_VERIFIED,
}


class ChannelName(str, Enum):
    """Channel names used in rollback/outcome reports (05 §2.8)."""

    STATE = "state"
    PARAMETERS = "parameters"


#: Stages at which a refusal cannot have been preceded by any write.
REFUSAL_STAGES: tuple[Stage, ...] = (Stage.PREFLIGHT, Stage.PLAN, Stage.SNAPSHOT)


# ---------------------------------------------------------------------------
# 05 §2.1 - §2.7 domain objects
# ---------------------------------------------------------------------------


@dataclass
class PluginInstanceRef:
    """A resolved plugin instance (05 §2.1).

    ``location`` is a lookup hint, not an identity; ``identity`` is the
    provider profile fingerprint (durable/cacheable), which does not by itself
    prove same-instance (AUD-003).
    """

    host: str
    location: dict[str, Any]
    provider: str
    identity: dict[str, Any]
    display: str | None = None


@dataclass
class RangeSpec:
    min: float
    max: float


@dataclass
class Authority:
    read: str  # "parameter" | "state"
    write: str  # "parameter" | "state"


@dataclass
class Law:
    kind: str  # affine | enum_index | exponential | identity | none | unmeasured
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class Evidence:
    strength: str  # strong | weak | none
    source: str = ""


@dataclass
class ControlCapability:
    """One semantic control (05 §2.2)."""

    id: str
    label: str
    type: str  # number | enum | bool | state_ref
    authority: Authority
    binding: dict[str, Any]
    module: str | None = None
    unit: str | None = None
    range: RangeSpec | None = None
    step: float | None = None
    enum_values: list[dict[str, Any]] | None = None
    aliases: list[str] | None = None
    law: Law | None = None
    evidence: Evidence | None = None
    discrete: bool | None = None


@dataclass
class ModuleDescriptor:
    id: str
    kind: str
    label: str | None = None
    parent: str | None = None
    controls: list[str] | None = None
    state_refs: list[str] | None = None
    props: dict[str, Any] = field(default_factory=dict)


@dataclass
class Value:
    """A typed value (05 §2.4)."""

    type: str
    display: Any
    native: Any
    normalized: float | None = None
    unit: str | None = None


@dataclass
class RouteRef:
    """A provider-identified route (05 §2.5). Identity is provider-defined."""

    identity: Any
    source: str = ""
    destination: str = ""
    depth: Value | None = None
    amount: float | None = None
    fields: dict[str, Any] = field(default_factory=dict)


@dataclass
class RouteSpec:
    """A requested route (05 §2.5)."""

    source: str
    destination: str
    depth: Any | None = None
    amount: float | None = None
    fields: dict[str, Any] = field(default_factory=dict)


@dataclass
class Finding:
    """A dependency/warning/info row (05 §2.6)."""

    kind: str  # hard | soft | incompatible | warning | info
    rule: str
    subject: dict[str, Any] = field(default_factory=dict)
    message: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    informational: bool = False


@dataclass
class PlanItem:
    subject: dict[str, Any]
    channel: str  # "state" | "parameter"
    from_: Any = None
    to: Any = None
    value_space: str = "display"
    path: str | None = None
    binding: dict[str, Any] | None = None


@dataclass
class Plan:
    """A plan is data; building it performs zero writes (05 §2.7)."""

    label: str
    instance_ref: PluginInstanceRef | None
    profile_key: str
    changes: list[PlanItem] = field(default_factory=list)
    disable_set: dict[str, Any] = field(default_factory=dict)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    write_order: tuple[str, ...] = ()
    n_slots_in_use: int | None = None


@dataclass
class RollbackReport:
    """The recovery state, with `verified` and `complete` kept **separate**.

    ``verified`` - every channel this restore actually wrote was compared after
    the write and matched its captured value. ``complete`` - that set covered
    *everything* that was captured. They differ whenever a codec declares a leaf
    it cannot restore (a plugin re-derived blob): the remaining channels are
    provably back, and the report must not present that as "everything is back"
    (Decision 2/5, 2026-10-10; contract 11 §2.7).
    """

    attempted: bool = False
    verified: bool = False
    #: True only when a restore actually ran and covered the whole capture: a
    #: report that never attempted a restore must not read as "complete".
    complete: bool = False
    channels: dict[str, bool] = field(default_factory=dict)
    excluded: list[str] = field(default_factory=list)
    why: str = ""


@dataclass
class ChannelReport:
    rows: list[dict[str, Any]] = field(default_factory=list)
    mismatched: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class HostAccounting:
    """Host accounting counters (05 §2.8). Normative rules:

    - ``write_calls_attempted`` counts every write verb invoked on the host;
      "zero host writes" may be claimed only when it is 0.
    - ``write_effects_verified`` counts writes whose effect a later read-back
      verified; ``write_effects_unknown`` counts writes whose effect could not
      be established.
    """

    reads: int = 0
    write_calls_attempted: int = 0
    write_effects_verified: int = 0
    write_effects_unknown: int = 0

    @property
    def zero_writes(self) -> bool:
        """The ONLY condition under which "zero host writes" may be claimed."""
        return self.write_calls_attempted == 0

    def as_dict(self) -> dict[str, int]:
        return {
            "reads": self.reads,
            "write_calls_attempted": self.write_calls_attempted,
            "write_effects_verified": self.write_effects_verified,
            "write_effects_unknown": self.write_effects_unknown,
        }


@dataclass
class Outcome:
    """The post-write/recovery report, separate from the error (05 §2.8)."""

    stage: Stage
    verdict: Verdict
    final_state: FinalState
    rollback: RollbackReport = field(default_factory=RollbackReport)
    channels: dict[str, ChannelReport] = field(default_factory=dict)
    host: HostAccounting = field(default_factory=HostAccounting)
    named_gaps: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Envelope-ready shape. Contains no write-target token: the token is
        session-scoped and travels only in the live response's ``meta`` (P1-5)."""
        return {
            "stage": str(self.stage.value if isinstance(self.stage, Stage) else self.stage),
            "verdict": str(self.verdict.value if isinstance(self.verdict, Verdict) else self.verdict),
            "final_state": str(
                self.final_state.value if isinstance(self.final_state, FinalState) else self.final_state
            ),
            "rollback": {
                "attempted": self.rollback.attempted,
                "verified": self.rollback.verified,
                "complete": self.rollback.complete,
                "channels": dict(self.rollback.channels),
                "excluded": list(self.rollback.excluded),
                "why": self.rollback.why,
            },
            "channels": {
                name: {"rows": list(report.rows), "mismatched": list(report.mismatched)}
                for name, report in self.channels.items()
            },
            "host": self.host.as_dict(),
            "named_gaps": list(self.named_gaps),
        }

    def mapping_is_consistent(self) -> bool:
        """The declared verdict -> final_state mapping holds (11 §3)."""
        expected = final_state_for(self.verdict) if isinstance(self.verdict, Verdict) else None
        return expected is not None and self.final_state == expected


# ---------------------------------------------------------------------------
# 05 §2.9 transaction, host and state objects
# ---------------------------------------------------------------------------


@dataclass
class TrackView:
    """Minimal enumeration row returned by HostBridge.list_tracks()."""

    track_index: int
    name: str
    n_fx: int = 0


@dataclass
class FxHostFacts:
    """Fresh host facts for one FX slot (05 §2.9).

    Read fresh for every validation; never treated as durable identity. A GUID
    is session evidence only and is never serialized (05 §5).
    """

    track_index: int
    fx_index: int
    fx_name: str
    class_id: str | None = None
    module: str | None = None
    param_count: int | None = None
    version: str | None = None
    track_name: str | None = None
    fx_count: int | None = None
    session_handle: str | None = None  # session evidence, never serialized


@dataclass
class TargetedStateSlice:
    """The exact state block span uniquely correlated to the target (05 §2.9).

    The span is only valid for its ``chunk_digest``; a stale slice is
    re-located, never reused. ``framing`` is provider framing parsed from the
    block text; it is filled by the provider codec (P5/P7) and stays ``None``
    while only the read bridge exists.
    """

    chunk_digest: str
    line_start: int
    line_end: int
    header_line: str
    class_hex: str | None
    ordinal: int
    block_text: str
    framing: dict[str, Any] | None = None

    def is_valid_for(self, chunk: str) -> bool:
        """The slice may only be used against the chunk it came from."""
        return text_digest(chunk) == self.chunk_digest

    @property
    def block_digest(self) -> str:
        return text_digest(self.block_text)


@dataclass
class StateRevision:
    """Digest over the captured channels, plus scope and read timestamp.

    Component digests are explicit: an absent channel contributes a ``none``
    marker, never an empty-string hash (an empty set hashes like an empty set -
    that is not evidence of anything).
    """

    digest: str
    scope: tuple[str, ...]
    read_utc: str

    @classmethod
    def compute(
        cls,
        *,
        state_channel: str | bytes | None,
        parameters: Mapping[str, float] | None,
        scope: Sequence[str] | None = None,
        read_utc: str | None = None,
    ) -> "StateRevision":
        """Digest over (state channel bytes, captured parameter vector).

        The state channel is the located slice's block text: it carries the
        stored document and the route view the host holds for that instance.
        ``None`` means "not captured" and is digested as an explicit marker, so
        a missing channel can never be confused with an empty one.
        """
        if state_channel is None and parameters is None:
            raise ValueError("a revision needs at least one captured channel")
        h = hashlib.sha256()
        h.update(b"state:")
        h.update(b"none" if state_channel is None else bytes.fromhex(text_digest(state_channel)))
        h.update(b"|parameters:")
        if parameters is None:
            h.update(b"none")
        else:
            vector = {str(k): float(v) for k, v in sorted(parameters.items())}
            h.update(bytes.fromhex(text_digest(canonical_json(vector))))
        return cls(
            digest=h.hexdigest(),
            scope=tuple(scope or ("document", "routes", "parameters")),
            read_utc=read_utc or utc_now_iso(),
        )


@dataclass
class RevisionTracker:
    """Expected-revision state for one transaction (19 Decision 3).

    The expected revision advances **only** from the transaction's own verified
    read-back; an unverified read-back is never adopted, or an external change
    would be erased from the model and later overwritten.
    """

    expected: StateRevision | None = None
    last_own_readback: StateRevision | None = None

    def check(self, observed: StateRevision) -> None:
        """Raise ``StateConflict`` when the live state is not what we planned
        against; the detail carries both digests, never prose alone."""
        from .errors import PlanError, StateConflict

        if self.expected is None:
            raise PlanError("revision tracker has no expected revision")
        if observed.digest != self.expected.digest:
            raise StateConflict(
                "expected state revision disagrees with the live state",
                detail={"expected": self.expected.digest, "observed": observed.digest},
            )

    def advance_from_verified_readback(self, readback: StateRevision) -> None:
        self.expected = readback
        self.last_own_readback = readback

    def seed(self, initial: StateRevision) -> None:
        self.expected = initial

    def is_own_verified_write(self, observed: StateRevision) -> bool | None:
        """True when ``observed`` is exactly what the transaction last verified
        writing; ``None`` when nothing comparable has been recorded yet (a
        tri-state, so "no evidence" can never collapse into "no difference")."""
        if self.last_own_readback is None:
            return None
        return observed.digest == self.last_own_readback.digest


@dataclass
class StateSnapshot:
    """Complete-or-refuse capture (05 §2.9, 11 §2.3).

    ``manifest`` reports ``captured`` and ``not_captured``; for writes a
    non-empty ``not_captured`` inside the capture scope is a refusal, not a
    footnote.
    """

    document: Any
    routes: Any
    parameters: dict[str, float] = field(default_factory=dict)
    base_revision: StateRevision | None = None
    manifest: dict[str, Any] = field(default_factory=dict)

    def captured_channels(self) -> list[str]:
        captured = self.manifest.get("captured", [])
        return [str(channel) for channel in captured]

    def missing_channels(self, scope_channels: Sequence[str]) -> list[str]:
        captured = set(self.captured_channels())
        return [channel for channel in scope_channels if channel not in captured]

    def is_complete_for(self, scope_channels: Sequence[str]) -> bool:
        return not self.missing_channels(scope_channels)


@dataclass
class StateWriteUnit:
    """One ordered write unit produced by the codec (05 §2.9)."""

    order_index: int
    kind: str  # "state" | "parameters"
    payload: Any = None
    expected_revision: StateRevision | None = None
    target_token_id: str | None = None
    preconditions: list[str] = field(default_factory=list)


@dataclass
class StateWritePlan:
    """Ordered units + rollback plan + verification plan (05 §2.9)."""

    units: list[StateWriteUnit] = field(default_factory=list)
    rollback_plan: list[dict[str, Any]] = field(default_factory=list)
    verification_plan: dict[str, Any] = field(default_factory=dict)


@dataclass
class WriteAttempt:
    """One write verb invocation, recorded by the transport (05 §2.9).

    ``returned`` is the host value: recorded, never trusted as evidence.
    """

    unit_index: int
    attempted_utc: str
    returned: Any = None
    slice_digest_sent: str | None = None
    readback_slice_digest: str | None = None
    effects_verified: bool | None = None
    error: str | None = None


@dataclass
class VerificationReport:
    """Per-unit verification evidence (05 §2.9).

    Comparison happens in the space that was written; a report without a
    comparator for a written value type is invalid.
    """

    ok: bool
    rows: list[dict[str, Any]] = field(default_factory=list)
    mismatched: list[dict[str, Any]] = field(default_factory=list)
    comparators: dict[str, str] = field(default_factory=dict)
    tolerances: dict[str, float] = field(default_factory=dict)
    space: str = ""


@dataclass
class VerificationPolicy:
    """Provider-declared verification policy (05 §2.9)."""

    tolerances: dict[str, float] = field(default_factory=dict)
    comparators: dict[str, str] = field(default_factory=dict)
    timing: TimingProfile | None = None


@dataclass
class TimingProfile:
    """Provider-declared settle/retry timing, injected in tests (11 §6)."""

    settle_s: float = 0.0
    retry_after_s: float = 0.0
    retry_count: int = 0
    timeout_s: float = 0.0


@dataclass
class CaptureScope:
    """The mutable channels an operation may touch and must snapshot.

    The first provider declares document + routes + the full parameter vector;
    the default is complete capture (11 §2.3).
    """

    channels: list[str] = field(default_factory=lambda: ["document", "routes", "parameters"])
    parameters: dict[str, Any] = field(
        default_factory=lambda: {"scope": "full_vector", "ident_required": True}
    )
    restore_order: list[str] = field(default_factory=lambda: ["document", "parameters"])
    verify_order: list[str] = field(default_factory=lambda: ["document", "parameters"])


@dataclass
class ResolvedRequest:
    """A semantic request resolved against a profile; no host access (08 §2)."""

    controls: dict[str, Any] = field(default_factory=dict)
    structural: bool = False
    routes_create: list[RouteSpec] = field(default_factory=list)
    routes_update: list[dict[str, Any]] = field(default_factory=list)
    routes_remove: list[dict[str, Any]] = field(default_factory=list)
    state: dict[str, Any] = field(default_factory=dict)
    problems: list[dict[str, Any]] = field(default_factory=list)
