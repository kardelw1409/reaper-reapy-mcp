"""The Vital `PluginProvider` (08 §2, §7).

Vital is the first provider, not a template: everything specific to it - its
identity strings, its registry, its document format, its depth fits and its rule
table - lives in this package, and Core sees only the contracts.

Two honest boundaries are stated here rather than discovered later:

1. **The measured version.** The host reports no plugin version; Vital's version
   lives in its state document (`synth_version`). `identify` therefore carries
   whatever the host gives it, the profile declares the measured set, and
   `version_gate()` is the pure check. Wiring the *document* version into a
   write's preflight needs either a provider hook Core calls with the decoded
   document or a Core change; until then a write cannot be gated on a live
   document version, and this is recorded as an open item (not claimed).
2. **The parameter ident.** Core's parameter channel is ident-addressed, and the
   ident is host-provided (there is no offline source for it). A control write
   whose binding has no ident is therefore **refused with a named reason** rather
   than silently writing nothing - a silent no-op would be reported as a
   successful write.
"""

from __future__ import annotations

from typing import Any, Mapping

from ...binding import declared_names as _declared_names
from ...binding import normalise_name as _normalise
from ...errors import InvalidRoute, InvalidValue, NoConversion, UnsupportedControl, VersionMismatch
from ...model import (
    CaptureScope,
    Finding,
    FxHostFacts,
    ResolvedRequest,
    RouteSpec,
    TimingProfile,
    VerificationPolicy,
)
from ...provider import DependencyRuleSet
from . import codec as codec_module
from . import dependencies_rules as rules_module
from . import depth as depth_module
from .codec import VitalCodec
from .profile_build import (
    DEFAULT_REGISTRY_DIR,
    REGISTRY_DIR_ENV,
    SUPPORTED_VERSIONS,
    VITAL_CLASS_ID,
    VITAL_FX_NAME,
    VITAL_MODULE,
    VitalRegistry,
    build_profile,
    load_registry,
)

__all__ = ["IMPLEMENTATION_REVISION", "VitalProvider"]

#: Bumped on ANY semantic change to a declared artifact (07 §3.4): the cache key
#: covers provider logic, so a behaviour change without a bump is a release bug.
#: r2 (P8): live ident binding verification + the `document_version` hook.
#: r3 (P8): the codec's document locator (first brace) and its pad insertion
#: point (before the closing brace) - a live run found both.
#: r4 (P8): plugin-derived leaves are declared, refused as write targets and
#: excluded from a restore with a named gap.
#: r5 (2026-10-11): route creation is refused before the first write because
#: `route_into_off_filter` cannot be evaluated (the guard is a behaviour change:
#: a route create that used to be applied is now a typed refusal).
IMPLEMENTATION_REVISION = "vital-provider-2026.10-r5"

_PROVIDER_FILES = (
    "provider.py",
    "codec.py",
    "profile_build.py",
    "dependencies_rules.py",
    "depth.py",
    "__init__.py",
)

_STATE_ROOT = codec_module.MUTABLE_ROOT


class VitalProvider:
    """Vital Audio's Vital, VST3 build, version set measured in the registry."""

    id = "vital"
    implementation_revision = IMPLEMENTATION_REVISION

    def __init__(
        self,
        *,
        registry: VitalRegistry | None = None,
        registry_dir: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._registry = registry
        self._registry_dir = registry_dir
        self._env = env
        self._last_profile: dict[str, Any] | None = None
        self._timing = TimingProfile(settle_s=0.4, retry_after_s=2.0, retry_count=1, timeout_s=20.0)

    # -- configuration (read on first use, never at import) -----------------

    def registry(self) -> VitalRegistry:
        """The loaded registry; loads and validates on first use."""
        if self._registry is None:
            self._registry = load_registry(directory=self._registry_dir, env=self._env)
        return self._registry

    def artifact_paths(self) -> list[str]:
        """Behaviour-affecting files of this provider (hashed into the cache key)."""
        from pathlib import Path

        here = Path(__file__).parent
        return [str(here / name) for name in _PROVIDER_FILES]

    # -- identity -----------------------------------------------------------

    def matches(self, facts: FxHostFacts) -> bool:
        """Vital's VST3 build only; a VST2 build is not this provider's target.

        The measured discriminators are the class id (32 hex) and the module
        string; the name alone is not evidence, and a build whose module is
        `vital.vst2` or whose class differs is refused by **not matching**
        (fail closed: no profile, no describe, no write).
        """
        if "vital" not in (facts.fx_name or "").lower():
            return False
        if (facts.class_id or "").upper() != VITAL_CLASS_ID.upper():
            return False
        return (facts.module or "").lower() == VITAL_MODULE.lower()

    def identify(self, facts: FxHostFacts) -> dict[str, Any]:
        """The profile fingerprint as the host reports it right now (pure)."""
        identity: dict[str, Any] = {
            "fx_name": facts.fx_name,
            "class_id": facts.class_id,
            "module": facts.module,
            "param_count": facts.param_count,
        }
        if facts.version is not None:
            identity["version"] = facts.version
        return identity

    def version_gate(self, profile: Mapping[str, Any], live_version: str | None) -> None:
        """07 §3.2: a live version outside the measured set refuses writes."""
        if live_version is None:
            return
        compatible = [str(item) for item in (profile.get("identity") or {}).get("compatible_versions") or []]
        if str(live_version) not in compatible:
            raise VersionMismatch(
                f"the live Vital version {live_version!r} is not in the measured set {sorted(compatible)}; "
                "writes are refused and no override exists in V1",
                detail={"live_version": live_version, "compatible_versions": sorted(compatible), "override": None},
            )

    def document_version(self, document: Any) -> str | None:
        """The **live** version carried by a decoded document (07 §3.2).

        Measured live (P8, 2026-10-10): Vital's document carries
        ``synth_version`` (``"1.5.5"`` on the loaded instance), and the host
        reports no plugin version of its own - so this is the only live version
        a write can be gated on. Core calls this hook if it exists; a provider
        without a document-side version simply does not implement it.
        """
        if not isinstance(document, Mapping):
            return None
        value = document.get("synth_version")
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    # -- profile ------------------------------------------------------------

    def build_profile(self, identity: dict[str, Any]) -> dict[str, Any]:
        profile = build_profile(self.registry(), identity)
        self._last_profile = profile
        return profile

    # -- request normalization ---------------------------------------------

    def normalize_request(self, profile: dict[str, Any], request: dict[str, Any]) -> ResolvedRequest:
        """Resolve names/values against the profile; no host access.

        Refusals are typed problem rows, never silent skips:
        unknown control; a law of `none`/`unmeasured` (no conversion is possible
        and none is invented); a value outside the declared range or hitting the
        ``%`` trap; an unbound parameter ident; a route outside the measured
        vocabulary (the POC's reason: an unknown destination fails *silently* in
        the plugin).
        """
        resolved = ResolvedRequest()
        self._last_profile = profile or self._last_profile
        controls = list(profile.get("controls") or [])

        for name, value in dict(request.get("controls") or {}).items():
            control = self._control_by_name(controls, name)
            if control is None:
                resolved.problems.append(
                    {
                        "control": name,
                        "code": "UnsupportedControl",
                        "message": f"unknown control {name!r}",
                        "detail": {"suggestions": self._suggest(name, controls)},
                    }
                )
                continue
            try:
                import sys

                values = sys.modules.get("reaper_reapy_mcp.plugins.values")
                if values is None:  # pragma: no cover - imported lazily on first use
                    from ... import values as values_module

                    values = values_module
                native = values.to_native(control, value)
                normalized = values.to_normalized(control, native)
            except (InvalidValue, NoConversion) as exc:
                resolved.problems.append(
                    {"control": name, "code": exc.code, "message": exc.message, "detail": exc.detail}
                )
                continue
            binding = dict(control.get("binding") or {})
            if not binding.get("ident"):
                unverified = binding.get("unverified") or {}
                reason = str(unverified.get("reason") or "ident_not_bound")
                resolved.problems.append(
                    {
                        "control": name,
                        "code": "UnsupportedControl",
                        "message": (
                            "the parameter ident of this control is not bound to a verified live "
                            "address; an unbound write is refused instead of silently writing "
                            "nothing (the binding is measured against the host on every write)"
                        ),
                        "detail": {
                            "control": control.get("id"),
                            "index": binding.get("index"),
                            "reason": reason,
                            "observed_name": unverified.get("observed_name"),
                            "declared_names": unverified.get("declared_names"),
                        },
                    }
                )
                continue
            resolved.controls[control["id"]] = {
                "control": control,
                "display": value,
                "native": native,
                "normalized": normalized,
                "channel": str((control.get("authority") or {}).get("write") or "parameter"),
                "binding": binding,
            }

        vocabulary_sources = set(str(key) for key in (profile.get("modulation") or {}).get("sources") or [])
        vocabulary_destinations = set(
            str(key) for key in (profile.get("modulation") or {}).get("destinations") or []
        )
        for spec in request.get("routes_create") or []:
            source = spec.get("source")
            destination = spec.get("destination")
            if source not in vocabulary_sources or destination not in vocabulary_destinations:
                resolved.problems.append(
                    {
                        "route": dict(spec),
                        "code": "InvalidRoute",
                        "message": (
                            f"route vocabulary violation: {source!r} -> {destination!r}; an unknown "
                            "destination fails silently in the plugin, so it is refused here"
                        ),
                        "detail": {
                            "reason": "vocabulary",
                            "sources": sorted(vocabulary_sources)[:40],
                            "destinations_count": len(vocabulary_destinations),
                        },
                    }
                )
                continue
            amount = spec.get("amount")
            if amount is None and spec.get("depth") is not None:
                try:
                    amount = self.convert_depth(profile, str(destination), spec["depth"])
                except (NoConversion, InvalidValue) as exc:
                    resolved.problems.append(
                        {"route": dict(spec), "code": exc.code, "message": exc.message, "detail": exc.detail}
                    )
                    continue
            if amount is None:
                resolved.problems.append(
                    {
                        "route": dict(spec),
                        "code": "InvalidValue",
                        "message": "a route needs an amount or a depth to convert",
                        "detail": {"reason": "missing_amount"},
                    }
                )
                continue
            resolved.routes_create.append(
                {
                    "source": str(source),
                    "destination": str(destination),
                    "amount": float(amount),
                    "bipolar": float(spec.get("bipolar") or 0.0),
                }
            )

        for key, target in (("routes_update", resolved.routes_update), ("routes_remove", resolved.routes_remove)):
            for spec in request.get(key) or []:
                if spec.get("slot") is None:
                    resolved.problems.append(
                        {
                            "route": dict(spec),
                            "code": "InvalidRoute",
                            "message": f"a {key.split('_')[1]} must name the route's slot identity",
                            "detail": {"reason": "missing_slot"},
                        }
                    )
                    continue
                target.append({k: v for k, v in spec.items() if v is not None})

        for path, value in dict(request.get("state") or {}).items():
            if not str(path).startswith(f"{_STATE_ROOT}."):
                resolved.problems.append(
                    {
                        "path": path,
                        "code": "InvalidValue",
                        "message": f"state path {path!r} is not rooted at {_STATE_ROOT!r}",
                        "detail": {"roots": [_STATE_ROOT]},
                    }
                )
                continue
            resolved.state[str(path)] = value

        resolved.structural = bool(request.get("structural"))
        if resolved.structural:
            # The DISABLE set is a provider-side write on the caller's behalf, so
            # every target must be a path the profile **measured** and a value in
            # the document's own space. Spelling the candidate name as a path
            # created phantom keys instead (measured 2026-10-10: 13 keys created,
            # no real setting changed, and the plan verified itself against the
            # keys it had just invented); an unmappable or unjustifiable target is
            # now a typed refusal (Decision 1).
            planned: list[str] = []
            for row in resolved.controls.values():
                control = row.get("control")
                if isinstance(control, dict):
                    planned.extend(_declared_names(control))
            to_disable = {_normalise(name) for name in rules_module.disable_set(planned)}
            for target in rules_module.disable_targets(profile):
                if _normalise(target["candidate"]) not in to_disable:
                    continue  # the plan keeps this one on
                reason = target.get("reason")
                if reason:
                    resolved.problems.append(
                        {
                            "state_path": target.get("path") or target["candidate"],
                            "code": "PlanError",
                            "message": (
                                f"the structural DISABLE target {target['candidate']!r} cannot be "
                                "addressed in the live document"
                            ),
                            "detail": {
                                "reason": reason,
                                "candidate": target["candidate"],
                                "off_value": target.get("off"),
                                "explanation": target.get("detail"),
                                "why_disable": target.get("why"),
                            },
                        }
                    )
                    continue
                resolved.state.setdefault(str(target["path"]), target["value"])
        return resolved

    def _control_by_name(self, controls: list[dict[str, Any]], name: Any) -> dict[str, Any] | None:
        wanted = str(name).strip().lower()
        for control in controls:
            candidates = {str(control.get("id", "")).lower(), str(control.get("label", "")).lower()}
            candidates.update(str(alias).lower() for alias in control.get("aliases") or [])
            if wanted in candidates:
                return control
        return None

    def _suggest(self, name: Any, controls: list[dict[str, Any]], limit: int = 3) -> list[str]:
        wanted = str(name).lower()
        scored: list[tuple[int, str]] = []
        for control in controls:
            control_id = str(control.get("id", ""))
            score = sum(1 for token in wanted.replace(" ", "_").split("_") if token and token in control_id.lower())
            if score:
                scored.append((score, control_id))
        scored.sort(key=lambda row: (-row[0], row[1]))
        return [control_id for _score, control_id in scored[:limit]]

    # -- depth --------------------------------------------------------------

    def convert_depth(self, profile: dict[str, Any], destination: str, depth: Any) -> float:
        """Semantic depth -> native amount, or `NoConversion` with the reason."""
        registry = self.registry()
        result = depth_module.modulation_depth(registry.rows(), registry.scales(), str(destination), float(depth))
        return float(result["amount"])

    def depth_table(self, *, destinations: list[str] | None = None) -> dict[str, Any]:
        registry = self.registry()
        return depth_module.depth_table(registry.rows(), registry.scales(), destinations)

    # -- rules / policy -----------------------------------------------------

    def dependencies(self) -> DependencyRuleSet:
        """The measured rule table plus the projected-world evaluator.

        The evaluator is built against the last profile this provider produced,
        because a rule needs the profile's display mapping; Core builds the
        profile before it runs a transaction (P4 wiring).
        """
        return rules_module.dependencies(self._last_profile or {})

    def unevaluated_rules(self) -> list[dict[str, Any]]:
        """Honest status of every declared rule this provider does not decide.

        Core attaches this list to a mutation's response (dry-run included), so a
        caller is told what was **not** checked instead of reading an empty
        findings list as "everything was checked" (Decision 1).
        """
        return rules_module.rules_not_evaluated()

    def disable_targets(self) -> list[dict[str, Any]]:
        """The structural DISABLE set as addressable targets, or named refusals."""
        return rules_module.disable_targets(self._last_profile or {})

    def codec(self) -> VitalCodec:
        return VitalCodec()

    def write_order(self) -> tuple[str, ...]:
        """Fixed document -> parameters (the measured ordering rule)."""
        return ("state", "parameters")

    def verification_policy(self) -> VerificationPolicy:
        return VerificationPolicy(
            tolerances={"state.float": 1e-5, "parameter.normalized": 1e-3},
            comparators={"enum": "exact", "number": "abs_le"},
            timing=self._timing,
        )

    def capture_scope(self) -> CaptureScope:
        return CaptureScope(
            channels=["document", "routes", "parameters"],
            parameters={"scope": "full_vector", "ident_required": True},
            restore_order=["document", "parameters"],
            verify_order=["document", "parameters"],
        )

    def limits(self) -> list[str]:
        profile = self._last_profile
        if profile and profile.get("limits"):
            return [str(item) for item in profile["limits"]]
        return [
            "VST2 build not supported",
            f"only the measured version(s) {sorted(SUPPORTED_VERSIONS)} are accepted",
            "one route per state write",
            "no line_mapping writes",
            "a control write is issued only when the host's parameter name at the declared "
            "index matches the control (verified live on every write; a mismatch refuses)",
        ]

    # -- diagnostics --------------------------------------------------------

    def settings_report(self) -> dict[str, Any]:
        """Where the registry was resolved from and what it declares."""
        registry = self.registry()
        return {
            "registry_dir": registry.directory,
            "variable": REGISTRY_DIR_ENV,
            "default_dir": DEFAULT_REGISTRY_DIR,
            "files": dict(registry.files),
            "measured_version": registry.version,
            "compatible_versions": sorted({registry.version or SUPPORTED_VERSIONS[0], *SUPPORTED_VERSIONS}),
            "fake_name": VITAL_FX_NAME,
        }
