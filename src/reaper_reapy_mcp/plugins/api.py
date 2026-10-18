"""The 7 tool bodies (P9): resolution -> profile -> transaction -> envelope.

These are the bodies the registered tools dispatch to. They contain no host work
at import time, no provider identifier (the provider is resolved by id through
`providers.registry`, 08 §8.1) and no way around the safety pipeline: a mutating
call builds the same `Transaction` the P2 engine tests exercise, and a read-only
call uses only read verbs.

Rules kept here (06 §2, §10):

- every answer is a schema-2 envelope; a typed error is never text in a success
  payload, and a mutating call carries an `outcome` whether it succeeded or not;
- `dry_run` defaults to **true**: a caller must ask for application explicitly;
- with **zero providers configured** every tool answers a typed
  `UnsupportedProvider` (AUD-013: registration is unconditional, the target must
  refuse) - the tools are visible and inert, never absent;
- `meta` carries the cache status, the provisional flag and the target token; the
  token is the only sanctioned bearer of the opaque id, and never appears inside
  `data` or an `outcome`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from . import modulation as MOD
from . import host_scope as HS
from . import resolve as RS
from . import values as V
from .binding import declared_names, indexed_controls, normalise_name
from .cache import ProfileCache
from .errors import (
    AmbiguousInstance,
    IdentityContinuityUnprovable,
    InstanceNotFound,
    InvalidValue,
    NotConnected,
    PluginError,
    SnapshotUnavailable,
    StaleInstance,
    TargetChanged,
    UnsupportedOverride,
    UnsupportedProvider,
)
from .host.reaper import ReaperHostBridge
from .model import PluginInstanceRef, utc_now_iso
from .providers import registry as provider_registry
from .resolve import TargetTokenRegistry
from .transaction import Transaction

__all__ = [
    "PROVIDERS_ENV",
    "BODIES",
    "PluginConfig",
    "plugin_instances",
    "plugin_describe",
    "plugin_find",
    "plugin_read",
    "plugin_write",
    "plugin_state",
    "plugin_modulation",
    "plugin_prepare",
    "register_production_bodies",
    "session_bridge",
    "session_registry",
]

#: Which providers are enabled (`REAPER_MCP_PLUGIN_PROVIDERS`, comma separated).
#: Unset or empty means **zero providers** (13 §4): every plugin tool then answers
#: a typed `UnsupportedProvider`, which is the documented inert-but-visible state.
PROVIDERS_ENV = "REAPER_MCP_PLUGIN_PROVIDERS"

#: The strength order of the prepare modes (P27). A stored artifact answers a
#: request only when its own mode is at least as strong; a request that outranks
#: it is measured instead (measured live 2026-10-12: `mode="prepare"` on a
#: stored `inventory` artifact served it with zero host calls and no display
#: sweep, against the declared "prepare adds the display sweep").
_MODE_RANK: dict[str, int] = {"inventory": 1, "prepare": 2, "deep": 3}

_BRIDGE: ReaperHostBridge | None = None
_REGISTRY: TargetTokenRegistry | None = None


@dataclass
class PluginConfig:
    """Provider configuration, read from the environment on first use."""

    provider_ids: tuple[str, ...] = ()
    bridge_timeout: float = 30.0
    cache_dir: str | None = None
    env: Mapping[str, str] | None = field(default=None, repr=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "PluginConfig":
        environ = os.environ if env is None else env
        raw = environ.get(PROVIDERS_ENV)
        ids = tuple(
            part.strip().lower() for part in str(raw or "").replace(";", ",").split(",") if part.strip()
        )
        return cls(provider_ids=ids, env=environ)


def session_bridge(config: PluginConfig | None = None) -> ReaperHostBridge:
    """The process-wide host bridge (created on first use; no connection yet)."""
    global _BRIDGE
    if _BRIDGE is None:
        _BRIDGE = ReaperHostBridge(timeout=float((config or PluginConfig.from_env()).bridge_timeout))
    return _BRIDGE


def session_registry() -> TargetTokenRegistry:
    """The process-wide, session-scoped token registry (one token per target)."""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = TargetTokenRegistry(session_context={"server": "reaper-reapy-mcp"})
    return _REGISTRY


# ---------------------------------------------------------------------------
# Configuration and resolution helpers
# ---------------------------------------------------------------------------


def _config(config: Any) -> PluginConfig:
    if isinstance(config, PluginConfig):
        return config
    return PluginConfig.from_env()


def _providers(config: PluginConfig) -> list[Any]:
    """Every configured provider, or a typed refusal listing what is known."""
    if not config.provider_ids:
        raise UnsupportedProvider(
            f"no plugin provider is configured; set {PROVIDERS_ENV} to enable one",
            detail={
                "variable": PROVIDERS_ENV,
                "configured": [],
                "known_providers": list(provider_registry.known_provider_ids()),
            },
        )
    return [provider_registry.load_provider(provider_id) for provider_id in config.provider_ids]


def _selector(instance: Any) -> dict[str, Any] | None:
    """A selector from the caller's instance argument (a ref or a selector)."""
    if instance is None:
        return None
    if not isinstance(instance, dict):
        raise InvalidValue(
            "`instance` must be the object `plugin_instances` returned, or a selector object",
            detail={"type": type(instance).__name__},
        )
    location = instance.get("location")
    if isinstance(location, dict):
        selector = {
            key: location[key] for key in ("track_index", "fx_index") if location.get(key) is not None
        }
        return selector or None
    nested = instance.get("selector")
    if isinstance(nested, dict):
        return dict(nested) or None
    selector = {
        key: instance[key]
        for key in ("track_index", "fx_index", "track_name", "name")
        if instance.get(key) is not None
    }
    return selector or None


def _provider_for(config: PluginConfig, instance: Any) -> Any:
    """The provider the caller addressed, or the only one configured."""
    providers = _providers(config)
    wanted = None
    if isinstance(instance, dict) and instance.get("provider"):
        wanted = str(instance["provider"]).strip().lower()
    if wanted is None:
        if len(providers) == 1:
            return providers[0]
        raise UnsupportedProvider(
            "several providers are configured and the instance names none",
            detail={"configured": list(config.provider_ids), "instance_provider": None},
        )
    for provider in providers:
        if str(getattr(provider, "id", "")).strip().lower() == wanted:
            return provider
    raise UnsupportedProvider(
        f"the instance names the provider {wanted!r}, which is not configured",
        detail={"configured": list(config.provider_ids), "instance_provider": wanted},
    )


def _instance_claims(instance: Any) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str | None]:
    """What the caller's ``instance`` object claims, split by what can be checked.

    Three separate things travel in one object and they are **not** equivalent:

    - a **selector** (track/fx, optionally names) — re-resolved against the host;
    - a **build identity** claim — comparable with fresh facts, so a mismatch is
      a typed refusal (before this check, a caller-supplied identity was
      silently ignored: the write went to whatever occupied the slot);
    - a **session token** — only meaningful to this process's registry, so it is
      validated rather than ignored.

    None of them proves *instance continuity*: an identical-build replacement at
    the same slot is indistinguishable (measured, `p8_l3_identity__20261010T203101Z`),
    and the layer says so in ``meta.identity_basis`` instead of pretending the
    token settled it.
    """
    selector = _selector(instance)
    identity: dict[str, Any] | None = None
    token_id: str | None = None
    if isinstance(instance, dict) and "identity" in instance:
        raw_identity = instance.get("identity")
        if raw_identity is None:
            pass  # a caller that supplies nothing claims nothing
        elif not isinstance(raw_identity, dict):
            raise InvalidValue(
                "`instance.identity` must be an object of build facts",
                detail={
                    "reason": "identity_claim_not_comparable",
                    "argument": "instance.identity",
                    "type": type(raw_identity).__name__,
                    "comparable_fields": list(RS.IDENTITY_FACT_FIELDS),
                },
            )
        else:
            comparable = {
                key: raw_identity[key]
                for key in RS.IDENTITY_FACT_FIELDS
                if raw_identity.get(key) is not None
            }
            if raw_identity.get("version") is not None:
                comparable["version"] = raw_identity["version"]
            if not comparable:
                raise InvalidValue(
                    "`instance.identity` carries no field this layer can check; it is refused "
                    "rather than silently ignored",
                    detail={
                        "reason": "identity_claim_not_comparable",
                        "argument": "instance.identity",
                        "supplied": sorted(str(key) for key in raw_identity),
                        "comparable_fields": list(RS.IDENTITY_FACT_FIELDS) + ["version"],
                    },
                )
            identity = comparable
    if isinstance(instance, dict):
        for key in ("target_token", "token"):
            value = instance.get(key)
            if isinstance(value, str) and value.strip():
                token_id = value.strip()
                break
    return selector, identity, token_id


def _require_continuity_basis(
    token_id: str | None,
    registry: TargetTokenRegistry,
    *,
    tool: str,
) -> None:
    """Refuse a mutation that rests on instance continuity (Decision 6b).

    A caller-supplied write-target token is a claim about **the instance that was
    validated**, and the measured host cannot back it: removing and re-adding the
    same build at the same slot reproduced the same session handle and the same
    build facts (class id, module, name, parameter count), and the artifact records
    no datum the host exposes that separates a replacement from the original
    (`p8_l3_identity__20261010T203101Z`; no ordinal or state-digest comparison of
    the replacement was made - the probe compared digests to show the *addressed*
    instance had not moved). So "still the same instance" is unprovable. Accepting
    the token as a permit would let a caller's belief stand in for verification -
    the same class of defect as an accepted `force` - so the mutation is refused
    **before any host work**, and the refusal names the remedy: address the instance
    by location, which makes a present-tense claim the layer *can* honour (it
    captures, revalidates, writes, verifies and can restore within the one call).

    Reads are deliberately not affected: a read makes no write claim, so the
    token is compared against fresh facts and refused only when they disagree.
    """
    if not token_id:
        return
    token = registry.get(token_id)  # unknown/expired -> TargetChanged before any host work
    raise IdentityContinuityUnprovable(
        "this mutation carries a write-target token, which claims the instance that was "
        "validated; the host cannot prove instance continuity (an identical-build replacement "
        "is indistinguishable), so the claim is refused before any host work",
        detail={
            "reason": "instance_continuity_not_provable",
            "tool": tool,
            "target_token": token.redacted_id,
            "not_provable_by": [
                "location",
                "build_facts",
                "session_handle",
                "state_revision",
            ],
            "measured_by": RS.IDENTITY_CONTINUITY_LIMIT["measured_by"],
            "remedy": (
                "address the instance by location (track_index/fx_index, or the instance object "
                "from plugin_instances) - a present-tense claim the layer verifies inside the call"
            ),
            "reads_unaffected": True,
        },
    )


def _check_instance_claims(
    resolution: RS.ProfileResolution,
    *,
    expected_identity: dict[str, Any] | None,
    token_id: str | None,
    registry: TargetTokenRegistry,
) -> None:
    """Refuse when a caller-supplied identity or token disagrees with the host.

    Two claims are decided here and they are not equivalent:

    - an **identity** claim is a present-tense expectation ("these facts hold at
      that location now") - it is compared against fresh host facts, and a
      difference is a typed refusal;
    - a **token** is a claim about the past ("the instance that was validated");
      for a mutation it is refused earlier by `_require_continuity_basis`, and on
      the read path it is compared against fresh facts, because a read makes no
      write claim.
    """
    facts = resolution.instance.facts
    if expected_identity:
        observed = RS.facts_identity(facts)
        differences = [
            field
            for field, expected in expected_identity.items()
            if observed.get(field) != expected
        ]
        if differences:
            raise TargetChanged(
                "the instance the caller supplied does not match the host facts at that location",
                detail={
                    "reason": "identity_mismatch",
                    "fields": differences,
                    "expected": dict(expected_identity),
                    "observed": observed,
                },
            )
    if token_id:
        token = registry.get(token_id)  # unknown/expired -> TargetChanged
        differences = token.differences_against(facts)
        if differences:
            raise TargetChanged(
                "the supplied write-target token no longer matches the host facts",
                detail={
                    "reason": "target_facts_changed",
                    "fields": differences,
                    "target_token": token.redacted_id,
                },
            )


def _resolution_any(
    config: PluginConfig,
    instance: Any,
    *,
    require_writable: bool = False,
    tool: str = "plugin",
) -> tuple[Any, RS.ProfileResolution, ReaperHostBridge, TargetTokenRegistry, str]:
    """Resolve the instance on the provider path, or on the discovery path.

    The provider path is tried first and unchanged: a configured provider that
    claims the FX serves it exactly as before. The **discovery path** answers the
    cases the provider path cannot - no provider configured, or none of them
    claims this FX - by materializing the instance's prepared discovery artifact
    (P26's `plugin_prepare`) into a profile-shaped view (P28).

    Two boundaries are deliberate:

    * a caller who **names** a provider (`instance.provider`) gets that
      provider's refusal - there is no silent switch to another path, because a
      named provider is a statement about which plugin the caller means;
    * the discovery path never sweeps the plugin: with no usable artifact it
      refuses with `DiscoveryArtifactMissing` and the `plugin_prepare` remedy,
      since an unmeasured plugin has no verified addresses to write.
    """
    try:
        provider, resolution, bridge, registry = _resolution(
            config, instance, require_writable=require_writable, tool=tool
        )
        return provider, resolution, bridge, registry, "provider"
    except (UnsupportedProvider, InstanceNotFound) as exc:
        if isinstance(instance, dict) and instance.get("provider"):
            # A caller who NAMED a provider gets that provider's refusal: a named
            # provider is a statement about which plugin they mean, so there is no
            # silent switch to the generic path.
            raise
        if isinstance(exc, UnsupportedProvider) and exc.detail.get("variable") != PROVIDERS_ENV:
            # Several providers are configured and the instance names none: that
            # ambiguity is the caller's to resolve (P24's multi-provider rule),
            # and it is not the "no provider exists for this plugin" case the
            # generic path answers.
            raise
        from . import host_scope as HS  # noqa: PLC0415 - lazy: keeps registration import-light

        selector, expected_identity, token_id = _instance_claims(instance)
        bridge = session_bridge(config)
        registry = session_registry()
        if require_writable:
            _require_continuity_basis(token_id, registry, tool=tool)
        provider, resolution, _artifact = HS.discovery_resolution(
            bridge, selector=selector, registry=registry
        )
        if require_writable:
            resolution.require_writable()
        _check_instance_claims(
            resolution,
            expected_identity=expected_identity,
            token_id=token_id,
            registry=registry,
        )
        return provider, resolution, bridge, registry, "discovery"


def _requires_provider(source: str, tool: str, channel: str) -> None:
    """Refuse a channel the discovery path cannot honour, before any host work."""
    if source != "discovery":
        return
    raise UnsupportedProvider(
        f"{tool} needs the {channel} channel, which only a provider codec can read or write; "
        "this instance is served by the providerless discovery path",
        detail={
            "reason": "channel_unavailable_without_provider",
            "channel": channel,
            "tool": tool,
            "profile_source": source,
            "remedy": (
                "configure a provider for this plugin, or use the parameter channel "
                "(plugin_read / plugin_find / plugin_write on controls)"
            ),
        },
    )


def _resolution(
    config: PluginConfig,
    instance: Any,
    *,
    require_writable: bool = False,
    tool: str = "plugin",
) -> tuple[Any, RS.ProfileResolution, ReaperHostBridge, TargetTokenRegistry]:
    """Resolve one instance and its profile (cache-aware), optionally gating writes."""
    provider = _provider_for(config, instance)
    bridge = session_bridge(config)
    registry = session_registry()
    cache = ProfileCache(
        provider_id=str(provider.id),
        directory=config.cache_dir,
        env=config.env,
    )
    selector, expected_identity, token_id = _instance_claims(instance)
    if require_writable:
        # Before the first host call of this request: a token is a continuity
        # claim, and the layer has no provable basis for one (Decision 6b).
        _require_continuity_basis(token_id, registry, tool=tool)
    try:
        resolution = RS.resolve_instance_profile(
            bridge,
            provider,
            selector=selector,
            registry=registry,
            cache=cache,
        )
    except NotConnected:
        raise
    if require_writable:
        resolution.require_writable()
    _check_instance_claims(
        resolution,
        expected_identity=expected_identity,
        token_id=token_id,
        registry=registry,
    )
    return provider, resolution, bridge, registry


def _meta(resolution: RS.ProfileResolution, *, source: str = "provider") -> dict[str, Any]:
    """The sanctioned live-response metadata (cache, provisional flag, token).

    ``identity_basis`` is part of the honesty contract: it states what the target
    token validated and what it explicitly does **not** claim. ``startup``
    carries the one-shot preload summary so a caller can see whether the process
    started with prepared profiles loaded - and that a warm call reused them
    rather than researching the plugin again. ``profile_source`` names which of
    the two paths answered: a configured *provider*'s profile, or the
    *discovery* path's materialization of a prepared artifact - they have
    different powers (the state channel exists only on the provider path), so a
    caller must be able to tell them apart without guessing from the fields.
    """
    from . import preload as _preload  # noqa: PLC0415 - lazy: keeps registration import-light

    meta = resolution.meta()
    meta["instance"] = _ref_dict(resolution.instance.ref)
    meta["target_token"] = resolution.instance.token.id
    meta["identity_basis"] = RS.identity_basis()
    meta["profile_key"] = resolution.build.key_for(
        str(resolution.instance.ref.provider), resolution.identity
    )
    meta["profile_source"] = source
    if source == "discovery":
        from . import host_scope as HS  # noqa: PLC0415 - lazy import
        from .discovery.index import index_census  # noqa: PLC0415 - lazy import

        meta["index"] = index_census()
        meta["materialization"] = HS.materialization_stats()
        meta["artifact"] = HS.artifact_stats()
    else:
        report = _preload.prepared_profiles().report
        if report is not None:
            meta["startup"] = report.summary()
    return meta


def _ref_dict(ref: PluginInstanceRef) -> dict[str, Any]:
    return {
        "host": ref.host,
        "location": dict(ref.location),
        "provider": ref.provider,
        "identity": dict(ref.identity),
        "display": ref.display,
    }


def _control_by_name(profile: dict[str, Any], name: Any) -> dict[str, Any] | None:
    """Resolve a declared control name against the profile's own declared names.

    The comparison is the same one the live binding check uses (case- and
    whitespace-insensitive equality against `label`/`id`/`aliases`/`state_key`),
    so a name that resolves here is a name the profile really declares.
    """
    wanted = normalise_name(name)
    for control in profile.get("controls") or []:
        if wanted in declared_names(control):
            return control
    return None


def _suggestions(profile: dict[str, Any], name: Any, limit: int = 3) -> list[str]:
    wanted = normalise_name(name)
    tokens = [token for token in wanted.replace("-", "_").split("_") if token]
    scored: list[tuple[int, str]] = []
    for control in profile.get("controls") or []:
        control_id = str(control.get("id", ""))
        score = sum(1 for token in tokens if token and token in control_id.lower())
        if score:
            scored.append((score, control_id))
    scored.sort(key=lambda row: (-row[0], row[1]))
    return [control_id for _score, control_id in scored[:limit]]


# ---------------------------------------------------------------------------
# plugin_instances
# ---------------------------------------------------------------------------


def plugin_instances(
    *,
    config: Any = None,
    provider: str | None = None,
    selector: dict[str, Any] | None = None,
    include_identity: bool = True,
    scope: str = "provider",
    include_readiness: bool = False,
    **_: Any,
) -> dict[str, Any]:
    from .tools import ok_envelope

    mode = str(scope or "provider").strip().lower()
    if mode not in ("provider", "project"):
        raise InvalidValue(
            "`scope` must be 'provider' or 'project'",
            detail={"argument": "scope", "value": scope, "accepted": ["provider", "project"]},
        )
    resolved_config = _config(config)
    named_scope_only = bool(provider) and str(provider).strip().lower() in (HS.SCOPE_ID, "host", "reaper")
    if mode == "project":
        return _project_instances(
            resolved_config,
            selector=selector,
            include_identity=include_identity,
            include_readiness=include_readiness,
        )
    if resolved_config.provider_ids:
        providers = _providers(resolved_config)
    elif named_scope_only or selector:
        # Measured live (P28 stage G): this tool refused with zero providers even
        # when the caller had *named the slot*, so the host-scope block below was
        # unreachable and a providerless plugin could not be discovered by a
        # caller that knew exactly which track/slot it meant. An enumeration needs
        # a vocabulary (and still refuses without one); a selector does not, so a
        # selected slot is answered from the host-scope path instead.
        providers = []
    else:
        raise UnsupportedProvider(
            f"no plugin provider is configured; set {PROVIDERS_ENV} to enable one",
            detail={
                "variable": PROVIDERS_ENV,
                "configured": [],
                "known_providers": list(provider_registry.known_provider_ids()),
                "remedy": (
                    "pass a selector (track_index/fx_index) to ask about one slot, which the "
                    "host-scope path answers from its prepared artifact, or configure a provider"
                ),
            },
        )
    if provider and providers:
        wanted = str(provider).strip().lower()
        providers = [
            item for item in providers if str(getattr(item, "id", "")).strip().lower() == wanted
        ]
        if not providers:
            raise UnsupportedProvider(
                f"the provider {provider!r} is not configured",
                detail={"configured": list(resolved_config.provider_ids), "provider": provider},
            )
    bridge = session_bridge(resolved_config)
    registry = session_registry()
    instances: list[dict[str, Any]] = []
    tokens: list[str] = []
    for item in providers:
        for resolved in RS.resolve_instances(bridge, item, selector=selector, registry=registry):
            ref = _ref_dict(resolved.ref)
            if not include_identity:
                ref.pop("identity", None)
            instances.append(ref)
            tokens.append(resolved.token.id)
    ambiguous = len(instances) > 1
    data = {"instances": instances, "ambiguous": ambiguous, "refused": []}
    # A slot no configured provider claims is still a slot a caller can address -
    # and the providerless path can serve it from its prepared artifact. It is
    # reported SEPARATELY (`host_scope`), never mixed into `instances`: those are
    # instances of a configured provider, and blurring the two would make a
    # caller's `provider` claim hold for a plugin no such provider matched.
    if selector and (provider is None or str(provider).strip().lower() == HS.SCOPE_ID):
        data["host_scope"] = _host_scope_candidates(bridge, selector, include_identity=include_identity)
        if len(data["host_scope"]) == 1 and not instances:
            data["ambiguous"] = False
    meta: dict[str, Any] = {
        "configured_providers": list(resolved_config.provider_ids),
        "selector": dict(selector or {}),
        "checked_utc": utc_now_iso(),
        "identity_basis": RS.identity_basis(),
        "profile_source": "provider" if instances else ("discovery" if data.get("host_scope") else "none"),
    }
    warnings: list[dict[str, Any]] = []
    if not resolved_config.provider_ids:
        warnings.append(
            {
                "code": "NoProviderConfigured",
                "message": (
                    f"no plugin provider is configured ({PROVIDERS_ENV} is unset); only the "
                    "selected slot's host-scope readiness can be reported"
                ),
                "detail": {"variable": PROVIDERS_ENV, "instances_source": "host_scope"},
            }
        )
    if len(tokens) == 1:
        # the one target's session-scoped token, so a caller can address it
        meta["target_token"] = tokens[0]
    return ok_envelope("plugin_instances", data, warnings=warnings, meta=meta)


def _project_instances(
    config: Any,
    *,
    selector: dict[str, Any] | None,
    include_identity: bool,
    include_readiness: bool = False,
) -> dict[str, Any]:
    """`plugin_instances(scope="project")`: every loaded FX, provider or not.

    The enumeration is the host's (`HS.enumerate_project`): one track-list read
    plus one facts read per slot, which is the cost of *listing* rather than of
    addressing one instance - so it is an explicit scope, never the default, and
    the answer reports what it scanned. Rows carry the provider that claims each
    slot (or `host-scope` when none does), so a mixed project is one answer with a
    per-row truth instead of two half-answers.

    `ambiguous` keeps its historical meaning - "this answer holds more than one
    candidate" - which for a full listing is the normal case; a caller who wants a
    single target passes a selector and reads the same flag.
    """
    from .tools import ok_envelope

    bridge = session_bridge(config)
    providers = _providers(config) if config.provider_ids else []
    registry = session_registry()
    listed = HS.enumerate_project(
        bridge,
        selector=selector,
        providers=providers,
        include_readiness=bool(include_readiness),
    )
    instances: list[dict[str, Any]] = []
    for row in listed["instances"]:
        item = dict(row)
        if not include_identity:
            item.pop("identity", None)
        instances.append(item)
    data = {
        "instances": instances,
        "ambiguous": len(instances) > 1,
        "refused": list(listed["refused"]),
        "scope": "project",
        "slots_scanned": listed["slots_scanned"],
        "tracks_scanned": listed["tracks_scanned"],
        "readiness_included": listed["readiness_included"],
    }
    warnings: list[dict[str, Any]] = []
    if listed["refused"]:
        warnings.append(
            {
                "code": "SlotsUnreadable",
                "message": (
                    f"{len(listed['refused'])} slot(s) have an index whose identity the host could "
                    "not read; they are reported under `refused` and offered as no target"
                ),
                "detail": {"count": len(listed["refused"])},
            }
        )
    if not config.provider_ids:
        warnings.append(
            {
                "code": "NoProviderConfigured",
                "message": (
                    f"no plugin provider is configured ({PROVIDERS_ENV} is unset); every slot is "
                    "reported from the host's own facts, and a slot with a prepared artifact is "
                    "readable and writable through the host-scope path"
                ),
                "detail": {"variable": PROVIDERS_ENV, "configured": []},
            }
        )
    meta: dict[str, Any] = {
        "configured_providers": list(config.provider_ids),
        "selector": dict(selector or {}),
        "checked_utc": utc_now_iso(),
        "identity_basis": RS.identity_basis(),
        "profile_source": "host_facts",
        "enumerated_by": "host_bridge",
        "note": (
            "each row is the object `plugin_describe`/`plugin_read`/`plugin_write` accept as their "
            "`instance`; `identity` is the fingerprint those calls re-verify"
        ),
        "registry_size": len(getattr(registry, "tokens", {}) or {}),
    }
    return ok_envelope("plugin_instances", data, warnings=warnings, meta=meta)


def _host_scope_candidates(
    bridge: ReaperHostBridge, selector: dict[str, Any], *, include_identity: bool = True
) -> list[dict[str, Any]]:
    """Read-only: the slots a selector names, with their artifact readiness.

    Only the selector's own slot(s) are read (the enumeration narrows to an
    addressed track/slot), so this costs one track-state read per candidate and
    cannot degenerate into a project sweep. A host that refuses the enumeration
    yields an empty list: this is an *addition* to the answer, and a failure to
    add it must not turn a working `plugin_instances` into an error.
    """
    rows: list[dict[str, Any]] = []
    try:
        found = RS.iter_instances(bridge, HS.HostScope(), selector=selector)
    except PluginError:
        return rows
    for facts, _track_name in found:
        identity = HS.HostScope().identify(facts)
        artifact = HS.load_artifact(identity)
        row: dict[str, Any] = {
            "host": "reaper",
            "location": RS.facts_location(facts),
            "provider": HS.SCOPE_ID,
            "display": f"{facts.fx_name} @ track {facts.track_index + 1} fx {facts.fx_index + 1}",
            "artifact": {
                "status": artifact.status,
                "mode": artifact.document.get("mode"),
                "written_utc": artifact.document.get("written_utc"),
                "path": artifact.path,
                "reason": artifact.reason,
            },
            "ready": artifact.ok,
            "remedy": (
                None
                if artifact.ok
                else "call plugin_prepare with this instance (mode='prepare') to measure it once"
            ),
        }
        if include_identity:
            row["identity"] = identity
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# plugin_describe / plugin_find
# ---------------------------------------------------------------------------


def _module_rows(profile: dict[str, Any], *, full: bool) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for module in profile.get("modules") or []:
        row = {
            "id": module.get("id"),
            "kind": module.get("kind"),
            "label": module.get("label"),
            "parent": module.get("parent"),
            "n_controls": len(module.get("controls") or []),
        }
        if full:
            row["controls"] = list(module.get("controls") or [])
        rows.append(row)
    return rows


def _counts(profile: dict[str, Any]) -> dict[str, int]:
    controls = list(profile.get("controls") or [])
    return {
        "controls": len(controls),
        # `writable` means what a caller can actually write: a parameter-channel
        # control *with a verified conversion*. A control whose law is `none`
        # (a constant display, an unmeasurable one) holds write authority and
        # still cannot be written - counting it as writable would overstate the
        # capability (P28).
        "writable": sum(
            1
            for row in controls
            if (row.get("authority") or {}).get("write") == "parameter"
            and str((row.get("law") or {}).get("kind")) not in ("none", "unmeasured")
        ),
        "enums": sum(1 for row in controls if row.get("type") == "enum"),
        "state_only": sum(1 for row in controls if (row.get("authority") or {}).get("write") == "state"),
        "no_law": sum(
            1 for row in controls if ((row.get("law") or {}).get("kind") in ("none", "unmeasured"))
        ),
        "indexed": len(indexed_controls(profile)),
    }


def _sampled_points(result: Any, *, limit: int = 65) -> dict[str, dict[str, Any]]:
    """The measured (position, text) pairs of a targeted run, per control index.

    A row is included only when the run actually sampled it: a control the run did
    not touch has no observation, and inventing an empty list would read as "this
    control was measured and printed nothing".
    """
    out: dict[str, dict[str, Any]] = {}
    for observation in getattr(result, "observations", None) or []:
        index = getattr(observation, "index", None)
        samples = getattr(observation, "samples", None) or []
        if index is None or not samples:
            continue
        out[str(int(index))] = {
            "name": getattr(observation, "name", None),
            "status": getattr(observation, "status", None),
            "n_samples": len(samples),
            "points": [
                {
                    "normalized": round(float(sample.normalized), 12),
                    "text": str(sample.text),
                }
                for sample in sorted(samples, key=lambda item: item.normalized)[:limit]
            ],
        }
    return out


def plugin_describe(
    *, config: Any = None, instance: Any = None, detail: str = "summary", **_: Any
) -> dict[str, Any]:
    from .tools import ok_envelope

    resolved_config = _config(config)
    provider, resolution, _bridge, _registry, source = _resolution_any(resolved_config, instance)
    profile = resolution.profile
    modulation = profile.get("modulation") or {}
    data = {
        "identity": dict(resolution.identity),
        "profile": {
            "key": resolution.build.key_for(str(provider.id), resolution.identity),
            "schema": profile.get("profile_schema_version"),
            "source": resolution.cache.source,
            "provider_revision": resolution.build.provider_revision,
            "compatible_versions": list((profile.get("identity") or {}).get("compatible_versions") or []),
        },
        "modules": _module_rows(profile, full=str(detail) == "full"),
        "counts": _counts(profile),
        "routes": {
            "supported": bool(modulation.get("supported")),
            "identity": modulation.get("identity"),
            "slots": modulation.get("slots"),
            "active": None,
            "reason": modulation.get("reason"),
        },
        "state": {
            "codec": (profile.get("state") or {}).get("codec"),
            "derived_leaves": list((profile.get("state") or {}).get("derived_leaves") or []),
            "structural_keys": list((profile.get("state") or {}).get("structural_keys") or []),
            "reason": (profile.get("state") or {}).get("reason"),
        },
        "limits": list(profile.get("limits") or []),
        "warnings": [],
    }
    if source == "discovery":
        # What a caller needs to know *before* acting on this answer: which
        # measurement it came from, how old it is, and which channels exist.
        artifact = dict((profile.get("evidence") or {}).get("artifact") or {})
        data["discovery"] = {
            **artifact,
            "searchable": True,
            "writable_controls": int((profile.get("counts") or {}).get("writable") or 0),
            "controls_without_law": int((profile.get("counts") or {}).get("law_none") or 0),
            "write_blocker_note": (
                "a control whose law could not be verified against the artifact's samples is "
                "readable and refused for writing (a typed NoConversion), never guessed"
            ),
        }
    return ok_envelope("plugin_describe", data, meta=_meta(resolution, source=source))


def plugin_find(
    *,
    config: Any = None,
    instance: Any = None,
    query: str | None = None,
    group: str | None = None,
    kind: str = "control",
    limit: int = 12,
    include_capability: bool = False,
    **_: Any,
) -> dict[str, Any]:
    from .tools import ok_envelope

    resolved_config = _config(config)
    provider, resolution, _bridge, _registry, source = _resolution_any(resolved_config, instance)
    profile = resolution.profile
    controls = list(profile.get("controls") or [])
    modules = list(profile.get("modules") or [])
    kind = str(kind or "control")
    limit = max(1, int(limit))
    index = getattr(provider, "index", None)
    # A musical request names several functions at once ("oscillator pitch
    # envelope filter distortion level"), so the query is split into terms. One
    # term keeps the historical substring semantics exactly; several terms rank
    # by how many of them a control answers, and a term that matches nothing is
    # reported as unmatched instead of being dropped silently.
    terms = [token for token in normalise_name(query).split(" ") if token] if query else []
    prebuilt: list[dict[str, Any]] | None = None
    unmatched_terms: list[str] = []
    term_suggestions: dict[str, list[str]] = {}
    ranking: str | None = None

    if group:
        module = next((row for row in modules if str(row.get("id")) == str(group)), None)
        if module is None:
            module = next(
                (row for row in modules if str(row.get("label") or "") == str(group)),
                None,
            )
        if module is None:
            raise InvalidValue(
                f"unknown group {group!r}",
                detail={"group": group, "groups": [row.get("id") for row in modules][:40]},
            )
        wanted_ids = [str(item) for item in (module.get("controls") or [])]
        matches = [row for row in controls if str(row.get("id")) in wanted_ids]
        groups = []
    elif kind == "module":
        matches = _module_rows(profile, full=bool(include_capability))
        groups = []
    elif kind == "route_vocab":
        _requires_provider(source, "plugin_find(kind=route_vocab)", "routes")
        modulation = profile.get("modulation") or {}
        matches = [
            {"kind": "source", "id": str(key)} for key in (modulation.get("sources") or [])[:limit]
        ] + [
            {"kind": "destination", "id": str(key)}
            for key in (modulation.get("destinations") or [])[:limit]
        ]
        groups = []
    elif index is not None and terms:
        # The discovery path answers from the artifact's index: role synonyms,
        # module tokens and an explicit per-match reason, with the tally of what
        # matched in total (a bounded answer must not hide a large match set).
        found = index.find(terms, limit=limit)
        # The index carries the ENGINE's tentative fit for each control, and the
        # materializer re-derives the law it will actually use with its own
        # preference rules - measured live (P28 stage G), ReaComp's `ratio` was
        # reported `law: "none"` in a search row while `plugin_read` answered a
        # display for it, i.e. the field a caller uses to decide "is this writable"
        # disagreed with the write path. The profile is the authority here.
        materialized = {
            str(control.get("id")): str((control.get("law") or {}).get("kind") or "none")
            for control in controls
        }
        matches = [
            {**row, "law": materialized.get(str(row.get("id")), row.get("law"))}
            for row in found["matches"]
        ]
        prebuilt = matches
        unmatched_terms = list(found["unmatched_terms"])
        term_suggestions = dict(found["suggestions"])
        ranking = "artifact_index"
        groups = []
    else:
        if terms:
            scored: list[tuple[int, int, dict[str, Any]]] = []
            for position, row in enumerate(controls):
                count, matched = _score_control(row, terms)
                if count:
                    scored.append((count, position, row))
            hits: dict[str, int] = {term: 0 for term in terms}
            for row in controls:
                for term in terms:
                    if _term_hits(row, term):
                        hits[term] += 1
            unmatched_terms = [term for term, count in hits.items() if count == 0]
            term_suggestions = {term: _suggestions(profile, term) for term in unmatched_terms}
            if len(terms) > 1:
                scored.sort(key=lambda item: (-item[0], item[1]))
                ranking = "term_coverage"
            matches = [row for _count, _position, row in scored]
        else:
            matches = list(controls)
        groups = [
            {"id": row.get("id"), "kind": row.get("kind"), "n_controls": len(row.get("controls") or [])}
            for row in modules
        ]

    def _entry(row: Mapping[str, Any]) -> dict[str, Any]:
        # The address is the field a caller acts on, and it must be in the row at
        # the same key the write/read rows use (`index`). Measured live (P28 stage
        # G): the index path emitted it ONLY inside `binding`, so a caller reading
        # `match.index` (the natural key, and the one `plugin_write`'s preview and
        # verification rows carry) got nothing - the batch then had no address to
        # use. `compact()` declares both; this projection keeps its promise.
        address = row.get("index")
        if address is None:
            address = (row.get("binding") or {}).get("index")
        entry = {
            "kind": str(row.get("type") or row.get("kind") or "control"),
            "id": row.get("id"),
            "label": row.get("label"),
            "index": address,
            "module": row.get("module"),
            "type": row.get("type"),
            "unit": row.get("unit"),
            "range": row.get("range"),
            "binding": {"index": address},
        }
        if row.get("name") is not None:
            entry["name"] = row["name"]
        for key in ("matched_terms", "why", "roles", "class", "confidence", "law", "members", "step"):
            if row.get(key) is not None:
                entry[key] = row[key]
        if row.get("enum_labels"):
            entry["enum_labels"] = list(row["enum_labels"])
        if row.get("enum_values"):
            entry["enum_values"] = [
                item.get("id") if isinstance(item, Mapping) else item for item in row["enum_values"]
            ]
        if include_capability:
            entry["aliases"] = list(row.get("aliases") or [])
            entry["authority"] = dict(row.get("authority") or {})
            entry["law_full"] = dict(row.get("law") or {})
            entry["evidence"] = dict(row.get("evidence") or {})
        return entry

    rows = [_entry(row) for row in matches[:limit]]
    data: dict[str, Any] = {
        "matches": rows,
        "n_matches": len(matches),
        "suggestions": _suggestions(profile, query) if query and not matches else [],
        "groups": groups,
        "terms": list(terms),
        "unmatched_terms": unmatched_terms,
        "term_suggestions": term_suggestions,
        "returned": len(rows),
        "truncated": len(matches) > len(rows),
        "profile_source": source,
    }
    if ranking:
        data["ranking"] = ranking
    return ok_envelope("plugin_find", data, meta=_meta(resolution, source=source))


def _term_hits(row: Mapping[str, Any], term: str) -> bool:
    """Whether one term matches a profile control (single-term search semantics)."""
    return any(term in name for name in declared_names(row)) or term in normalise_name(row.get("label"))


def _score_control(row: Mapping[str, Any], terms: Sequence[str]) -> tuple[int, list[str]]:
    matched = [term for term in terms if _term_hits(row, term)]
    return len(matched), matched


# ---------------------------------------------------------------------------
# plugin_read
# ---------------------------------------------------------------------------


def plugin_read(
    *,
    config: Any = None,
    instance: Any = None,
    controls: list[Any] | None = None,
    include_display: bool = True,
    **_: Any,
) -> dict[str, Any]:
    """Batched, index-verified current values. One host hop for every control."""
    from .tools import ok_envelope

    if not controls:
        raise InvalidValue("`controls` must name at least one control", detail={"controls": controls})
    resolved_config = _config(config)
    provider, resolution, bridge, _registry, source = _resolution_any(resolved_config, instance)
    profile = resolution.profile
    ref = resolution.instance.ref

    problems: list[dict[str, Any]] = []
    wanted: list[tuple[str, dict[str, Any], int]] = []
    for name in controls:
        control = _control_by_name(profile, name)
        if control is None:
            problems.append(
                {
                    "control": str(name),
                    "code": "UnsupportedControl",
                    "message": f"unknown control {name!r}",
                    "detail": {"suggestions": _suggestions(profile, name)},
                }
            )
            continue
        binding = control.get("binding") or {}
        index = binding.get("index")
        if isinstance(index, bool) or not isinstance(index, int):
            problems.append(
                {
                    "control": str(control.get("id")),
                    "code": "UnsupportedControl",
                    "message": "the control declares no parameter index, so it cannot be read",
                    "detail": {"reason": "index_missing"},
                }
            )
            continue
        wanted.append((str(control.get("id")), control, int(index)))

    values: dict[str, Any] = {}
    host_texts: dict[str, str | None] = {}
    if wanted:
        indices = [index for _cid, _control, index in wanted]
        names = bridge.param_names(ref, indices)
        raw = bridge.read_params(ref, indices)
        if len(names) != len(indices) or len(raw) != len(indices):
            raise SnapshotUnavailable(
                "the host returned a short read; a short read is not evidence",
                detail={"asked": len(indices), "names": len(names), "values": len(raw)},
            )
        if source == "discovery":
            # The plugin's OWN text for the current value (one batched hop). On this
            # path the fitted law is a *model* of the display, so the host formatter
            # is the authority: it is what a human would read, and what a caller
            # comparing a target against reality needs. It is also the only display
            # available for a control whose law could not be verified.
            formatted = bridge.format_params_normalized(
                ref, [(index, float(value)) for index, value in zip(indices, raw)]
            )
            host_texts = {
                index: text for index, text in zip(indices, formatted.get("texts") or [])
            }
        for (control_id, control, index), live_name, normalized in zip(wanted, names, raw):
            if live_name is None or normalise_name(live_name) not in declared_names(control):
                problems.append(
                    {
                        "control": control_id,
                        "code": "UnsupportedControl",
                        "message": "the host's parameter name at the declared index is not this control",
                        "detail": {
                            "index": index,
                            "observed_name": live_name,
                            "declared_names": sorted(declared_names(control)),
                            "reason": "name_mismatch",
                        },
                    }
                )
                continue
            value = {
                "normalized": float(normalized),
                "native": V.normalized_to_native(control, float(normalized)),
                "unit": control.get("unit"),
            }
            if source == "discovery":
                value["host_display"] = host_texts.get(index)
                value["display_source"] = "host_formatter"
            kind = str((control.get("law") or {}).get("kind") or "identity")
            if include_display and kind not in ("none", "unmeasured"):
                value["display"] = V.from_native(control, value["native"])
            elif include_display:
                value["display"] = None
                value["display_note"] = f"the control's law is {kind!r}: no display conversion exists"
            values[control_id] = value

    data = {"values": values, "problems": problems}
    return ok_envelope("plugin_read", data, meta=_meta(resolution, source=source))


# ---------------------------------------------------------------------------
# plugin_write
# ---------------------------------------------------------------------------


def plugin_write(
    *,
    config: Any = None,
    instance: Any = None,
    request: dict[str, Any] | None = None,
    dry_run: bool = True,
    **_: Any,
) -> dict[str, Any]:
    from .tools import error_envelope, ok_envelope

    if not isinstance(request, dict) or not request:
        raise InvalidValue("`request` must be a non-empty object", detail={"request": request})
    # strict, and before any host contact: `0` from a client means "do not apply"
    enabled = V.strict_bool(dry_run, "dry_run")
    resolved_config = _config(config)
    provider, resolution, bridge, registry, source = _resolution_any(
        resolved_config, instance, require_writable=True, tool="plugin_write"
    )
    body = {key: value for key, value in request.items() if key != "label"}
    label = str(request.get("label") or "plugin_write")
    transaction = Transaction(
        provider=provider,
        codec=provider.codec(),
        bridge=bridge,
        profile=resolution.profile,
        ref=resolution.instance.ref,
        request=body,
        token=resolution.instance.token,
        registry=registry,
        dry_run=enabled,
        label=label,
    )
    result = transaction.run()
    meta = _meta(resolution, source=source)
    data = {
        "instance": _ref_dict(resolution.instance.ref),
        "dry_run": enabled,
        **result.data,
    }
    _gaps_from_outcome(result.data, data)
    if result.plan is not None:
        data.setdefault(
            "plan",
            {
                "label": result.plan.label,
                "n_items": len(result.plan.changes),
                "items": [
                    {
                        "subject": item.subject,
                        "channel": item.channel,
                        # `from_` is the value the plan was built against - the live
                        # baseline read inside THIS call (plan.py builds it from
                        # `live.parameter_readback`). Exposing it lets a caller see
                        # what a dry run is planning to change FROM, which is how an
                        # external change between calls becomes visible in the
                        # preview (the P18 freshness probe asserts exactly that).
                        "from": item.from_,
                        "to": item.to,
                        # Which space `from`/`to` are stated in (P29): a plan item
                        # for a law-less control is a *normalized* pair, and the
                        # key must not be guessed from the value's shape.
                        "value_space": item.value_space,
                    }
                    for item in result.plan.changes
                ],
                "write_order": list(result.plan.write_order),
            },
        )
    if result.ok:
        return ok_envelope("plugin_write", data, meta=meta, outcome=result.data.get("outcome"))
    error = result.error or PluginError("the write did not complete")
    if isinstance(error, InvalidValue) and result.data.get("problems"):
        data["problems"] = result.data["problems"]
    return error_envelope(
        "plugin_write", error, meta=meta, outcome=result.data.get("outcome")
    )


# ---------------------------------------------------------------------------
# plugin_state (inspect only in V1)
# ---------------------------------------------------------------------------


def plugin_state(
    *,
    config: Any = None,
    instance: Any = None,
    op: str = "inspect",
    include: list[str] | None = None,
    **_: Any,
) -> dict[str, Any]:
    from .tools import ok_envelope

    if str(op or "inspect") != "inspect":
        raise InvalidValue(
            f"unknown state op {op!r}; V1 exposes inspection only (snapshot/restore are deferred)",
            detail={"op": op, "accepted": ["inspect"]},
        )
    resolved_config = _config(config)
    provider, resolution, bridge, _registry, source = _resolution_any(resolved_config, instance)
    _requires_provider(source, "plugin_state", "state/document")
    ref = resolution.instance.ref
    track_index, fx_index = ref.location["track_index"], ref.location["fx_index"]
    facts = bridge.fx_facts(track_index, fx_index)
    chunk = bridge.read_state_chunk(ref)
    slice_ = bridge.locate_state_slice(ref, facts, chunk)
    document = provider.codec().decode(slice_)
    routes = list(document.get("routes") or [])
    settings = document.get("settings") or {}
    warnings: list[dict[str, Any]] = []
    structural_keys = list((resolution.profile.get("state") or {}).get("structural_keys") or [])
    wanted = [str(item) for item in (include or [])] or structural_keys
    inventory = {
        key.split(".")[-1]: {
            "present": key.split(".")[-1] in settings,
            "value_type": type(settings.get(key.split(".")[-1])).__name__,
        }
        for key in wanted
    }
    data = {
        "state": {
            "settings_keys": len(settings),
            "routes_total": len(routes),
            "active_routes": [row for row in routes if row.get("destination")][:12],
            "n_active_routes": sum(1 for row in routes if row.get("destination")),
            "document_keys": sorted((document.get("document") or {}).keys()),
            "provider_fields": {
                key: document.get(key)
                for key in ("synth_version", "preset_name")
                if key in document
            },
        },
        "structural_keys": structural_keys,
        "inventory": inventory,
        "counts": {"settings": len(settings), "routes": len(routes)},
        "slice": {
            "header": slice_.header_line,
            "class_hex": slice_.class_hex,
            "ordinal": slice_.ordinal,
            "line_start": slice_.line_start,
            "line_end": slice_.line_end,
            "block_digest": slice_.block_digest,
            "block_chars": len(slice_.block_text),
        },
    }
    # The provider's optional read view (P30). A provider that declares `state_view`
    # answers with descriptor-shaped content a generic summary cannot express - which
    # table each oscillator reads (content ids, frame counts) is document state with no
    # parameter behind it, and the inventory does not carry it at all. Its failure must
    # not turn a readable state into an error, so it is reported as a warning row and
    # omitted.
    view_hook = getattr(provider, "state_view", None)
    if callable(view_hook):
        try:
            data["state_view"] = view_hook(document)
        except Exception as exc:  # noqa: BLE001 - an additive view must not fail the read
            warnings.append(
                {
                    "code": "state_view_failed",
                    "message": f"the provider's state view raised {type(exc).__name__}: {exc}",
                }
            )
    return ok_envelope("plugin_state", data, warnings=warnings, meta=_meta(resolution, source=source))


# ---------------------------------------------------------------------------
# plugin_modulation
# ---------------------------------------------------------------------------


def plugin_modulation(
    *,
    config: Any = None,
    instance: Any = None,
    op: str | None = None,
    routes: list[dict[str, Any]] | None = None,
    selector: dict[str, Any] | None = None,
    destination: str | None = None,
    depth: Any = None,
    force: bool = False,
    dry_run: bool = True,
    **_: Any,
) -> dict[str, Any]:
    if not op:
        raise InvalidValue("`op` is required", detail={"accepted": ["list", "create", "update", "remove", "depth"]})
    enabled = V.strict_bool(dry_run, "dry_run")
    forced = V.strict_bool(force, "force")
    if forced:
        # Refused BEFORE any host work: `force` promises a policy override that
        # V1 does not implement, and an argument that looks like a permission
        # must never be accepted while nothing honours it (Decision 4).
        raise UnsupportedOverride(
            "`force` is not supported: V1 implements no override for a policy refusal, "
            "so the request is refused before any host contact instead of being accepted "
            "and ignored",
            detail={
                "argument": "force",
                "supported": False,
                "op": str(op),
                "reason": "force_not_supported",
                "next": "fix the request, or read the live state and route it without a policy conflict",
            },
        )
    force = False
    resolved_config = _config(config)
    mutation = str(op) in ("create", "update", "remove")
    provider, resolution, bridge, registry, source = _resolution_any(
        resolved_config, instance, require_writable=mutation, tool=f"plugin_modulation:{op}"
    )
    _requires_provider(source, f"plugin_modulation:{op}", "routes/modulation")
    result = MOD.modulation(
        provider=provider,
        bridge=bridge,
        profile=resolution.profile,
        ref=resolution.instance.ref,
        token=resolution.instance.token,
        registry=registry,
        op=str(op),
        routes=routes,
        selector=selector,
        destination=destination,
        depth=depth,
        force=bool(force),
        dry_run=enabled,
    )
    envelope = result.to_envelope("plugin_modulation")
    envelope.setdefault("meta", {})
    envelope["meta"].update(_meta(resolution, source=source))
    envelope["meta"]["dry_run"] = enabled
    return envelope


# ---------------------------------------------------------------------------
# plugin_prepare (P26): the universal, provider-free discovery path
# ---------------------------------------------------------------------------


class _HostScopeProvider(HS.HostScope):
    """Historical name of the host-scope class (P26's `plugin_prepare` uses it).

    It is the same object as `host_scope.HostScope`: `plugin_prepare` needs a
    scope that claims every FX slot without plugin knowledge, and that scope is
    now shared with the providerless tool path rather than duplicated.
    """

    id = "host-scope"

    def matches(self, facts: Any) -> bool:
        return True

    def identify(self, facts: Any) -> dict[str, Any]:
        return {
            "fx_name": facts.fx_name,
            "class_id": facts.class_id,
            "module": facts.module,
            "param_count": facts.param_count,
        }


def plugin_prepare(
    *,
    config: Any = None,
    instance: Any = None,
    mode: str = "reuse",
    sample_indices: Any = None,
    max_controls: Any = None,
    grid_points: Any = None,
    refine_budget: Any = None,
    force: bool = False,
    include_controls: bool = False,
    **_: Any,
) -> dict[str, Any]:
    """Inventory and prepare an FX instance for discovery (read-only).

    Four modes, one of which is a pure lookup: ``reuse`` serves a stored prepared
    artifact when one exists (and otherwise runs the cheapest useful pass,
    ``inventory``), ``inventory`` reads host metadata only, ``prepare`` adds the
    display sweep, and ``deep`` adds adaptive refinement of the disputed
    boundaries. ``force`` ignores a stored artifact and measures again. A stored
    artifact answers a request only when its own mode is at least as strong as
    the requested one - a ``prepare``/``deep`` request that outranks the stored
    artifact re-measures and reports the replaced artifact as
    ``prepared.previous_artifact`` (rule ``mode_strength``). A **limited** run
    (``sample_indices``/``max_controls``) is returned in full but is never stored
    over a broader artifact for the identity (``store_refusal`` says so) - not even
    with ``force``: the broader artifact may be the only copy of measurements that
    cost many host calls.

    The host is only **read**: values, names, idents, step sizes, named-config
    entries and the display text of caller-supplied values. Nothing is written to
    the plugin, no preset is applied and no REAPER project is saved; the artifact
    this tool stores is local discovery data, not plugin state.
    """
    from .discovery import report as DISCOVERY_REPORT
    from .discovery.pipeline import DiscoveryEngine, EngineConfig
    from .discovery.store import PreparedStore
    from .tools import ok_envelope

    resolved_config = _config(config)
    bridge = session_bridge(resolved_config)
    selector, expected_identity, token_id = _instance_claims(instance)
    found = RS.iter_instances(bridge, _HostScopeProvider(), selector=selector)
    if not found:
        raise InstanceNotFound(
            "no FX slot matches the selector",
            detail={"selector": dict(selector or {}), "scope": "host"},
        )
    if len(found) > 1:
        raise AmbiguousInstance(
            "more than one FX slot matches; address one with track_index/fx_index",
            detail={"selector": dict(selector or {}), "matches": len(found)},
        )
    facts, _track_name = found[0]
    identity = _HostScopeProvider().identify(facts)
    if expected_identity:
        disagreements = [
            name
            for name in ("class_id", "fx_name", "module", "param_count")
            if name in expected_identity
            and expected_identity.get(name) is not None
            and str(expected_identity.get(name)) != str(identity.get(name))
        ]
        if disagreements:
            raise StaleInstance(
                "the addressed instance no longer matches the identity the caller supplied",
                detail={"fields": disagreements, "live": identity, "claimed": dict(expected_identity)},
            )
    ref = PluginInstanceRef(
        host="reaper",
        location=RS.facts_location(facts),
        provider=_HostScopeProvider.id,
        identity=identity,
        display=f"{facts.fx_name} @ track {facts.track_index + 1} fx {facts.fx_index + 1}",
    )

    requested_mode = str(mode or "reuse").strip().lower()
    if requested_mode not in ("reuse", "inventory", "prepare", "deep"):
        raise InvalidValue(
            f"unknown prepare mode {mode!r}",
            detail={"accepted": ["reuse", "inventory", "prepare", "deep"]},
        )
    store = PreparedStore()
    artifact = None
    stored_candidate: Any = None
    refused_before: dict[str, Any] | None = None
    build_mode = "inventory" if requested_mode == "reuse" else requested_mode
    # The store is read even under `force`: its state is what the limited-run guard
    # below has to see (a force-run must not silently replace a broader artifact
    # with a narrower measurement - found by the P27 independent review, F-C4).
    candidate = store.load(identity)
    stored_candidate = candidate
    if not force:
        if candidate.ok:
            stored_mode = str(candidate.document.get("mode") or "inventory")
            if _MODE_RANK.get(requested_mode, 0) > _MODE_RANK.get(stored_mode, 0):
                # A weaker artifact is not an answer to a stronger request: the
                # declared semantics rank the modes, so `prepare` must include the
                # display sweep and `deep` the refinement. The artifact is replaced
                # by the requested measurement, and the replacement is reported.
                refused_before = {
                    "status": "not_used",
                    "rule": "mode_strength",
                    "reason": (
                        f"the stored artifact was measured in mode {stored_mode!r}, weaker than "
                        f"the requested {requested_mode!r}; it is replaced by the requested measurement"
                    ),
                    "path": candidate.path,
                }
            else:
                artifact = candidate
        elif candidate.rule is not None:
            # A stored artifact that cannot be used is reported with the new run,
            # never quietly replaced: a caller has to be able to see that the
            # measurement it is about to trust is not the one that was stored.
            refused_before = {
                "status": candidate.status,
                "rule": candidate.rule,
                "reason": candidate.reason,
                "path": candidate.path,
            }
    if artifact is not None:
        payload = artifact.payload()
        data = _discovery_view(payload, include_controls=bool(include_controls))
        data["prepared"] = {
            **artifact.meta(),
            "status": "served",
            "requested_mode": requested_mode,
            "note": "served from the prepared artifact: no host call, no measurement",
        }
        meta = {"host_calls": 0, "cache": "not_used", "instance": _ref_dict(ref)}
        if token_id:
            meta["target_token"] = str(token_id)
        return ok_envelope("plugin_prepare", data, meta=meta)

    engine_config = EngineConfig(
        mode=build_mode,
        grid_points=int(grid_points) if grid_points is not None else 17,
        refine_budget=int(refine_budget) if refine_budget is not None else 0,
        max_controls=int(max_controls) if max_controls is not None else None,
        sample_indices=list(sample_indices) if sample_indices else None,
    ).resolved()
    result = DiscoveryEngine(bridge=bridge).run(ref, facts, config=engine_config)
    # P27: a *limited* run (`sample_indices` / `max_controls`) covers part of the
    # identity, and the artifact it would write sits at the identity's path. It must
    # not replace a broader stored artifact: that file may be the only copy of
    # measurements that cost many host calls (the P26 lesson: a run that overwrote
    # the way back destroyed the record of the state it measured). The measurement
    # is still returned in full - only the *store* refuses - and the refusal is
    # reported, never silent.
    limited_run = bool(engine_config.sample_indices) or engine_config.max_controls is not None
    broader_stored = bool(stored_candidate is not None and stored_candidate.ok) and not _run_was_limited(
        stored_candidate.document
    )
    store_refusal: dict[str, Any] | None = None
    if limited_run and broader_stored:
        store_refusal = {
            "status": "not_stored",
            "rule": "limited_run_would_replace_a_broader_artifact",
            "reason": (
                "this run is limited (sample_indices/max_controls) while the stored artifact for "
                "this identity is a full measurement; the broader measurement was left in place"
            ),
            "path": str(store.path_for(identity)),
        }
        saved: dict[str, Any] = {}
    else:
        saved = store.save(result, identity=identity)
    payload = result.to_dict(with_observations=False, with_inventory=True)
    data = _discovery_view(payload, include_controls=bool(include_controls))
    data["prepared"] = {
        "status": "measured",
        "requested_mode": requested_mode,
        "mode": build_mode,
        "engine_version": result.engine_version,
        "identity_key": saved.get("identity_key"),
        "path": saved.get("path"),
        "size": saved.get("size"),
        "sha256_matches_disk": saved.get("sha256_matches_disk"),
        "observations": saved.get("observations"),
        "stored": store_refusal is None,
        "note": (
            "measured now and stored as a prepared artifact; a later call with mode='reuse' serves it"
            if store_refusal is None
            else "measured now; NOT stored - the broader stored artifact for this identity was kept"
        ),
    }
    if store_refusal is not None:
        data["prepared"]["store_refusal"] = store_refusal
    if limited_run:
        # P29: a targeted run's whole point is the *measurements*, and the stored
        # artifact is deliberately not replaced - so the points have to travel with
        # the answer or the refinement is unusable through the tool (measured live:
        # `mapping.params` carries only the point COUNT, so the numbers existed
        # nowhere the caller could read). Bounded to the controls the run asked for.
        data["sampled_points"] = _sampled_points(result, limit=65)
        data["sampled_note"] = (
            "the positions and the text the host printed at each of them, for the controls this "
            "targeted run measured; they are NOT written into the stored artifact for this "
            "identity (a limited run never replaces a broader one), so a caller keeps them "
            "where it needs them"
        )
    if refused_before is not None:
        data["prepared"]["previous_artifact"] = refused_before
    meta = {
        "host_calls": result.cost.total_calls,
        "cache": "not_used",
        "instance": _ref_dict(ref),
        "engine_version": result.engine_version,
        "mode": build_mode,
        "limits_hit": list(result.cost.limits_hit),
    }
    if token_id:
        meta["target_token"] = str(token_id)
    return ok_envelope("plugin_prepare", data, meta=meta)


def _run_was_limited(document: Mapping[str, Any]) -> bool:
    """Whether the stored artifact was produced by a limited run (P27).

    A limited run (``sample_indices`` / ``max_controls``) covers part of the
    identity's parameters; the budget the engine recorded is what says so.
    """
    budgets = document.get("payload") or {}
    budgets = budgets.get("cost") or {}
    budgets = budgets.get("budgets") or {}
    engine = budgets.get("engine") or {}
    return bool(engine.get("sample_indices")) or engine.get("max_controls") is not None


def _discovery_view(payload: Mapping[str, Any], *, include_controls: bool) -> dict[str, Any]:
    """The compact view an agent reads first: modules, then counts, then cost.

    The brief's requirement is that a caller can see the probable structure of a
    plugin *without* loading thousands of controls, so the module list is always
    present and the control list only on request.
    """
    modules = [
        {
            "id": node.get("id"),
            "label": node.get("label"),
            "kind": node.get("kind"),
            "status": node.get("status"),
            "confidence": node.get("confidence"),
            "parent": node.get("parent"),
            "n_controls": len(node.get("controls") or []),
            "signals": list(node.get("signals") or []),
        }
        for node in ((payload.get("modules") or {}).get("nodes") or [])
    ]
    controls = list(payload.get("controls") or [])
    classes: dict[str, int] = {}
    unknown_by_reason: dict[str, int] = {}
    for control in controls:
        kind = str((control.get("classification") or {}).get("kind"))
        classes[kind] = classes.get(kind, 0) + 1
        for row in control.get("unknown") or []:
            reason = str(row.get("reason"))
            unknown_by_reason[reason] = unknown_by_reason.get(reason, 0) + 1
    view: dict[str, Any] = {
        "identity": dict(payload.get("identity") or {}),
        "modules": modules,
        "module_signals": list((payload.get("modules") or {}).get("signals_used") or []),
        "classes": classes,
        "unknown_by_reason": unknown_by_reason,
        "counts": dict(payload.get("counts") or {}),
        "cost": dict(payload.get("cost") or {}),
        "formats": list(payload.get("formats") or []),
        "warnings": list(payload.get("warnings") or []),
    }
    if include_controls:
        view["controls"] = controls
    return view


# ---------------------------------------------------------------------------
# plugin_presets (the plugin's own preset library - P38)
# ---------------------------------------------------------------------------


def _plan_projection(plan: Any) -> dict[str, Any]:
    """The compact plan projection shared with plugin_write's response shape."""
    return {
        "label": plan.label,
        "n_items": len(plan.changes),
        "items": [
            {
                "subject": item.subject,
                "channel": item.channel,
                "from": item.from_,
                "to": item.to,
                "value_space": item.value_space,
            }
            for item in plan.changes
        ],
        "write_order": list(plan.write_order),
    }


def _gaps_from_outcome(result_data: dict[str, Any], data: dict[str, Any]) -> None:
    """Fold the transaction's own named gaps into the response, and mark it PARTIAL.

    MEASURED (2026-10-18, offline reproduction of a live batch): a wavetable the read ceiling
    refused produced `outcome.named_gaps = [{reason: state_ceiling_reached, path: ...}]` while the
    response carried `gaps: []`, `partial: false` and `verdict: ALL_VERIFIED`. The verdict is true
    about the units the transaction RAN - it is not a statement that every requested change
    arrived, and without this fold a caller cannot tell the difference. Two presets of a live
    family batch lost exactly that way: `ALL_VERIFIED`, no reason, and the oscillator kept its old
    table.

    Gaps already present (a preset's own plan gaps) are kept and de-duplicated, so the two lists
    read as one.
    """
    outcome = (result_data or {}).get("outcome")
    if not isinstance(outcome, dict):
        return
    named = [gap for gap in (outcome.get("named_gaps") or []) if isinstance(gap, dict)]
    if not named:
        return
    existing = [gap for gap in (data.get("gaps") or []) if isinstance(gap, dict)]
    seen = {(str(gap.get("reason")), str(gap.get("path"))) for gap in existing}
    added = 0
    for gap in named:
        key = (str(gap.get("reason")), str(gap.get("path")))
        if key in seen:
            continue
        seen.add(key)
        existing.append(
            {
                "channel": gap.get("channel"),
                "reason": gap.get("reason"),
                "path": gap.get("path"),
                "message": gap.get("detail"),
            }
        )
        added += 1
    data["gaps"] = existing
    reasons: dict[str, int] = {}
    for gap in existing:
        reason = str(gap.get("reason") or "unspecified")
        reasons[reason] = reasons.get(reason, 0) + 1
    data["partial"] = True
    data["write_gaps"] = {
        "from_outcome": added,
        "total": len(existing),
        "reasons": reasons,
        "note": (
            "the outcome's verdict covers the units the transaction RAN; these gaps are the "
            "requested changes that did not arrive, each with its reason"
        ),
    }


def _preset_carry_flag(plan_view: dict[str, Any], data: dict[str, Any]) -> None:
    """Mark a preset apply PARTIAL whenever any part of the file was not carried.

    The transaction's own `partial` covers what IT refused or lost; a preset apply can
    also leave parts out at PLAN time (a host-owned value, a guarded rule, a table or a
    curve the growth band refuses, a routing this build drops). In that case the live
    state is not the file, and the response must not read as "the preset is loaded" -
    each reason is counted here so a caller sees WHY without walking `gaps[]`.
    """
    gaps = list(plan_view.get("gaps") or [])
    if not gaps:
        return
    reasons: dict[str, int] = {}
    for gap in gaps:
        reason = str(gap.get("reason") or "unspecified")
        reasons[reason] = reasons.get(reason, 0) + 1
    data["partial"] = True
    data["preset_carry"] = {
        "gaps": len(gaps),
        "reasons": reasons,
        "note": (
            "the live state is not the preset file in every part: gaps[] names each part "
            "that was not carried, and the outcome covers what was written"
        ),
    }


def plugin_presets(
    *,
    config: Any = None,
    instance: Any = None,
    op: str | None = None,
    query: str | None = None,
    style: str | None = None,
    author: str | None = None,
    preset: str | None = None,
    limit: int = 20,
    offset: int = 0,
    name: str | None = None,
    bank: str | None = None,
    dry_run: bool = True,
    **_: Any,
) -> dict[str, Any]:
    """The plugin's NATIVE preset library: search / info / apply / save.

    The apply rides the SAME transaction machinery as any other write: the change
    set is built from the preset file and the live document, the codec plans it
    as measured growth-budgeted batches, and every unit is read back and
    verified before the next one is sent.
    """
    from .tools import error_envelope, ok_envelope

    operation = str(op or "").strip().lower()
    if operation not in ("search", "info", "apply", "save", "init"):
        raise InvalidValue(
            f"unknown preset op {op!r}",
            detail={"op": op, "accepted": ["search", "info", "apply", "save", "init"]},
        )
    if operation == "init":
        raise InvalidValue(
            "op='init' is not available yet: this build CONSTRUCTS its Init state in code (no "
            "init document ships), and no measured init artifact has been recorded for this "
            "provider. Load an init-style preset with op='apply' instead",
            detail={"op": "init", "reason": "init_artifact_not_recorded"},
        )
    enabled = V.strict_bool(dry_run, "dry_run")
    resolved_config = _config(config)
    provider, resolution, bridge, registry, source = _resolution_any(
        resolved_config,
        instance,
        require_writable=operation in ("apply", "save"),
        tool="plugin_presets",
    )
    _requires_provider(source, "plugin_presets", "the plugin's own preset library")
    hook = getattr(provider, "presets", None)
    capability = hook() if callable(hook) else None
    if capability is None:
        raise UnsupportedProvider(
            "this provider declares no preset capability",
            detail={"provider": str(getattr(provider, "id", "?")), "tool": "plugin_presets"},
        )
    meta = _meta(resolution, source=source)
    ref = resolution.instance.ref
    data: dict[str, Any] = {"op": operation, "instance": _ref_dict(ref)}

    if operation == "search":
        data.update(
            capability.search(query=query, style=style, author=author, limit=limit, offset=offset)
        )
        return ok_envelope("plugin_presets", data, meta=meta)
    if operation == "info":
        if not preset:
            raise InvalidValue(
                "op='info' needs `preset` (an id from op='search')", detail={"op": "info"}
            )
        data["preset"] = capability.info(str(preset))
        return ok_envelope("plugin_presets", data, meta=meta)

    track_index, fx_index = ref.location["track_index"], ref.location["fx_index"]
    facts = bridge.fx_facts(track_index, fx_index)
    chunk = bridge.read_state_chunk(ref)
    slice_ = bridge.locate_state_slice(ref, facts, chunk)
    document = provider.codec().decode(slice_)["document"]

    if operation == "save":
        if not name:
            raise InvalidValue(
                "op='save' needs `name` for the new preset file", detail={"op": "save"}
            )
        data["saved"] = capability.save(document, name=str(name), bank=bank)
        return ok_envelope("plugin_presets", data, meta=meta)

    # -- apply ---------------------------------------------------------------
    if not preset:
        raise InvalidValue(
            "op='apply' needs `preset` (an id from op='search')", detail={"op": "apply"}
        )
    guard_hook = getattr(provider, "guarded_state_paths", None)
    guarded = list(guard_hook(resolution.profile) or []) if callable(guard_hook) else []
    # A pair destination this build's plugin was measured to DROP (the slot reads back
    # empty) is skipped at plan time: writing it again would repeat an apply that the
    # plugin's own load discards anyway (measured P38 2026-10-17).
    live_version = str(document.get("synth_version") or "")
    known_drops: list[str] = []
    drops_hook = getattr(capability, "known_dropped_destinations", None)
    if callable(drops_hook):
        known_drops = list(drops_hook(live_version) or [])
    plan_view = capability.plan(
        document,
        str(preset),
        guarded_paths=guarded,
        dropped_destinations=known_drops,
    )
    data["preset"] = plan_view.get("preset")
    data["counts"] = plan_view.get("counts")
    data["gaps"] = plan_view.get("gaps") or []
    request_body: dict[str, Any] = {"state": dict(plan_view.get("paths") or {})}
    if plan_view.get("wavetables"):
        request_body["wavetable"] = list(plan_view["wavetables"])
    for key in ("routes_create", "routes_update", "routes_remove"):
        if plan_view.get(key):
            request_body[key] = list(plan_view[key])
    if not any(request_body.get(key) for key in ("state", "wavetable", "routes_create", "routes_update", "routes_remove")):
        data["note"] = "the live state already matches this preset: nothing to write"
        return ok_envelope("plugin_presets", data, meta=meta)
    transaction = Transaction(
        provider=provider,
        codec=provider.codec(),
        bridge=bridge,
        profile=resolution.profile,
        ref=ref,
        request=request_body,
        token=resolution.instance.token,
        registry=registry,
        dry_run=enabled,
        label="plugin_presets:apply",
    )
    result = transaction.run()
    data.update(result.data)
    _gaps_from_outcome(result.data, data)
    _preset_carry_flag(plan_view, data)
    if result.plan is not None:
        data.setdefault("plan", _plan_projection(result.plan))
    # Losing a pair to the plugin is a finding about THIS build, so it is remembered
    # (keyed by the live synth version) and named - the memo only ever makes a later
    # apply skip a routing the plugin would have dropped anyway.
    record_hook = getattr(capability, "record_dropped_destinations", None)
    if callable(record_hook):
        outcome_rows = (result.data.get("outcome") or {}) if isinstance(result.data.get("outcome"), dict) else {}
        freshly_dropped = list(result.data.get("dropped_routes") or [])
        if not freshly_dropped:
            freshly_dropped = [
                gap.get("route") or {}
                for gap in (outcome_rows.get("named_gaps") or [])
                if isinstance(gap, dict) and gap.get("reason") == "route_dropped_by_plugin"
            ]
        if freshly_dropped:
            try:
                data["dropped_destinations_recorded"] = record_hook(
                    freshly_dropped, synth_version=live_version, preset_id=str(preset)
                )
            except OSError as exc:  # a memo write must never fail an apply that landed
                data["dropped_destinations_recorded"] = {"error": f"{exc.__class__.__name__}: {exc}"}
    if result.ok:
        return ok_envelope("plugin_presets", data, meta=meta, outcome=result.data.get("outcome"))
    error = result.error or PluginError("the preset apply did not complete")
    if isinstance(error, InvalidValue) and result.data.get("problems"):
        data["problems"] = result.data["problems"]
    return error_envelope("plugin_presets", error, meta=meta, outcome=result.data.get("outcome"))


def plugin_wavetable(
    *,
    config: Any = None,
    instance: Any = None,
    op: str | None = None,
    oscillator: int | None = 1,
    table: dict[str, Any] | None = None,
    frames: int = 2,
    points: int = 8,
    dry_run: bool = True,
    **_: Any,
) -> dict[str, Any]:
    """Build, preview and read a wavetable - the SOUND DESIGN channel (P40).

    Three ops over one small object (the spec, documented in the tool's own description and in the
    provider's table module):

    * ``write`` - build the plugin's table from the spec and deliver it through the state channel
      that every other write uses: the growth bound, the balloon that opens room, the read-back
      verification and the rollback all apply unchanged, so a table that cannot be delivered is
      REFUSED with its reason rather than half-written;
    * ``preview`` - render the spec with no host at all: what each described frame's peak, RMS and
      first partials are, how many frames it makes, and what it will cost in state;
    * ``read`` - what the addressed slot holds NOW, in the same numbers, so "did my waveform
      arrive" is a comparison and not an opinion.

    The provider supplies the vocabulary (`WavetableCapability`); a provider without it is refused
    by name, and nothing plugin-specific lives in this layer.
    """
    from .tools import error_envelope, ok_envelope

    operation = str(op or "").strip().lower()
    if operation not in ("write", "read", "preview"):
        raise InvalidValue(
            f"unknown wavetable op {op!r}",
            detail={"op": op, "accepted": ["write", "read", "preview"]},
        )
    enabled = V.strict_bool(dry_run, "dry_run")
    resolved_config = _config(config)

    if operation == "preview":
        # host-free BY DESIGN: designing a waveform must not need a REAPER instance
        providers = _providers(resolved_config)
        if instance is None and len(providers) != 1:
            raise InvalidValue(
                "op='preview' with no instance needs exactly ONE configured provider: the spec's "
                "vocabulary is the provider's, and the response must not guess which one was meant",
                detail={
                    "providers": [str(getattr(provider, "id", "?")) for provider in providers],
                    "remedy": "pass `instance` (any resolved address) or configure one provider",
                },
            )
        provider = providers[0] if instance is None else _resolution_any(
            resolved_config, instance, tool="plugin_wavetable"
        )[0]
        capability = _wavetable_capability(provider)
        if table is None:
            raise InvalidValue(
                "op='preview' needs `table` (the spec)", detail={"op": "preview"}
            )
        data = capability.preview(table, frames=int(frames), points=int(points))
        data["op"] = operation
        return ok_envelope("plugin_wavetable", data, meta={"instance": None})

    provider, resolution, bridge, registry, source = _resolution_any(
        resolved_config,
        instance,
        require_writable=operation == "write",
        tool="plugin_wavetable",
    )
    _requires_provider(source, "plugin_wavetable", "the provider's table vocabulary")
    capability = _wavetable_capability(provider)
    meta = _meta(resolution, source=source)
    ref = resolution.instance.ref

    facts = bridge.fx_facts(ref.location["track_index"], ref.location["fx_index"])
    chunk = bridge.read_state_chunk(ref)
    slice_ = bridge.locate_state_slice(ref, facts, chunk)
    document = provider.codec().decode(slice_)["document"]

    if operation == "read":
        data = capability.read(
            document, oscillator=oscillator, frames=int(frames), points=int(points)
        )
        data["op"] = operation
        return ok_envelope("plugin_wavetable", data, meta=meta)

    if table is None:
        raise InvalidValue(
            "op='write' needs `table` (the spec: one of `shape`, `harmonics`, `samples`)",
            detail={"op": "write", "shapes": list(getattr(capability, "shapes", ()))},
        )
    live_version = document.get("synth_version") if isinstance(document, dict) else None
    built = capability.build(table, live_version=str(live_version) if live_version else None)
    path = _wavetable_slot_path(capability, oscillator)
    data: dict[str, Any] = {
        "op": operation,
        "oscillator": oscillator,
        "path": path,
        "described": built.get("described"),
        "dry_run": enabled,
        "shapes": list(getattr(capability, "shapes", ())),
    }
    transaction = Transaction(
        provider=provider,
        codec=provider.codec(),
        bridge=bridge,
        profile=resolution.profile,
        ref=ref,
        # The built table goes in as a whole-slot STATE write: the guard that judges a table, the
        # growth bound, the balloon, the read-back verification and the rollback all live on that
        # path, and a second way in would be a second set of rules. No `wavetable` spec travels
        # with it - that vocabulary says "take the table from this preset file", and this table was
        # BUILT here; the response's own `described` block is what a caller compares against.
        request={"state": {path: built["table"]}},
        token=resolution.instance.token,
        registry=registry,
        dry_run=enabled,
        label="plugin_wavetable:write",
    )
    result = transaction.run()
    data.update(result.data)
    _gaps_from_outcome(result.data, data)
    data["delivered"] = bool(result.ok) and not [
        gap for gap in data.get("gaps") or [] if str(gap.get("path")) == path
    ]
    if result.ok:
        return ok_envelope(
            "plugin_wavetable", data, meta=meta, outcome=result.data.get("outcome")
        )
    error = result.error or PluginError("the wavetable write did not complete")
    return error_envelope(
        "plugin_wavetable", error, meta=meta, outcome=result.data.get("outcome")
    )


def _wavetable_capability(provider: Any) -> Any:
    """The provider's table vocabulary, or a typed refusal naming the provider."""
    hook = getattr(provider, "wavetables", None)
    capability = hook() if callable(hook) else None
    if capability is None:
        raise UnsupportedProvider(
            "this provider declares no wavetable capability, so no table can be built for it",
            detail={"provider": str(getattr(provider, "id", "?")), "tool": "plugin_wavetable"},
        )
    return capability


def plugin_sample(
    *,
    config: Any = None,
    instance: Any = None,
    op: str | None = None,
    file_path: str | None = None,
    samples: Any = None,
    sample_rate: int | None = None,
    name: str | None = None,
    on: bool | None = True,
    loop: Any = None,
    destination: Any = None,
    level: Any = None,
    transpose: Any = None,
    tune: Any = None,
    pan: Any = None,
    keytrack: Any = None,
    dry_run: bool = True,
    **_: Any,
) -> dict[str, Any]:
    """Load and read the plugin's SAMPLER - the sample engine as a channel (P41).

    Two ops:

    * ``load`` - put a waveform into the sampler: ``file_path`` (a PCM WAV) or ``samples``
      (raw int16 values, with ``sample_rate``). The block is written through the SAME verified
      state channel as every other write - the landing bound, the sample balloon, the read-back
      verification and the rollback all apply - and the sampler's own controls travel in the
      same batch, so one call both loads the material and switches the engine on
      (``on`` defaults to true; that is the step a caller forgets);
    * ``read`` - what the sampler HOLDS now: name, length, sample_rate, channels, seconds,
      payload size, plus the current value of its controls.

    The provider supplies the vocabulary and the conversions (``SampleCapability``); a provider
    without the hook is refused by name, and nothing plugin-specific lives in this layer.
    """
    from .tools import error_envelope, ok_envelope

    operation = str(op or "").strip().lower()
    if operation not in ("read", "load"):
        raise InvalidValue(
            f"unknown sample op {op!r}",
            detail={"op": op, "accepted": ["read", "load"]},
        )
    enabled = V.strict_bool(dry_run, "dry_run")
    resolved_config = _config(config)
    provider, resolution, bridge, registry, source = _resolution_any(
        resolved_config,
        instance,
        require_writable=operation == "load",
        tool="plugin_sample",
    )
    _requires_provider(source, "plugin_sample", "the provider's sampler vocabulary")
    capability = _sampler_capability(provider)
    meta = _meta(resolution, source=source)
    ref = resolution.instance.ref
    profile = resolution.profile

    if operation == "read":
        facts = bridge.fx_facts(ref.location["track_index"], ref.location["fx_index"])
        chunk = bridge.read_state_chunk(ref)
        slice_ = bridge.locate_state_slice(ref, facts, chunk)
        document = provider.codec().decode(slice_)["document"]
        data = capability.read(document)
        controls = _sampler_control_state(
            resolved_config, instance, profile, _sampler_mapping(capability)
        )
        data["op"] = operation
        data["controls"] = controls
        return ok_envelope("plugin_sample", data, meta=meta)

    wanted, skipped = _sampler_controls(
        profile,
        {
            "on": on,
            "loop": loop,
            "destination": destination,
            "level": level,
            "transpose": transpose,
            "tune": tune,
            "pan": pan,
            "keytrack": keytrack,
        },
        _sampler_mapping(capability),
    )
    built = capability.load(file_path=file_path, samples=samples, sample_rate=sample_rate, name=name)
    path = _sampler_slot_path(capability)
    request: dict[str, Any] = {"state": {path: built["block"]}}
    if wanted:
        request["controls"] = wanted
    data = {
        "op": operation,
        "path": path,
        "described": built["described"],
        "dry_run": enabled,
        "controls": wanted,
    }
    if skipped:
        data["controls_skipped"] = skipped
    transaction = Transaction(
        provider=provider,
        codec=provider.codec(),
        bridge=bridge,
        profile=profile,
        ref=ref,
        request=request,
        token=resolution.instance.token,
        registry=registry,
        dry_run=enabled,
        label="plugin_sample:load",
    )
    result = transaction.run()
    data.update(result.data)
    _gaps_from_outcome(result.data, data)
    data["loaded"] = bool(result.ok) and not [
        gap for gap in data.get("gaps") or [] if str(gap.get("path")) == path
    ]
    if result.ok:
        return ok_envelope("plugin_sample", data, meta=meta, outcome=result.data.get("outcome"))
    error = result.error or PluginError("the sample load did not complete")
    return error_envelope("plugin_sample", error, meta=meta, outcome=result.data.get("outcome"))


def _sampler_mapping(capability: Any) -> dict[str, str]:
    """The capability's knob -> control id map (the core never knows a provider's ids)."""
    mapping = getattr(capability, "controls", None)
    if not isinstance(mapping, dict) or not mapping:
        raise UnsupportedProvider(
            "this provider's sampler capability does not name its controls, so a knob cannot be "
            "addressed on it",
            detail={"missing": "SampleCapability.controls"},
        )
    return {str(knob): str(control) for knob, control in mapping.items()}


def _sampler_controls(
    profile: dict[str, Any], knobs: dict[str, Any], mapping: dict[str, str]
) -> tuple[dict[str, Any], dict[str, str]]:
    """The caller's knobs as `{control_id: value}`, with what could not be addressed.

    A knob is named in the tool's own vocabulary (`on`, `loop`, `destination`, ...) and the
    control id is the PROVIDER's - `mapping` is the capability's own table, so this layer never
    names a provider's ids. A boolean for an enum control is translated to the control's own
    label ("On"/"Off", read from the profile), because every declared enum takes a member id
    and a `True` written as `1` would be a guess at band positions. A control the profile does
    not declare is dropped and NAMED (`controls_skipped`) rather than silently narrowing the
    batch.
    """
    wanted: dict[str, Any] = {}
    skipped: dict[str, str] = {}
    for knob, value in knobs.items():
        if value is None:
            continue
        control_id = mapping.get(knob)
        if control_id is None:
            skipped[str(knob)] = f"the capability declares no control for knob {knob!r}"
            continue
        control = _control_by_name(profile, control_id)
        if control is None:
            skipped[control_id] = f"the profile declares no control {control_id!r} for knob {knob!r}"
            continue
        if isinstance(value, bool):
            members = [str(member.get("id")) for member in control.get("enum_values") or []]
            label = next(
                (
                    member
                    for member in members
                    if member.strip().lower() == ("on" if value else "off")
                ),
                None,
            )
            if label is None:
                raise InvalidValue(
                    f"{control_id!r} declares no 'On'/'Off' member, so a boolean cannot address it",
                    detail={"control": control_id, "knob": knob, "legal": members},
                )
            wanted[control_id] = label
            continue
        wanted[control_id] = value
    return wanted, skipped


def _sampler_control_state(
    config: Any, instance: Any, profile: dict[str, Any], mapping: dict[str, str]
) -> dict[str, Any]:
    """The sampler's controls as they are NOW (one batched read; never a guess)."""
    ids = [control for control in mapping.values() if _control_by_name(profile, control)]
    if not ids:
        return {}
    envelope = plugin_read(config=config, instance=instance, controls=ids, include_display=True)
    return (envelope.get("data") or {}).get("values") or {}


def _sampler_capability(provider: Any) -> Any:
    """The provider's sampler vocabulary, or a typed refusal naming the provider."""
    hook = getattr(provider, "samples", None)
    capability = hook() if callable(hook) else None
    if capability is None:
        raise UnsupportedProvider(
            "this provider declares no sampler capability, so no sample can be loaded for it",
            detail={"provider": str(getattr(provider, "id", "?")), "tool": "plugin_sample"},
        )
    return capability


def _sampler_slot_path(capability: Any) -> str:
    """The state path of the sampler block, through the provider's own vocabulary."""
    hook = getattr(capability, "slot_path", None)
    if not callable(hook):
        raise UnsupportedProvider(
            "this provider's sampler capability does not name its slot path, so a sample cannot "
            "be addressed on it",
            detail={"missing": "SampleCapability.slot_path"},
        )
    return str(hook())


def _wavetable_slot_path(capability: Any, oscillator: Any) -> str:
    """The state path of an oscillator's table slot, through the provider's own vocabulary.

    The path is the codec's (`settings.wavetables.<n>`) and this layer must not hard-code it: a
    provider that names its table list differently would otherwise be written to the wrong place.
    """
    hook = getattr(capability, "slot_path", None)
    if not callable(hook):
        raise UnsupportedProvider(
            "this provider's wavetable capability does not name its slot path, so a table cannot "
            "be addressed on it",
            detail={"missing": "WavetableCapability.slot_path"},
        )
    return str(hook(oscillator))


#: tool name -> production body. Registration is unconditional (AUD-013): the
#: table is attached by `setup_plugin_tools`, and a call with no provider
#: configured answers a typed `UnsupportedProvider` instead of being absent.
BODIES: dict[str, Any] = {
    "plugin_instances": plugin_instances,
    "plugin_describe": plugin_describe,
    "plugin_find": plugin_find,
    "plugin_read": plugin_read,
    "plugin_write": plugin_write,
    "plugin_state": plugin_state,
    "plugin_modulation": plugin_modulation,
    "plugin_prepare": plugin_prepare,
    "plugin_presets": plugin_presets,
    "plugin_wavetable": plugin_wavetable,
    "plugin_sample": plugin_sample,
}


def register_production_bodies() -> None:
    """Attach every production body to the tool dispatch table (idempotent)."""
    from .tools import register_dispatch

    for name, body in BODIES.items():
        register_dispatch(name, body)
