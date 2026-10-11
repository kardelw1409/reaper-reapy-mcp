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
from typing import Any, Mapping

from . import modulation as MOD
from . import resolve as RS
from . import values as V
from .binding import declared_names, indexed_controls, normalise_name
from .cache import ProfileCache
from .errors import (
    IdentityContinuityUnprovable,
    InvalidValue,
    NotConnected,
    PluginError,
    SnapshotUnavailable,
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
    "register_production_bodies",
    "session_bridge",
    "session_registry",
]

#: Which providers are enabled (`REAPER_MCP_PLUGIN_PROVIDERS`, comma separated).
#: Unset or empty means **zero providers** (13 §4): every plugin tool then answers
#: a typed `UnsupportedProvider`, which is the documented inert-but-visible state.
PROVIDERS_ENV = "REAPER_MCP_PLUGIN_PROVIDERS"

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


def _meta(resolution: RS.ProfileResolution) -> dict[str, Any]:
    """The sanctioned live-response metadata (cache, provisional flag, token).

    ``identity_basis`` is part of the honesty contract: it states what the target
    token validated and what it explicitly does **not** claim. ``startup``
    carries the one-shot preload summary so a caller can see whether the process
    started with prepared profiles loaded - and that a warm call reused them
    rather than researching the plugin again.
    """
    from . import preload as _preload  # noqa: PLC0415 - lazy: keeps registration import-light

    meta = resolution.meta()
    meta["instance"] = _ref_dict(resolution.instance.ref)
    meta["target_token"] = resolution.instance.token.id
    meta["identity_basis"] = RS.identity_basis()
    meta["profile_key"] = resolution.build.key_for(
        str(resolution.instance.ref.provider), resolution.identity
    )
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
    **_: Any,
) -> dict[str, Any]:
    from .tools import ok_envelope

    resolved_config = _config(config)
    providers = _providers(resolved_config)
    if provider:
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
    meta: dict[str, Any] = {
        "configured_providers": list(resolved_config.provider_ids),
        "selector": dict(selector or {}),
        "checked_utc": utc_now_iso(),
        "identity_basis": RS.identity_basis(),
    }
    if len(tokens) == 1:
        # the one target's session-scoped token, so a caller can address it
        meta["target_token"] = tokens[0]
    return ok_envelope("plugin_instances", data, meta=meta)


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
        "writable": sum(1 for row in controls if (row.get("authority") or {}).get("write") == "parameter"),
        "enums": sum(1 for row in controls if row.get("type") == "enum"),
        "state_only": sum(1 for row in controls if (row.get("authority") or {}).get("write") == "state"),
        "no_law": sum(
            1 for row in controls if ((row.get("law") or {}).get("kind") in ("none", "unmeasured"))
        ),
        "indexed": len(indexed_controls(profile)),
    }


def plugin_describe(
    *, config: Any = None, instance: Any = None, detail: str = "summary", **_: Any
) -> dict[str, Any]:
    from .tools import ok_envelope

    resolved_config = _config(config)
    provider, resolution, _bridge, _registry = _resolution(resolved_config, instance)
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
        },
        "state": {
            "codec": (profile.get("state") or {}).get("codec"),
            "derived_leaves": list((profile.get("state") or {}).get("derived_leaves") or []),
            "structural_keys": list((profile.get("state") or {}).get("structural_keys") or []),
        },
        "limits": list(profile.get("limits") or []),
        "warnings": [],
    }
    return ok_envelope("plugin_describe", data, meta=_meta(resolution))


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
    _provider, resolution, _bridge, _registry = _resolution(resolved_config, instance)
    profile = resolution.profile
    controls = list(profile.get("controls") or [])
    modules = list(profile.get("modules") or [])
    kind = str(kind or "control")
    limit = max(1, int(limit))

    if group:
        module = next((row for row in modules if str(row.get("id")) == str(group)), None)
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
        modulation = profile.get("modulation") or {}
        matches = [
            {"kind": "source", "id": str(key)} for key in (modulation.get("sources") or [])[:limit]
        ] + [
            {"kind": "destination", "id": str(key)}
            for key in (modulation.get("destinations") or [])[:limit]
        ]
        groups = []
    else:
        wanted = normalise_name(query)
        if wanted:
            matches = [
                row
                for row in controls
                if any(wanted in name for name in declared_names(row))
                or wanted in normalise_name(row.get("label"))
            ]
        else:
            matches = list(controls)
        groups = [
            {"id": row.get("id"), "kind": row.get("kind"), "n_controls": len(row.get("controls") or [])}
            for row in modules
        ]

    rows: list[dict[str, Any]] = []
    for row in matches[:limit]:
        entry = {
            "kind": str(row.get("type") or row.get("kind") or "control"),
            "id": row.get("id"),
            "label": row.get("label"),
            "module": row.get("module"),
            "type": row.get("type"),
            "unit": row.get("unit"),
            "range": row.get("range"),
            "binding": {"index": (row.get("binding") or {}).get("index")},
        }
        if row.get("enum_values"):
            entry["enum_values"] = [item.get("id") for item in row["enum_values"]]
        if include_capability:
            entry["aliases"] = list(row.get("aliases") or [])
            entry["authority"] = dict(row.get("authority") or {})
            entry["law"] = dict(row.get("law") or {})
            entry["evidence"] = dict(row.get("evidence") or {})
        rows.append(entry)
    data = {
        "matches": rows,
        "n_matches": len(matches),
        "suggestions": _suggestions(profile, query) if query and not matches else [],
        "groups": groups,
    }
    return ok_envelope("plugin_find", data, meta=_meta(resolution))


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
    provider, resolution, bridge, _registry = _resolution(resolved_config, instance)
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
    if wanted:
        indices = [index for _cid, _control, index in wanted]
        names = bridge.param_names(ref, indices)
        raw = bridge.read_params(ref, indices)
        if len(names) != len(indices) or len(raw) != len(indices):
            raise SnapshotUnavailable(
                "the host returned a short read; a short read is not evidence",
                detail={"asked": len(indices), "names": len(names), "values": len(raw)},
            )
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
            kind = str((control.get("law") or {}).get("kind") or "identity")
            if include_display and kind not in ("none", "unmeasured"):
                value["display"] = V.from_native(control, value["native"])
            elif include_display:
                value["display"] = None
                value["display_note"] = f"the control's law is {kind!r}: no display conversion exists"
            values[control_id] = value

    data = {"values": values, "problems": problems}
    return ok_envelope("plugin_read", data, meta=_meta(resolution))


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
    provider, resolution, bridge, registry = _resolution(
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
    meta = _meta(resolution)
    data = {
        "instance": _ref_dict(resolution.instance.ref),
        "dry_run": enabled,
        **result.data,
    }
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
    provider, resolution, bridge, _registry = _resolution(resolved_config, instance)
    ref = resolution.instance.ref
    track_index, fx_index = ref.location["track_index"], ref.location["fx_index"]
    facts = bridge.fx_facts(track_index, fx_index)
    chunk = bridge.read_state_chunk(ref)
    slice_ = bridge.locate_state_slice(ref, facts, chunk)
    document = provider.codec().decode(slice_)
    routes = list(document.get("routes") or [])
    settings = document.get("settings") or {}
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
    return ok_envelope("plugin_state", data, meta=_meta(resolution))


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
    provider, resolution, bridge, registry = _resolution(
        resolved_config, instance, require_writable=mutation, tool=f"plugin_modulation:{op}"
    )
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
    envelope["meta"].update(_meta(resolution))
    envelope["meta"]["dry_run"] = enabled
    return envelope


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
}


def register_production_bodies() -> None:
    """Attach every production body to the tool dispatch table (idempotent)."""
    from .tools import register_dispatch

    for name, body in BODIES.items():
        register_dispatch(name, body)
