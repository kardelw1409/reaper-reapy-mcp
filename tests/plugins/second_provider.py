"""A **test-only** second provider: the cross-provider test of P24, workstream B.

Why it exists: a successful Vital run proves the cache/preload path works for *one*
provider. What it cannot prove is that the path has no Vital-shaped assumption in it
- a control name, a calibration artifact path, an enum convention, a provenance
field. This provider is deliberately *unlike* Vital in every way that matters:

* **parameter-only**: it has no document/state channel at all, so a state codec,
  routes, capture scope and write ordering don't apply to it;
* **its own provenance vocabulary**: two sources, its own revision, and an unlabelled
  enum state recorded in the *law params* rather than in the first provider's
  artifact-block format;
* **its own identity**: a different fingerprint, class id, module and version.

It is never registered in the production provider map
(``plugins/providers/registry.py``): the tests inject a factory into the registry's
private map at runtime, so a fresh process does not know this provider exists. Nothing
here is a real plugin's data - the numbers are synthetic and the fixtures say so.

The class implements the same surface the first provider does where the shared layer
uses it (id, revision, artifact paths, source rows, identity, profile build,
adoption); everything it deliberately does *not* implement (state codec, request
normalisation) raises a typed refusal, which is itself a case the shared layer must
handle for a provider that only owns parameters.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from reaper_reapy_mcp.plugins.errors import UnsupportedProvider

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "second_provider"
SOURCES = ("second_registry.json", "second_notes.json")

#: The plugin id this provider answers to, and the id it is configured under.
PROVIDER_ID = "second"
#: Its own revision string - a key determinant a stale entry must be caught by.
IMPLEMENTATION_REVISION = "second-provider-r1"


def source_rows() -> list[dict[str, str]]:
    """The provider's declared sources, with digests read from disk."""
    rows: list[dict[str, str]] = []
    for name in SOURCES:
        data = (FIXTURES / name).read_bytes()
        rows.append({"file": name, "sha256": hashlib.sha256(data).hexdigest()})
    return rows


def artifact_paths() -> list[str]:
    """Behaviour-affecting files, digested into the cache key (AUD-008)."""
    return [str(FIXTURES / name) for name in SOURCES]


def registry() -> dict[str, Any]:
    return json.loads((FIXTURES / "second_registry.json").read_text(encoding="utf-8"))


class SecondProvider:
    """A parameter-only provider over a synthetic two-file registry."""

    def __init__(self, *, revision: str | None = None) -> None:
        self.id = PROVIDER_ID
        self.implementation_revision = revision or IMPLEMENTATION_REVISION
        self._profile: dict[str, Any] | None = None

    # -- identity -----------------------------------------------------------

    def matches(self, facts: Any) -> bool:
        identity = getattr(facts, "class_id", None)
        module = getattr(facts, "module", None)
        return identity == registry()["plugin"]["class_id"] or module == registry()["plugin"]["module"]

    def identify(self, facts: Any) -> dict[str, Any]:
        """The identity the shared layer requires: **flat** fields, host-reported.

        The contract (match the first provider's): ``fx_name``, ``class_id``,
        ``module`` and ``param_count``, plus ``version`` when the host reports one.
        A nested ``fingerprint`` dict here is not the contract, and the shared
        layer's cache gate refuses to save a profile whose identity cannot be
        re-derived from the live facts - measured 2026-10-12 while writing this
        provider: the save was refused with
        ``identity: a required identity field is missing: ['param_count']``.
        """
        plugin = registry()["plugin"]
        identity: dict[str, Any] = {
            "fx_name": getattr(facts, "fx_name", plugin["fx_name"]),
            "class_id": plugin["class_id"],
            "module": plugin["module"],
            "param_count": getattr(facts, "param_count", plugin["param_count"]),
        }
        version = getattr(facts, "version", None)
        if version:
            identity["version"] = str(version)
        return identity

    def version_gate(self, profile: Mapping[str, Any], live_version: str | None) -> None:
        """A live version outside the measured set refuses writes (shared contract)."""
        from reaper_reapy_mcp.plugins.errors import VersionMismatch

        if live_version is None:
            return
        compatible = [
            str(item) for item in (profile.get("identity") or {}).get("compatible_versions") or []
        ]
        if str(live_version) not in compatible:
            raise VersionMismatch(
                f"the live version {live_version!r} is not in the measured set {sorted(compatible)}",
                detail={"live_version": live_version, "compatible_versions": sorted(compatible)},
            )

    # -- key material (the optional hook the shared cache uses) --------------

    def artifact_paths(self) -> list[str]:
        return artifact_paths()

    def profile_source_rows(self, identity: Mapping[str, Any]) -> list[dict[str, str]]:
        return source_rows()

    # -- the profile --------------------------------------------------------

    def build_profile(self, identity: Mapping[str, Any]) -> dict[str, Any]:
        data = registry()
        plugin = data["plugin"]
        evidence = identity if isinstance(identity, Mapping) else {}
        profile_key = str(evidence.get("state_key") or plugin["plugin_id"])
        controls: list[dict[str, Any]] = []
        for row in data["controls"]:
            binding: dict[str, Any] = {"index": row.get("index"), "ident": None, "state_key": row["id"]}
            law = json.loads(json.dumps(row["law"]))
            boundary = law["params"].get("boundary_evidence")
            if boundary is not None:
                rows = source_rows()
                boundary["artifact"] = {"file": "second_notes.json", "sha256": rows[1]["sha256"]}
            controls.append(
                {
                    "id": row["id"],
                    "label": row["label"],
                    "module": row["module"],
                    "type": row["type"],
                    "unit": row["unit"],
                    "range": row["range"],
                    "discrete": row["discrete"],
                    "enum_values": row.get("enum_values"),
                    "aliases": [],
                    "authority": {"read": "parameter", "write": "parameter"},
                    "binding": binding,
                    "law": law,
                    "evidence": dict(row["evidence"]),
                }
            )
        module_by_id = {row["id"]: row for row in data["modules"]}
        modules = [
            {
                "id": row["id"],
                "kind": row["kind"],
                "label": row["label"],
                "parent": row["parent"],
                "controls": list(row["controls"]),
                "state_refs": list(row.get("state_refs") or []),
            }
            for row in module_by_id.values()
        ]
        profile = {
            "profile_schema_version": 2,
            "provider": self.id,
            "identity": {
                "plugin_id": plugin["plugin_id"],
                "vendor": plugin["vendor"],
                "format": plugin["format"],
                "fx_name": str(evidence.get("fx_name") or plugin["fx_name"]),
                "class_id": str(evidence.get("class_id") or plugin["class_id"]),
                "module": str(evidence.get("module") or plugin["module"]),
                "compatible_versions": [plugin["version"]],
                "fingerprint": {
                    "param_count": evidence.get("param_count", plugin["param_count"])
                },
            },
            "provenance": {
                "generated_utc": None,
                "generator": "second_provider/synthetic_registry",
                "sources": source_rows(),
                "measured_on": {
                    "host": "reaper",
                    "version": plugin["version"],
                    "evidence": list(SOURCES),
                    "registry_dir": str(FIXTURES),
                },
                "provider_revision": self.implementation_revision,
            },
            "modules": modules,
            "controls": controls,
            "values": {
                "kinds": ["affine", "enum_index"],
                "unit_traps": [],
            },
            "modulation": {
                # a parameter-only plugin: the validator forbids declaring slots or
                # fields for one that reports no modulation support
                "supported": False,
            },
            "state": {
                "supported": False,
                "codec": None,
                "capture_scope": {
                    "channels": ["parameters"],
                    "parameters": {"scope": "full_vector", "ident_required": True},
                    "restore_order": ["parameters"],
                    "verify_order": ["parameters"],
                },
                "restore_points": "unsupported",
            },
            "dependencies": [],
            "write_order": ["parameters", "verify"],
            "verification": {
                "tolerances": {"parameter.normalized": 0.001},
                "comparators": {"enum": "exact", "number": "abs_le"},
            },
            "limits": [
                "test-only provider: no state channel, no modulation, no real plugin behind it",
            ],
            "x_provider": {"profile_key": profile_key, "synthetic": True},
        }
        self._profile = profile
        return profile

    def adopt_profile(self, profile: Mapping[str, Any]) -> bool:
        """A cache hit skips `build_profile`, so the provider must adopt what is served."""
        self._profile = dict(profile)
        return True

    # -- deliberately unimplemented channels --------------------------------

    def codec(self) -> Any:
        raise UnsupportedProvider(
            "the second test provider owns parameters only: it has no state codec",
            detail={"provider": self.id, "channel": "state"},
        )

    def normalize_request(self, profile: Mapping[str, Any], request: Mapping[str, Any]) -> Any:
        raise UnsupportedProvider(
            "the second test provider cannot resolve requests: it is parameter-only",
            detail={"provider": self.id},
        )

    def dependencies(self) -> Any:
        from reaper_reapy_mcp.plugins.provider import DependencyRuleSet

        return DependencyRuleSet(rules=())

    def write_order(self) -> tuple[str, ...]:
        return ("parameters", "verify")

    def capture_scope(self) -> Any:
        return None

    def limits(self) -> list[str]:
        return ["test-only provider: no state channel, no modulation"]
