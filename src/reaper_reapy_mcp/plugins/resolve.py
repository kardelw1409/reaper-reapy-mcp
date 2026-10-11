"""Session-scoped write-target tokens (contract 05 §5; AUD-003).

Three identity concepts stay separate: the **profile fingerprint** (durable,
cacheable), the **write-target token** (session-scoped, opaque, revalidated at
use) and the **state revision** (mutable, in model.py). This module owns the
token: it is issued from fresh host facts, maps server-side to those facts, and
is revalidated immediately before every write unit and parameter batch.

A token is never durable: the raw host handle/GUID evidence it may carry is
never serialized, the token id is only ever handed to the caller in the live
response, and the transaction outcome contains no token at all (P1-5).
"""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass, field
from typing import Any

from .cache import (
    REQUIRED_FINGERPRINT_FIELDS,
    CacheLoad,
    CacheStatus,
    ProfileBuild,
    ProfileCache,
    adopt_profile,
    build_capability_profile,
    key_material_of,
)
from .errors import (
    AmbiguousInstance,
    IncompatibleSettings,
    InstanceNotFound,
    SnapshotUnavailable,
    TargetChanged,
    VersionMismatch,
)
from .model import FxHostFacts, PluginInstanceRef, utc_now_iso
from .profile import validate_profile

__all__ = [
    "IDENTITY_FACT_FIELDS",
    "IDENTITY_CONTINUITY_LIMIT",
    "TargetToken",
    "TargetTokenRegistry",
    "ResolvedInstance",
    "ProfileResolution",
    "facts_identity",
    "facts_location",
    "identity_basis",
    "identity_missing_fields",
    "version_refusal",
    "iter_instances",
    "resolve_instances",
    "resolve_one",
    "resolve_instance_profile",
]

#: The fact fields that decide "is this still the same instance" (05 §5).
IDENTITY_FACT_FIELDS = ("fx_name", "class_id", "module", "param_count")

#: What a validated target **can** and **cannot** establish (measured, P8).
#:
#: The measurement that shapes this: an FX removed and re-added at the same slot
#: with the same build reproduced the **same** host handle (`handle_changed:
#: false`), while a second instance at another slot reported a different one. The
#: handle therefore identifies the *slot*, not the *instance*, and no datum the
#: host exposes was found that distinguishes an identical-build replacement.
IDENTITY_CONTINUITY_LIMIT: dict[str, Any] = {
    "validated": ["location", "build_facts", "state_revision"],
    "not_claimed": ["instance_continuity"],
    "reason": "identical_build_replacement_indistinguishable",
    "why": (
        "measured 2026-10-10: removing and re-adding the same build at the same FX slot "
        "reproduced the same session handle, and a different slot reported a different one - "
        "the handle identifies the slot, not the instance"
    ),
    "measured_by": (
        "tests/plugins/live/evidence/p8_l3_identity__20261010T203101Z__dcd339e9c219.json"
    ),
    "consequence": (
        "a mutation is authorised for the instance at that location **now**; the layer's "
        "guarantees are state-based (complete capture, verified write, verified restore) and "
        "it does not claim the instance is the one a previous call addressed"
    ),
}


def identity_basis() -> dict[str, Any]:
    """A copy of the continuity disclosure for a live response's ``meta``."""
    return {
        key: (list(value) if isinstance(value, list) else value)
        for key, value in IDENTITY_CONTINUITY_LIMIT.items()
    }


def facts_location(facts: FxHostFacts) -> dict[str, int]:
    return {"track_index": facts.track_index, "fx_index": facts.fx_index}


def facts_identity(facts: FxHostFacts) -> dict[str, Any]:
    """The profile-fingerprint fields as the host reports them right now."""
    identity: dict[str, Any] = {
        "fx_name": facts.fx_name,
        "class_id": facts.class_id,
        "module": facts.module,
        "param_count": facts.param_count,
    }
    if facts.version is not None:
        identity["version"] = facts.version
    return identity


@dataclass
class TargetToken:
    """An opaque, session-scoped reference to one validated instance."""

    id: str
    location: dict[str, int]
    identity: dict[str, Any]
    facts_snapshot: dict[str, Any] = field(default_factory=dict)
    session_context: dict[str, Any] = field(default_factory=dict)
    issued_utc: str = field(default_factory=utc_now_iso)

    @property
    def redacted_id(self) -> str:
        return f"{self.id[:8]}…"

    def __repr__(self) -> str:
        """Never leaks the full token id into logs or tracebacks."""
        return (
            f"TargetToken(id={self.redacted_id!r}, location={self.location!r}, "
            f"identity={self.identity!r})"
        )

    def differences_against(self, fresh: FxHostFacts) -> list[str]:
        """Which identity facts no longer match the fresh host facts.

        ``version`` follows the profile rule (07 §3.5): its absence is tolerated
        (it is legitimate key material), but a *present* pair that differs is a
        difference - the build at this location is not the one validated.

        ``session_handle`` (the host's FX handle) is compared too, and **only in
        this direction**: it is session evidence, so it lives in the token's
        in-process snapshot and is never serialized (05 §5).

        **What this does NOT do (measured, P8 2026-10-10).** It does not close
        L-3. Removing an FX and re-adding the same build at the same slot
        reproduced the *same* handle (measured: `handle_changed: false`, while a
        second instance at a different slot reported a different one) - the
        handle identifies the slot, not the instance. An identical-build
        replacement is therefore **indistinguishable**, and this layer says so
        rather than inventing an identifier that would only appear to prove
        sameness. The comparison is kept because a handle that *does* change is a
        real difference and must refuse (fail closed), and because it costs
        nothing when it stays the same.
        """
        diffs: list[str] = []
        if facts_location(fresh) != self.location:
            diffs.append("location")
        now = facts_identity(fresh)
        for field_name in IDENTITY_FACT_FIELDS:
            if now.get(field_name) != self.identity.get(field_name):
                diffs.append(field_name)
        now_version = now.get("version")
        expected_version = self.identity.get("version")
        if now_version is not None and expected_version is not None and now_version != expected_version:
            diffs.append("version")
        expected_handle = self.facts_snapshot.get("session_handle")
        fresh_handle = fresh.session_handle
        if expected_handle is not None:
            if fresh_handle is None:
                diffs.append("session_handle_unreadable")
            elif str(fresh_handle) != str(expected_handle):
                diffs.append("session_handle")
        return diffs

    def to_meta(self) -> dict[str, Any]:
        """What the live response's ``meta`` may carry: the opaque id, the
        location and the fingerprint - never facts_snapshot/session_context."""
        return {"target_token": self.id, "location": dict(self.location), "identity": dict(self.identity)}


class TargetTokenRegistry:
    """Server-side, session-scoped token store (never serialized wholesale).

    The registry also owns the **target-scoped in-process lock** (19 Decision
    3): one lock per validated target token, never keyed to a string or a slot
    number, held from final resolution through the complete verified outcome
    and recovery path.
    """

    def __init__(self, *, session_context: dict[str, Any] | None = None) -> None:
        self._tokens: dict[str, TargetToken] = {}
        self._locks: dict[str, threading.Lock] = {}
        self.session_context: dict[str, Any] = dict(session_context or {})

    def __len__(self) -> int:
        return len(self._tokens)

    def __repr__(self) -> str:
        return f"TargetTokenRegistry(n={len(self._tokens)}, context={sorted(self.session_context)})"

    def lock_for(self, token_id: str) -> threading.Lock:
        """The target-scoped lock; the same target always yields the same lock."""
        lock = self._locks.get(token_id)
        if lock is None:
            lock = threading.Lock()
            self._locks[token_id] = lock
        return lock

    def issue(self, *, facts: FxHostFacts, identity: dict[str, Any] | None = None) -> TargetToken:
        """Issue - or reuse - the token for this validated target.

        A session has ONE token per validated instance, so two requests against
        the same target share the target-scoped lock instead of interleaving.
        """
        resolved_identity = dict(identity if identity is not None else facts_identity(facts))
        location = facts_location(facts)
        for existing in self._tokens.values():
            if existing.location == location and existing.identity == resolved_identity:
                return existing
        token = TargetToken(
            id="tok_" + secrets.token_hex(16),
            location=location,
            identity=resolved_identity,
            facts_snapshot={
                "track_name": facts.track_name,
                "fx_count": facts.fx_count,
                "session_handle": facts.session_handle,
            },
            session_context=dict(self.session_context),
        )
        self._tokens[token.id] = token
        return token

    def get(self, token_id: str) -> TargetToken:
        token = self._tokens.get(token_id)
        if token is None:
            raise TargetChanged(
                "unknown or expired write-target token (tokens are session-scoped)",
                detail={
                    "reason": "unknown_target_token",
                    "target_token": str(token_id)[:8] + "…",
                },
            )
        return token

    def revalidate(self, token_id: str, fresh_facts: FxHostFacts) -> TargetToken:
        """Revalidate the token against fresh host facts.

        A stale or mismatched token is a typed refusal: the transaction
        **never** silently re-resolves onto a new instance. When exactly one
        candidate exists the caller re-issues explicitly; the refusal itself
        names the fields that moved.
        """
        token = self.get(token_id)
        differences = token.differences_against(fresh_facts)
        if differences:
            raise TargetChanged(
                "the write target no longer matches the token's validated facts",
                detail={
                    "reason": "target_facts_changed",
                    "fields": differences,
                    "expected": {"location": token.location, "identity": token.identity},
                    "observed": {"location": facts_location(fresh_facts), "identity": facts_identity(fresh_facts)},
                },
            )
        return token

    def invalidate(self, token_id: str) -> None:
        self._tokens.pop(token_id, None)

    def clear(self) -> None:
        self._tokens.clear()


# ---------------------------------------------------------------------------
# Instance resolution (P3)
# ---------------------------------------------------------------------------


@dataclass
class ResolvedInstance:
    """One provider-matching instance with its freshly read facts and token."""

    ref: "PluginInstanceRef"
    facts: FxHostFacts
    token: TargetToken

    @property
    def location(self) -> dict[str, int]:
        return dict(self.ref.location)


def _matches_selector(facts: FxHostFacts, track_name: str | None, selector: dict[str, Any] | None) -> bool:
    if not selector:
        return True
    if "track_index" in selector and int(selector["track_index"]) != facts.track_index:
        return False
    if "fx_index" in selector and int(selector["fx_index"]) != facts.fx_index:
        return False
    if "track_name" in selector:
        wanted = str(selector["track_name"]).strip().lower()
        if (track_name or "").strip().lower() != wanted:
            return False
    if "name" in selector:
        wanted = str(selector["name"]).strip().lower()
        if wanted not in (facts.fx_name or "").lower():
            return False
    return True


def iter_instances(bridge: Any, provider: Any, *, selector: dict[str, Any] | None = None) -> list[tuple[FxHostFacts, str | None]]:
    """Every FX slot the provider claims, narrowed by the selector.

    Enumeration is host-level; ``matches`` is provider-level. A slot whose
    identity facts cannot be read does not become a candidate (fail closed).

    **An addressed slot is the only slot read** (measured 2026-10-11, live).
    A slot's facts cost a full track-state-chunk read (``fx_facts``); reading
    every slot of every track measured **1536 ms for 4 slots** on a
    one-FX-per-track project, while the rest of a warm call is ~50 ms - i.e. the
    sweep, not the profile, dominated a call whose own selector already named
    the instance. A selector naming ``track_index`` therefore restricts the
    enumeration to that track, and one naming ``fx_index`` as well to that single
    slot. The facts of the addressed slot are read exactly as before (identity is
    still verified against the host, never assumed), an out-of-range slot yields
    no candidate exactly as the loop over ``range(n_fx)`` did, and slots the
    selector cannot match were filtered before - the filter is applied earlier,
    not weakened.
    """
    wanted_track: int | None = None
    wanted_slot: int | None = None
    if isinstance(selector, dict):
        for name, target in (("track_index", "track"), ("fx_index", "slot")):
            value = selector.get(name)
            if value is None:
                continue
            try:
                if target == "track":
                    wanted_track = int(value)
                else:
                    wanted_slot = int(value)
            except (TypeError, ValueError):
                if target == "track":
                    wanted_track = None
                else:
                    wanted_slot = None
    found: list[tuple[FxHostFacts, str | None]] = []
    for track in bridge.list_tracks():
        if wanted_track is not None and int(track.track_index) != wanted_track:
            continue
        slots = range(int(track.n_fx))
        if wanted_slot is not None:
            if wanted_slot not in slots:
                continue
            slots = range(wanted_slot, wanted_slot + 1)
        for fx_index in slots:
            facts = bridge.fx_facts(track.track_index, fx_index)
            if not provider.matches(facts):
                continue
            if not _matches_selector(facts, track.name, selector):
                continue
            found.append((facts, track.name))
    return found


def resolve_instances(
    bridge: Any,
    provider: Any,
    *,
    selector: dict[str, Any] | None = None,
    registry: "TargetTokenRegistry | None" = None,
) -> list[ResolvedInstance]:
    """All matching instances, each with its session-scoped target token."""
    registry = registry if registry is not None else TargetTokenRegistry()
    resolved: list[ResolvedInstance] = []
    for facts, track_name in iter_instances(bridge, provider, selector=selector):
        identity = provider.identify(facts)
        token = registry.issue(facts=facts, identity=identity)
        resolved.append(
            ResolvedInstance(
                ref=PluginInstanceRef(
                    host="reaper",
                    location=facts_location(facts),
                    provider=str(provider.id),
                    identity=identity,
                    display=f"{facts.fx_name} @ track {facts.track_index + 1} fx {facts.fx_index + 1}",
                ),
                facts=facts,
                token=token,
            )
        )
    return resolved


def resolve_one(
    bridge: Any,
    provider: Any,
    *,
    selector: dict[str, Any] | None = None,
    registry: "TargetTokenRegistry | None" = None,
) -> ResolvedInstance:
    """Exactly one instance, or a typed refusal - Core never picks among equals."""
    candidates = resolve_instances(bridge, provider, selector=selector, registry=registry)
    if not candidates:
        raise InstanceNotFound(
            "no plugin instance matches the request",
            detail={"selector": dict(selector or {}), "provider": str(provider.id)},
        )
    if len(candidates) > 1:
        raise AmbiguousInstance(
            "several plugin instances match and no selector disambiguates",
            detail={
                "selector": dict(selector or {}),
                "candidates": [
                    {"location": candidate.ref.location, "identity": candidate.ref.identity}
                    for candidate in candidates
                ],
            },
        )
    return candidates[0]


# ---------------------------------------------------------------------------
# Profile resolution + the fail-closed write gate (P4; 07 §3.6, 09 §5-§6)
# ---------------------------------------------------------------------------


def identity_missing_fields(identity: dict[str, Any]) -> list[str]:
    """Required fingerprint fields that are absent or unreadable (AUD-008).

    A missing field does not stop diagnostic reads - it disables caching and
    every mutation for the instance (07 §3.6).
    """
    missing: list[str] = []
    for name in REQUIRED_FINGERPRINT_FIELDS:
        value = identity.get(name)
        if name == "param_count":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                missing.append(name)
            continue
        if not isinstance(value, str) or not value.strip():
            missing.append(name)
    return missing


def version_refusal(profile: dict[str, Any], live_version: str | None) -> str | None:
    """The V1 version policy (07 §3.2): a live version outside the measured set
    refuses writes; an absent version is tolerated (09 §2)."""
    if live_version is None:
        return None
    identity = profile.get("identity") if isinstance(profile.get("identity"), dict) else {}
    compatible = identity.get("compatible_versions")
    if not isinstance(compatible, list) or not compatible:
        return "the profile declares no compatible_versions to compare against"
    if str(live_version) in [str(item) for item in compatible]:
        return None
    return (
        f"the live version {live_version!r} is not in the measured set "
        f"{sorted(str(item) for item in compatible)}; writes are refused (no override exists in V1)"
    )


@dataclass
class ProfileResolution:
    """One resolved instance plus its validated capability profile and the
    write gate derived from it (07 §3.6, 09 §5-§6)."""

    instance: ResolvedInstance
    identity: dict[str, Any]
    profile: dict[str, Any]
    build: ProfileBuild
    cache: CacheStatus
    missing_identity: list[str] = field(default_factory=list)
    version_refusal_reason: str | None = None

    @property
    def usable_for_writes(self) -> bool:
        """Writes/mutations are enabled only with a complete identity, a
        version inside the measured set and a profile that validates."""
        return not self.write_blockers()

    @property
    def provisional(self) -> bool:
        """True while the identity is incomplete: reads are diagnostic only."""
        return bool(self.missing_identity)

    def write_blockers(self) -> list[str]:
        blockers: list[str] = []
        if self.missing_identity:
            blockers.append("identity_incomplete")
        if self.version_refusal_reason:
            blockers.append("version_mismatch")
        if self.build.validation.rebuild_required:
            blockers.append("profile_invalid")
        return blockers

    def require_writable(self) -> None:
        """Typed refusal for any mutation path (P4-5). Diagnostic reads never
        call this and are marked provisional through ``meta()`` instead."""
        if self.missing_identity:
            raise SnapshotUnavailable(
                "writes are disabled: a required identity field is missing or unreadable; "
                "reads are provisional and may never be the basis of a mutation",
                detail={
                    "missing_fields": list(self.missing_identity),
                    "reason": "identity_required_fields_missing",
                    "provisional": True,
                },
            )
        if self.version_refusal_reason:
            raise VersionMismatch(
                self.version_refusal_reason,
                detail={
                    "live_version": self.identity.get("version"),
                    "compatible_versions": list(
                        (self.profile.get("identity") or {}).get("compatible_versions") or []
                    ),
                    "override": None,
                },
            )
        if self.build.validation.rebuild_required:
            raise IncompatibleSettings(
                "the provider's profile failed validation and cannot be rebuilt into a usable one",
                detail={"problems": self.build.validation.codes()},
            )

    def meta(self) -> dict[str, Any]:
        """What a live response's ``meta`` may carry: the cache status and the
        provisional flag (the target token is added by the caller)."""
        return {
            "cache": self.cache.as_dict(),
            "provisional": self.provisional,
            "writes_disabled": not self.usable_for_writes,
            "write_blockers": self.write_blockers(),
        }


def resolve_instance_profile(
    bridge: Any,
    provider: Any,
    *,
    selector: dict[str, Any] | None = None,
    registry: "TargetTokenRegistry | None" = None,
    cache: ProfileCache | None = None,
) -> ProfileResolution:
    """Resolve one instance, then its profile: cache when valid, build when not.

    Fail-open on nothing, fail-closed on identity: with a required field
    missing, the profile is built in memory, the cache is touched neither for
    read nor write, and the instance is write-disabled with reads marked
    provisional (07 §3.6, 09 §5.1).

    **The warm path builds nothing** (owner Decision 4). When the provider can
    declare its key material without a build (`cache.key_material_of`), the key
    is computed from that declaration, the cache is consulted first, and a hit is
    served directly - the profile projection, the registry parse and the
    behaviour-affecting digest work a hit never uses are not performed. Two
    guards keep that from weakening anything: the cache load still re-derives the
    key from the entry's own provenance against the live identity, and a *miss*
    is followed by the full build whose provenance must re-derive the very key
    the fast path computed - a disagreement between the provider's declaration
    and its own built provenance is refused loudly (`cache.key_material`) instead
    of filing a profile under a key nothing can re-derive.

    Providers without the hook keep the established build-then-load order, which
    is correct but pays the build on every call.
    """
    instance = resolve_one(bridge, provider, selector=selector, registry=registry)
    identity = dict(instance.ref.identity)
    cache = cache if cache is not None else ProfileCache(provider_id=str(provider.id))
    cache.begin_call()
    missing = identity_missing_fields(identity)

    if missing:
        build = build_capability_profile(provider, identity)
        cache.refuse_no_identity(
            missing,
            reason="a required identity field is missing; the profile is in-memory only",
        )
        return ProfileResolution(
            instance=instance,
            identity=identity,
            profile=build.profile,
            build=build,
            cache=cache.status.snapshot(),
            missing_identity=missing,
            version_refusal_reason=None,
        )

    material = key_material_of(provider, identity)
    declared_key = material.key_for(str(provider.id), identity) if material is not None else None
    loaded: CacheLoad | None = None
    if declared_key is not None:
        cache.status.key_without_build = True
        loaded = cache.load(declared_key, identity=identity)
        if loaded.status == "hit" and loaded.profile is not None:
            adopt_profile(provider, loaded.profile)
            validation = loaded.validation or validate_profile(loaded.profile)
            return ProfileResolution(
                instance=instance,
                identity=identity,
                profile=loaded.profile,
                build=ProfileBuild.from_cached_profile(
                    profile=loaded.profile, validation=validation, material=material
                ),
                cache=cache.status.snapshot(),
                missing_identity=[],
                version_refusal_reason=version_refusal(loaded.profile, identity.get("version")),
            )

    build = build_capability_profile(provider, identity)
    cache.status.built = True
    key = build.key_for(str(provider.id), identity)
    if declared_key is not None and key != declared_key:
        cache.refuse_key_material(
            built_key=key, material_key=declared_key, provider_id=str(provider.id)
        )
        adopt_profile(provider, build.profile)
        return ProfileResolution(
            instance=instance,
            identity=identity,
            profile=build.profile,
            build=build,
            cache=cache.status.snapshot(),
            missing_identity=[],
            version_refusal_reason=version_refusal(build.profile, identity.get("version")),
        )

    if declared_key is None:
        loaded = cache.load(key, identity=identity)
    if loaded is not None and loaded.status == "hit" and loaded.profile is not None:
        profile = loaded.profile
    else:
        profile = build.profile
        if build.cacheable:
            cache.save(key, profile)

    adopt_profile(provider, profile)
    refusal = version_refusal(profile, identity.get("version"))
    return ProfileResolution(
        instance=instance,
        identity=identity,
        profile=profile,
        build=build,
        cache=cache.status.snapshot(),
        missing_identity=[],
        version_refusal_reason=refusal,
    )
