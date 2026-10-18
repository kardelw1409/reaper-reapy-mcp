"""Provider and State Codec contracts (contract 08 §2-§3; 05 §3).

P0 status: protocol declarations only. Core never sees provider identifiers,
provider semantics or block bytes; the codec operates on the selected
`TargetedStateSlice`, never on an undifferentiated track chunk (AUD-001), and
the bridge never receives a provider callback (AUD-004).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, Sequence, runtime_checkable

from .model import (
    CaptureScope,
    Finding,
    FxHostFacts,
    ResolvedRequest,
    StateWritePlan,
    StateWriteUnit,
    TargetedStateSlice,
    VerificationPolicy,
    VerificationReport,
)

__all__ = [
    "CapabilityProfile",
    "DocumentView",
    "DocumentSnapshot",
    "DependencyRuleSet",
    "PluginProvider",
    "StateCodec",
]

#: A capability profile is a JSON-shaped dict validated by profile.py (07 §1).
CapabilityProfile = dict[str, Any]
#: Provider-shaped decoded document (never interpreted by Core).
DocumentView = Any
#: Codec-produced restorable parts of a snapshot.
DocumentSnapshot = Any


@dataclass
class DependencyRuleSet:
    """Provider dependency rules: data plus an optional provider evaluator.

    The engine (P2) evaluates the rules against the *projected* world; rule
    content stays provider data (10 §3.3 - no dependency DSL in Core).
    """

    rules: list[dict[str, Any]] = field(default_factory=list)
    evaluate: Callable[[dict[str, Any], Sequence[dict[str, Any]]], list[Finding]] | None = None


@runtime_checkable
class PluginProvider(Protocol):
    """Plugin-specific semantics, bindings, laws, rules and codec (08 §2).

    Two **optional** hooks are recognized by Core when a provider declares them
    (they are not part of this protocol because Core's callers must work without
    them):

    - ``profile_source_rows(identity) -> list[{"file", "sha256"}]``: the data
      sources the profile derives from, **hashed without building it**. Core uses
      it to compute the cache key before deciding whether a build is needed, so a
      warm cache hit costs a hash instead of a full projection. The rows must be
      the same ones ``build_profile`` declares in ``provenance.sources``: a
      disagreement is caught at runtime (`cache.key_material`) and the cache
      write is refused rather than filed under an underivable key.
    - ``adopt_profile(profile) -> None``: adopt the profile Core is about to
      serve. A provider whose other methods consult its own last-built profile
      (rules, limits, structural targets) needs this, because a cache hit builds
      nothing.
    - ``state_view(document) -> dict``: a read-only, descriptor-shaped view of the
      decoded document, attached to ``plugin_state`` data as ``state_view`` when the
      provider declares it (P30). It exists for content a generic summary cannot
      express - *which table each oscillator reads*, which is document state with no
      parameter behind it. A provider without the hook answers the same summary it
      always did, and a failure inside the hook is reported as a warning row rather
      than turning a readable state into an error.
    """

    id: str
    """Stable provider id used in refs/profile."""

    implementation_revision: str
    """Provider-declared revision covering behaviour-affecting provider code
    (dependency rules, laws, codec, write order, verification policy, limits);
    a cache determinant (AUD-008)."""

    def artifact_paths(self) -> list[str]:
        """Behaviour-affecting files hashed into the profile cache key."""
        ...

    def matches(self, facts: FxHostFacts) -> bool:
        """Whether this provider serves the instance the fresh facts describe."""
        ...

    def identify(self, facts: FxHostFacts) -> dict[str, Any]:
        """Pure: produce the profile fingerprint fields (no writes)."""
        ...

    def build_profile(self, identity: dict[str, Any]) -> CapabilityProfile:
        """Build a profile from provider data; the cache is Core's concern."""
        ...

    def normalize_request(
        self, profile: CapabilityProfile, request: dict[str, Any]
    ) -> ResolvedRequest:
        """Resolve names/values against declarations; no host access.

        Refusals are returned as typed problem rows inside the result.
        """
        ...

    def convert_depth(self, profile: CapabilityProfile, destination: str, depth: Any) -> float:
        """Semantic depth -> native amount, or raise ``NoConversion``."""
        ...

    def dependencies(self) -> DependencyRuleSet:
        """Rules as data plus declared evaluation hooks."""
        ...

    def codec(self) -> "StateCodec":
        """The provider's state codec (one per provider in V1)."""
        ...

    def write_order(self) -> tuple[str, ...]:
        """Provider-declared write order, enforced by the engine (e.g.
        ``("state", "parameters")``); never caller-configurable."""
        ...

    def verification_policy(self) -> VerificationPolicy:
        """Tolerances, comparators and timing per value type."""
        ...

    def capture_scope(self) -> CaptureScope:
        """The mutable channels an operation may touch and must snapshot."""
        ...

    def limits(self) -> list[str]:
        """Honest declared restrictions surfaced to callers."""
        ...


@runtime_checkable
class StateCodec(Protocol):
    """Document semantics for one provider (08 §3).

    The codec operates on the selected native payload: ``decode`` receives a
    ``TargetedStateSlice`` whose span and digest bind it to the chunk it came
    from, so decoding the wrong block is impossible by construction.
    """

    def decode(self, slice: TargetedStateSlice) -> DocumentView:
        """The slice's own block text -> provider-shaped document view."""
        ...

    def plan_state_writes(
        self, doc: DocumentView, changes: dict[str, Any], *, other_track_chars: int = 0
    ) -> StateWritePlan:
        """Ordered ``StateWriteUnit`` rows + rollback plan + verification plan.

        Replaces the former ``structural_units()`` count: the codec must
        *decompose* multi-write structural limits explicitly (e.g. one route
        per state unit), never merely report a count.

        ``other_track_chars`` is the text in the SAME track chunk that is not this instance's
        block. The host's read limit applies to the whole track chunk, so a codec whose plan must
        stay readable subtracts it (P42, measured live: two instances on one track each inside the
        per-instance budget put the track past the limit and the chunk became unreadable). A
        provider with no such budget ignores the argument.
        """
        ...

    def encode_slice(
        self, doc: DocumentView, unit: StateWriteUnit, current: TargetedStateSlice
    ) -> TargetedStateSlice:
        """New block text for exactly the selected span; byte-exact outside
        the document; rooted-path enforcement is the codec's."""
        ...

    def verify(
        self, readback: DocumentView, unit: StateWriteUnit, policy: VerificationPolicy
    ) -> VerificationReport:
        """Compare in the space that was written; per-change problems."""
        ...

    def snapshot_document(self, doc: DocumentView) -> DocumentSnapshot:
        """Codec-produced restorable document/routes parts of a snapshot."""
        ...

    def restore_changes(self, doc: DocumentView, snap: DocumentSnapshot) -> dict[str, Any]:
        """Restoration expressed as document changes, executed through
        ``plan_state_writes`` - never a blind blob write."""
        ...
