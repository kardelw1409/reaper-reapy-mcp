"""The data the discovery stages hand to each other (P26).

Plain dataclasses with explicit ``to_dict``: an artifact written from these is
the same shape a test asserts on, and a stage can be replayed from a decoded
artifact without a second code path. Nothing here knows a plugin, a host call
or a provider.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..discovery.evidence import Conflict

__all__ = [
    "Attempt",
    "StepSizes",
    "ParamRow",
    "Inventory",
    "Sample",
    "ControlObservations",
    "MappingFit",
    "RangeAnalysis",
    "MappingAnalysis",
    "StateBand",
    "Transition",
    "DiscreteAnalysis",
    "Classification",
    "ModuleNode",
    "GroupingResult",
    "ControlResult",
    "RunCost",
    "DiscoveryResult",
]


@dataclass
class Attempt:
    """One try at a metadata channel, with its status and cost."""

    source: str
    status: str
    calls: int = 0
    detail: dict[str, Any] = field(default_factory=dict)
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        row: dict[str, Any] = {"source": self.source, "status": self.status, "calls": int(self.calls)}
        if self.message:
            row["message"] = self.message
        if self.detail:
            row["detail"] = dict(self.detail)
        return row


@dataclass
class StepSizes:
    """``TrackFX_GetParameterStepSizes`` as the host answered it."""

    reported: bool = False
    step: float = 0.0
    small: float = 0.0
    large: float = 0.0
    is_toggle: bool = False
    status: str = "not_attempted"
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "status": self.status,
            "reported": bool(self.reported),
            "is_toggle": bool(self.is_toggle),
        }
        if self.reported:
            row.update({"step": self.step, "small": self.small, "large": self.large})
        if self.note:
            row["note"] = self.note
        return row


@dataclass
class ParamRow:
    """One host parameter, as far as the metadata channels answered."""

    index: int
    name: str | None = None
    ident: str | None = None
    current_normalized: float | None = None
    steps: StepSizes = field(default_factory=StepSizes)
    named_config: dict[str, Any] = field(default_factory=dict)
    section: str | None = None
    native_range: tuple[float, float] | None = None
    normalized_range: tuple[float, float] = (0.0, 1.0)
    attempts: dict[str, Attempt] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    def attempt(self, source: str, status: str, *, calls: int = 0, message: str | None = None, **detail: Any) -> None:
        self.attempts[source] = Attempt(source=source, status=status, calls=calls, message=message, detail=detail)

    def status_of(self, source: str) -> str:
        attempt = self.attempts.get(source)
        return attempt.status if attempt else "not_attempted"

    def to_dict(self) -> dict[str, Any]:
        row: dict[str, Any] = {"index": int(self.index)}
        if self.name is not None:
            row["name"] = self.name
        if self.ident is not None:
            row["ident"] = self.ident
        if self.current_normalized is not None:
            row["current_normalized"] = float(self.current_normalized)
        row["steps"] = self.steps.to_dict()
        if self.named_config:
            row["named_config"] = dict(self.named_config)
        if self.section is not None:
            row["section"] = self.section
        if self.native_range is not None:
            row["native_range"] = list(self.native_range)
        if self.normalized_range != (0.0, 1.0):
            row["normalized_range"] = list(self.normalized_range)
        if self.extra:
            row["extra"] = dict(self.extra)
        row["attempts"] = {key: value.to_dict() for key, value in self.attempts.items()}
        return row


@dataclass
class Inventory:
    """The host's answer to "what parameters exist, and what does it say about them"."""

    identity: dict[str, Any] = field(default_factory=dict)
    param_count: int = 0
    reported_param_count: int | None = None
    params: list[ParamRow] = field(default_factory=list)
    attempts: dict[str, Attempt] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    origin: str = "host"  # host | recorded

    def by_index(self) -> dict[int, ParamRow]:
        return {row.index: row for row in self.params}

    def attempt(self, source: str, status: str, *, calls: int = 0, message: str | None = None, **detail: Any) -> None:
        self.attempts[source] = Attempt(source=source, status=status, calls=calls, message=message, detail=detail)

    @property
    def named(self) -> list[ParamRow]:
        return [row for row in self.params if row.name]

    def to_dict(self, *, with_params: bool = True) -> dict[str, Any]:
        row: dict[str, Any] = {
            "origin": self.origin,
            "identity": dict(self.identity),
            "param_count": int(self.param_count),
        }
        if self.reported_param_count is not None:
            row["reported_param_count"] = int(self.reported_param_count)
        row["attempts"] = {key: value.to_dict() for key, value in self.attempts.items()}
        if self.notes:
            row["notes"] = list(self.notes)
        if with_params:
            row["params"] = [param.to_dict() for param in self.params]
        return row


@dataclass
class Sample:
    """One (normalized position, printed text) observation."""

    normalized: float
    text: str
    calls: int = 1
    origin: str = "host"  # host | recorded

    def to_dict(self) -> dict[str, Any]:
        return {"normalized": float(self.normalized), "text": self.text, "origin": self.origin}


@dataclass
class ControlObservations:
    """Everything one run recorded about one control's *display* behaviour."""

    index: int
    name: str | None = None
    ident: str | None = None
    samples: list[Sample] = field(default_factory=list)
    status: str = "no_samples"
    reason: str | None = None
    calls: int = 0
    origin: str = "host"
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def positions(self) -> list[float]:
        return [sample.normalized for sample in self.samples]

    @property
    def texts(self) -> list[str]:
        return [sample.text for sample in self.samples]

    def sorted_samples(self) -> list[Sample]:
        return sorted(self.samples, key=lambda sample: sample.normalized)

    def to_dict(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "index": int(self.index),
            "status": self.status,
            "calls": int(self.calls),
            "origin": self.origin,
            "samples": [sample.to_dict() for sample in self.sorted_samples()],
        }
        if self.name is not None:
            row["name"] = self.name
        if self.ident is not None:
            row["ident"] = self.ident
        if self.reason is not None:
            row["reason"] = self.reason
        if self.extra:
            row["extra"] = dict(self.extra)
        return row


@dataclass
class MappingFit:
    """One candidate conversion shape, fitted on a training split and scored on
    points it never saw."""

    kind: str
    params: dict[str, Any] = field(default_factory=dict)
    train_error: float | None = None
    test_error: float | None = None
    max_error: float | None = None
    n_train: int = 0
    n_test: int = 0
    monotone: bool | None = None
    complexity: int = 0
    accepted: bool = False
    profile_law_kind: str | None = None
    reason: str | None = None
    #: Worst held-out error as a share of the budget the printed text allows at
    #: that value (``<= 1`` means the fit is inside what the display can resolve).
    budget_ratio: float | None = None

    def to_dict(self) -> dict[str, Any]:
        row: dict[str, Any] = {"kind": self.kind, "accepted": bool(self.accepted), "complexity": int(self.complexity)}
        for key in ("train_error", "test_error", "max_error", "budget_ratio"):
            value = getattr(self, key)
            if value is not None:
                row[key] = float(value)
        row["n_train"] = int(self.n_train)
        row["n_test"] = int(self.n_test)
        if self.monotone is not None:
            row["monotone"] = bool(self.monotone)
        if self.params:
            row["params"] = dict(self.params)
        if self.profile_law_kind:
            row["profile_law_kind"] = self.profile_law_kind
        if self.reason:
            row["reason"] = self.reason
        return row


@dataclass
class RangeAnalysis:
    """The range claims of one control, resolved and explained."""

    claims: list[dict[str, Any]] = field(default_factory=list)
    chosen: list[float] | None = None
    space: str | None = None  # normalized | declared | native | display | sampled | accepted
    conflict: Conflict | None = None
    reason: str | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        row: dict[str, Any] = {"claims": [dict(claim) for claim in self.claims]}
        if self.chosen is not None:
            row["chosen"] = list(self.chosen)
        if self.space:
            row["space"] = self.space
        if self.conflict is not None:
            row["conflict"] = self.conflict.to_dict()
        if self.reason:
            row["reason"] = self.reason
        if self.notes:
            row["notes"] = list(self.notes)
        return row


@dataclass
class MappingAnalysis:
    """The conversion-shape analysis of one numeric control."""

    fits: list[MappingFit] = field(default_factory=list)
    chosen: MappingFit | None = None
    resolution: dict[str, Any] = field(default_factory=dict)
    reason: str | None = None
    ascending: bool | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        row: dict[str, Any] = {"fits": [fit.to_dict() for fit in self.fits]}
        if self.chosen is not None:
            row["chosen"] = self.chosen.to_dict()
        if self.resolution:
            row["resolution"] = dict(self.resolution)
        if self.ascending is not None:
            row["ascending"] = bool(self.ascending)
        if self.reason:
            row["reason"] = self.reason
        if self.notes:
            row["notes"] = list(self.notes)
        return row


@dataclass
class StateBand:
    """One member of a discrete control: which positions display its label."""

    ordinal: int
    label: str
    lower: float
    upper: float
    #: The pair of sampled positions that brackets this band's **UPPER** edge: the
    #: last position that printed this member and the first that printed the next one.
    #: So the transition with ordinal ``k`` (1-based, between bands ``k-1`` and ``k``)
    #: is bracketed by ``members[k-1].bracket``. The last band has none (nothing
    #: follows it). A bracket spanning a whole band would accept almost any position,
    #: which is why the pair is carried instead of the band edges alone.
    bracket: list[float] | None = None
    interior_verified: bool = False
    unlabelled: bool = False
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "ordinal": int(self.ordinal),
            "label": self.label,
            "band": [float(self.lower), float(self.upper)],
            "interior_verified": bool(self.interior_verified),
        }
        if self.bracket is not None:
            row["bracket"] = [float(self.bracket[0]), float(self.bracket[1])]
        if self.unlabelled:
            row["unlabelled"] = True
        if self.note:
            row["note"] = self.note
        return row


@dataclass
class Transition:
    """One measured change between two adjacent members."""

    ordinal: int
    lower_label: str
    upper_label: str
    bracket: list[float] | None = None
    position: float | None = None
    state: str = "not_attempted"  # exact | exact_at | bracketed | unresolved | not_attempted | sampled_upper_bound
    calls: int = 0
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "ordinal": int(self.ordinal),
            "lower_label": self.lower_label,
            "upper_label": self.upper_label,
            "state": self.state,
            "calls": int(self.calls),
        }
        if self.bracket is not None:
            row["bracket"] = [float(self.bracket[0]), float(self.bracket[1])]
        if self.position is not None:
            row["position"] = float(self.position)
        if self.reason:
            row["reason"] = self.reason
        return row


@dataclass
class DiscreteAnalysis:
    """The discrete-state analysis of one control."""

    family: str = "unknown"  # enum | boolean | integer_ladder | ramp | continuous | unknown
    state: str = "not_attempted"  # measured | sampled_upper_bounds | undetermined | no_band_structure | budget_exhausted
    members: list[StateBand] = field(default_factory=list)
    transitions: list[Transition] = field(default_factory=list)
    distinct_count: int = 0
    unlabelled: list[str] = field(default_factory=list)
    budget_exhausted: bool = False
    calls: int = 0
    reason: str | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "family": self.family,
            "state": self.state,
            "distinct_count": int(self.distinct_count),
            "calls": int(self.calls),
            "budget_exhausted": bool(self.budget_exhausted),
            "members": [member.to_dict() for member in self.members],
            "transitions": [item.to_dict() for item in self.transitions],
        }
        if self.unlabelled:
            row["unlabelled"] = list(self.unlabelled)
        if self.reason:
            row["reason"] = self.reason
        if self.notes:
            row["notes"] = list(self.notes)
        return row


@dataclass
class Classification:
    """What kind of control this is, with the reasons that decided it."""

    kind: str = "unknown"  # continuous | integer_like | boolean | enum | ramp_like | stepped | unknown
    confidence: str = "none"
    reasons: list[str] = field(default_factory=list)
    alternatives: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "kind": self.kind,
            "confidence": self.confidence,
            "reasons": list(self.reasons),
        }
        if self.alternatives:
            row["alternatives"] = list(self.alternatives)
        if self.evidence:
            row["evidence"] = dict(self.evidence)
        return row


@dataclass
class ModuleNode:
    """One inferred module (or submodule) of the plugin."""

    id: str
    label: str = ""
    parent: str | None = None
    kind: str = "unknown"
    status: str = "candidate"  # confirmed | inferred | candidate | unknown
    confidence: str = "weak"
    signals: list[str] = field(default_factory=list)
    alternatives: list[dict[str, Any]] = field(default_factory=list)
    controls: list[int] = field(default_factory=list)
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "id": self.id,
            "label": self.label or self.id,
            "kind": self.kind,
            "status": self.status,
            "confidence": self.confidence,
            "controls": list(self.controls),
            "signals": list(self.signals),
        }
        if self.parent is not None:
            row["parent"] = self.parent
        if self.alternatives:
            row["alternatives"] = [dict(item) for item in self.alternatives]
        if self.reason:
            row["reason"] = self.reason
        return row


@dataclass
class GroupingResult:
    """The inferred structure plus what could not be placed."""

    nodes: list[ModuleNode] = field(default_factory=list)
    unassigned: list[int] = field(default_factory=list)
    signals_used: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": [node.to_dict() for node in self.nodes],
            "unassigned": list(self.unassigned),
            "signals_used": list(self.signals_used),
            "notes": list(self.notes),
        }


@dataclass
class ControlResult:
    """One control's complete discovery record."""

    index: int
    name: str | None = None
    ident: str | None = None
    label: str | None = None
    module: str | None = None
    classification: Classification = field(default_factory=Classification)
    range: RangeAnalysis | None = None
    mapping: MappingAnalysis | None = None
    discrete: DiscreteAnalysis | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    unknown: list[dict[str, str]] = field(default_factory=list)
    steps: dict[str, Any] = field(default_factory=dict)
    unit: str | None = None
    current_normalized: float | None = None
    section: str | None = None
    named_config: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "index": int(self.index),
            "classification": self.classification.to_dict(),
            "evidence": dict(self.evidence),
        }
        for key, value in (
            ("name", self.name),
            ("ident", self.ident),
            ("label", self.label),
            ("module", self.module),
            ("unit", self.unit),
            ("section", self.section),
        ):
            if value is not None:
                row[key] = value
        if self.current_normalized is not None:
            row["current_normalized"] = float(self.current_normalized)
        if self.steps:
            row["steps"] = dict(self.steps)
        if self.range is not None:
            row["range"] = self.range.to_dict()
        if self.mapping is not None:
            row["mapping"] = self.mapping.to_dict()
        if self.discrete is not None:
            row["discrete"] = self.discrete.to_dict()
        if self.unknown:
            row["unknown"] = [dict(item) for item in self.unknown]
        if self.named_config:
            row["named_config"] = dict(self.named_config)
        return row


@dataclass
class RunCost:
    """What the run cost, per stage, plus the limits it hit."""

    calls_by_stage: dict[str, int] = field(default_factory=dict)
    seconds_by_stage: dict[str, float] = field(default_factory=dict)
    budgets: dict[str, Any] = field(default_factory=dict)
    limits_hit: list[str] = field(default_factory=list)
    started_utc: str | None = None
    finished_utc: str | None = None
    checkpoints: list[dict[str, Any]] = field(default_factory=list)

    @property
    def total_calls(self) -> int:
        return int(sum(self.calls_by_stage.values()))

    @property
    def total_seconds(self) -> float:
        return float(sum(self.seconds_by_stage.values()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "calls_by_stage": {key: int(value) for key, value in self.calls_by_stage.items()},
            "seconds_by_stage": {key: round(float(value), 6) for key, value in self.seconds_by_stage.items()},
            "total_calls": self.total_calls,
            "total_seconds": round(self.total_seconds, 6),
            "budgets": dict(self.budgets),
            "limits_hit": list(self.limits_hit),
            "started_utc": self.started_utc,
            "finished_utc": self.finished_utc,
            "checkpoints": [dict(item) for item in self.checkpoints],
        }


@dataclass
class DiscoveryResult:
    """One discovery run's complete, re-checkable output."""

    engine_version: str
    mode: str
    identity: dict[str, Any] = field(default_factory=dict)
    inventory: Inventory = field(default_factory=Inventory)
    observations: list[ControlObservations] = field(default_factory=list)
    controls: list[ControlResult] = field(default_factory=list)
    grouping: GroupingResult = field(default_factory=GroupingResult)
    cost: RunCost = field(default_factory=RunCost)
    formats: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    sources: list[dict[str, Any]] = field(default_factory=list)

    def control(self, index: int) -> ControlResult | None:
        for row in self.controls:
            if row.index == index:
                return row
        return None

    def observation(self, index: int) -> ControlObservations | None:
        for row in self.observations:
            if row.index == index:
                return row
        return None

    def to_dict(self, *, with_observations: bool = True, with_inventory: bool = True) -> dict[str, Any]:
        row: dict[str, Any] = {
            "engine_version": self.engine_version,
            "mode": self.mode,
            "identity": dict(self.identity),
            "counts": {key: int(value) for key, value in self.counts.items()},
            "controls": [control.to_dict() for control in self.controls],
            "modules": self.grouping.to_dict(),
            "formats": [dict(item) for item in self.formats],
            "cost": self.cost.to_dict(),
            "warnings": [dict(item) for item in self.warnings],
        }
        if with_inventory:
            row["inventory"] = self.inventory.to_dict()
        if with_observations:
            row["observations"] = [item.to_dict() for item in self.observations]
        if self.sources:
            row["sources"] = [dict(item) for item in self.sources]
        return row
