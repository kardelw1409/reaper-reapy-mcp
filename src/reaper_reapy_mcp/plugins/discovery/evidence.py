"""Evidence, provenance and confidence (P26 §11 of the brief).

Every derived property the engine publishes carries **where it came from**, not
just its value. This module fixes the vocabulary once so that no analyzer can
invent its own idea of "probably", and it implements the two precedence rules
the brief demands:

1. a strong direct source outranks a weak indirect estimate;
2. **a weak source may never narrow a claim a stronger source already made**
   (`resolve_range`) - the weak claim is recorded as a conflict instead.

Confidence is deliberately **categorical**, not a percentage: a bare number
would be a guess dressed as a measurement. The strength ladder below is the
whole scale, and the artifacts record the evidence that produced each level.

Source kinds (ordered from direct to weakest):

======================  =======================================================
``accepted``            the host *stored* and read back a value (setter proof)
``declared``            a host/format metadata field answered for this control
``measured``            the host's own formatter printed these values
``derived``             computed from measurements (fit, band edges, counts)
``heuristic``           inference from names/structure/repetition alone
``unknown``             not established; the reason is recorded, never filled in
======================  =======================================================
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

__all__ = [
    "SOURCE_KINDS",
    "SOURCE_RANK",
    "STRENGTHS",
    "ATTEMPT_STATUSES",
    "UNKNOWN_REASONS",
    "Evidence",
    "Claim",
    "Conflict",
    "strength_for",
    "rank_of",
    "resolve_range",
    "resolve_scalar",
]

#: Ordered strongest-first; the order is the precedence ladder.
SOURCE_KINDS: tuple[str, ...] = ("accepted", "declared", "measured", "derived", "heuristic", "unknown")

#: Rank used by the precedence rules (larger = stronger).
SOURCE_RANK: dict[str, int] = {kind: len(SOURCE_KINDS) - index for index, kind in enumerate(SOURCE_KINDS)}

#: Categorical strength. `strong` = a direct source or a measurement with an
#: independent check; `medium` = measured but ambiguous (a competing model fits
#: equally well); `weak` = heuristic inference only; `none` = nothing usable.
STRENGTHS: tuple[str, ...] = ("strong", "medium", "weak", "none")

#: The status of one *attempt* to read a metadata channel. A channel that the
#: plugin or the host does not implement is `unsupported`/`absent` - an explicit
#: fact about the plugin, never an empty string.
ATTEMPT_STATUSES: tuple[str, ...] = ("ok", "absent", "unsupported", "refused", "error", "not_attempted")

#: Why a property is unknown. The brief forbids "guessing instead of measuring";
#: this vocabulary is what a consumer reads instead of a guess.
UNKNOWN_REASONS: tuple[str, ...] = (
    "api_does_not_expose",  # the host API has no such read for this control
    "format_adapter_unavailable",  # no reachable native-metadata channel
    "formatter_absent",  # the plugin prints no text for this value
    "formatter_constant",  # every sampled value prints the same text
    "no_observations",  # nothing was sampled (budget or refusal)
    "model_unstable",  # no candidate model fits within the printed precision
    "insufficient_points",  # too few usable (position, value) pairs
    "ambiguous_model",  # two models fit equally well
    "metadata_conflict",  # sources disagree and no rule resolves them
    "printed_precision_unknown",  # the text carries no usable resolution
    "budget_exhausted",  # the run hit its declared limit
    "requires_setter_experiment",  # needs a (not-yet-authorised) write test
    "not_attempted",  # the stage did not run
)


@dataclass
class Evidence:
    """One derived property's provenance row."""

    source_kind: str = "unknown"
    method: str = ""
    method_version: Any = None
    strength: str = "none"
    inputs: dict[str, Any] = field(default_factory=dict)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    verified: bool = False
    reason: str | None = None
    notes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.source_kind not in SOURCE_KINDS:
            raise ValueError(f"unknown evidence source kind {self.source_kind!r}")
        if self.strength not in STRENGTHS:
            raise ValueError(f"unknown evidence strength {self.strength!r}")
        if self.reason is not None and self.reason not in UNKNOWN_REASONS:
            raise ValueError(f"unknown evidence reason {self.reason!r}")

    def to_dict(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "source_kind": self.source_kind,
            "method": self.method,
            "strength": self.strength,
        }
        if self.method_version is not None:
            row["method_version"] = self.method_version
        if self.inputs:
            row["inputs"] = dict(self.inputs)
        if self.conflicts:
            row["conflicts"] = [dict(item) for item in self.conflicts]
        row["verified"] = bool(self.verified)
        if self.reason is not None:
            row["reason"] = self.reason
        if self.notes:
            row["notes"] = list(self.notes)
        return row

    @classmethod
    def unknown(cls, reason: str, *, method: str = "", notes: Sequence[str] = ()) -> "Evidence":
        return cls(
            source_kind="unknown",
            method=method,
            strength="none",
            reason=reason,
            notes=list(notes),
        )


@dataclass
class Claim:
    """One source's answer about a property, with its own provenance."""

    value: Any
    source_kind: str
    source: str = ""
    strength: str = "none"
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        row = {
            "value": self.value,
            "source_kind": self.source_kind,
            "source": self.source,
            "strength": self.strength,
        }
        if self.detail:
            row["detail"] = dict(self.detail)
        return row


@dataclass
class Conflict:
    """Two sources disagree and the rule that was applied."""

    field_name: str
    claims: list[Claim]
    rule: str
    resolution: Any
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field_name,
            "rule": self.rule,
            "resolution": self.resolution,
            "note": self.note,
            "claims": [claim.to_dict() for claim in self.claims],
        }


def rank_of(source_kind: str) -> int:
    return SOURCE_RANK.get(str(source_kind), 0)


def strength_for(source_kind: str, *, checked: bool = False, ambiguous: bool = False) -> str:
    """The strength ladder, derived from the source kind (never hand-set).

    ``checked`` means an independent second look confirmed it (a held-out point,
    a re-read, a band interior). ``ambiguous`` means a competing explanation fits
    the same evidence, which caps the level at ``medium``.
    """
    if source_kind == "accepted":
        return "strong"
    if source_kind == "declared":
        return "medium" if ambiguous else "strong"
    if source_kind == "measured":
        if ambiguous:
            return "medium"
        return "strong" if checked else "medium"
    if source_kind == "derived":
        if ambiguous:
            return "weak"
        return "medium" if checked else "weak"
    if source_kind == "heuristic":
        return "weak"
    return "none"


def _bounds(value: Any) -> tuple[float, float] | None:
    if isinstance(value, (list, tuple)) and len(value) == 2:
        low, high = value
    elif isinstance(value, dict):
        low, high = value.get("min"), value.get("max")
    else:
        return None
    try:
        low_f, high_f = float(low), float(high)
    except (TypeError, ValueError):
        return None
    if low_f > high_f:
        low_f, high_f = high_f, low_f
    return low_f, high_f


def resolve_range(field_name: str, claims: Iterable[Claim]) -> tuple[tuple[float, float] | None, Conflict | None]:
    """Resolve competing numeric-range claims under the precedence rules.

    Returns ``(bounds, conflict)``. The chosen bounds are the **strongest**
    claim's bounds widened to the union of every stronger-or-equal claim, so a
    weaker (e.g. corpus-sampled or display-derived) source can never narrow a
    range a stronger one declared. A claim that tried to narrow is reported as a
    conflict rather than silently dropped.
    """
    usable = [(claim, _bounds(claim.value)) for claim in claims]
    usable = [(claim, bounds) for claim, bounds in usable if bounds is not None]
    if not usable:
        return None, None
    best_rank = max(rank_of(claim.source_kind) for claim, _ in usable)
    strongest = [(claim, bounds) for claim, bounds in usable if rank_of(claim.source_kind) == best_rank]
    low = min(bounds[0] for _, bounds in strongest)
    high = max(bounds[1] for _, bounds in strongest)
    conflict: Conflict | None = None
    disagreements = [
        (claim, bounds)
        for claim, bounds in usable
        if bounds[0] < low - 1e-12
        or bounds[1] > high + 1e-12
        or bounds != (low, high)
    ]
    if disagreements:
        narrowed = [
            claim
            for claim, bounds in disagreements
            if bounds[0] > low + 1e-12 or bounds[1] < high - 1e-12
        ]
        conflict = Conflict(
            field_name=field_name,
            claims=[claim for claim, _ in usable],
            rule="strongest_widest_wins",
            resolution=[low, high],
            note=(
                "sources disagree; the strongest claim's bounds are kept and widened to the "
                "union of equally strong claims - a weaker source is recorded but never narrows them"
                + (f"; {len(narrowed)} weaker claim(s) were narrower" if narrowed else "")
            ),
        )
    return (low, high), conflict


def resolve_scalar(field_name: str, claims: Sequence[Claim]) -> tuple[Any, Conflict | None]:
    """Resolve competing scalar claims: the strongest wins, disagreement is kept.

    Equally strong claims that disagree resolve to the first declared (stable
    order) and are reported as an unresolved conflict - the engine never picks a
    value it cannot justify.
    """
    usable = [claim for claim in claims if claim.value is not None]
    if not usable:
        return None, None
    best_rank = max(rank_of(claim.source_kind) for claim in usable)
    strongest = [claim for claim in usable if rank_of(claim.source_kind) == best_rank]
    values = {repr(claim.value) for claim in strongest}
    conflict = None
    if len(values) > 1:
        conflict = Conflict(
            field_name=field_name,
            claims=list(usable),
            rule="strongest_wins_ties_reported",
            resolution=strongest[0].value,
            note="equally strong sources disagree; the first is published and the tie is recorded",
        )
    elif len({repr(claim.value) for claim in usable}) > 1:
        conflict = Conflict(
            field_name=field_name,
            claims=list(usable),
            rule="strongest_wins",
            resolution=strongest[0].value,
            note="a weaker source disagrees; the stronger claim is published",
        )
    return strongest[0].value, conflict
