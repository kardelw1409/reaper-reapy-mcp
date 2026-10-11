"""Vital registry loading and profile building (08 §6, 07 §1).

Two jobs, one file, because the profile *is* a projection of the registry:

1. **Load** the registry directory exactly as 08 §6 specifies: resolution order
   (`REAPER_MCP_VITAL_REGISTRY_DIR` when set - even empty, which is a
   configuration error - else the documented default), normalisation to an
   absolute literal path (no ``~``/env expansion), validation of every expected
   file (exists, readable, parses, expected top-level shape), and **no fallback
   anywhere**: an invalid configured directory is a typed `IncompatibleSettings`
   naming the variable and the resolved path.
2. **Build** a [07 §1] capability profile from that data: identity, modules,
   controls (with laws derived from the measured correspondence), the modulation
   model (vocabulary + depth-fit table), the dependency rule table, the declared
   limits and the provenance rows (real hashes over the files actually read).

Nothing here runs at import time, and no host module is imported.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from ...errors import IncompatibleSettings
from . import codec as codec_module
from . import depth as depth_module
from . import dependencies_rules as rules_module

__all__ = [
    "REGISTRY_DIR_ENV",
    "DEFAULT_REGISTRY_DIR",
    "REGISTRY_FILES",
    "SUPPORTED_VERSIONS",
    "VITAL_FX_NAME",
    "VITAL_CLASS_ID",
    "VITAL_MODULE",
    "VitalRegistry",
    "resolve_registry_dir",
    "load_registry",
    "build_profile",
]

REGISTRY_DIR_ENV = "REAPER_MCP_VITAL_REGISTRY_DIR"

#: The documented default (08 §6.1). It is NOT expanded and NOT a fallback for a
#: configured-but-broken value: only an unset variable reaches it.
DEFAULT_REGISTRY_DIR = r"C:\REAPER\prompts\vital_sound_design\registry"

#: Logical name -> file name. Fixed here (08 §6.2) against the POC's registry.
REGISTRY_FILES: dict[str, str] = {
    "controls": "vital_controls.json",
    "modules": "vital_modules.json",
    "correspondence": "vital_correspondence.json",
    "laws": "vital_scales_full.json",
    "capabilities": "modulation_vocabulary.json",
}

#: The only plugin version this provider has measured evidence for (07 §3.2).
SUPPORTED_VERSIONS: tuple[str, ...] = ("1.5.5",)

#: Measured identity of the VST3 build (02 §5, recorded live). The module string is
#: taken from the recorded chunk header (`... Vital.vst3 0 "" 56535456...`) -
#: the slice correlation compares it against the host's block header, so the
#: *exact* spelling matters; `matches` compares it case-insensitively.
VITAL_FX_NAME = "VST3i: Vital (Vital Audio)"
VITAL_CLASS_ID = "56535456697461766974616C00000000"
VITAL_MODULE = "Vital.vst3"

_VST3_FORMAT = "vst3"


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass
class VitalRegistry:
    """A loaded, validated registry: the data plus where it came from."""

    directory: str
    files: dict[str, str] = field(default_factory=dict)
    data: dict[str, Any] = field(default_factory=dict)
    sources: list[dict[str, Any]] = field(default_factory=list)
    version: str | None = None

    def rows(self) -> list[dict[str, Any]]:
        return list((self.data.get("correspondence") or {}).get("rows") or [])

    def scales(self) -> dict[str, Any]:
        return dict((self.data.get("laws") or {}).get("scales") or {})

    def vocabulary(self) -> dict[str, Any]:
        return dict(self.data.get("capabilities") or {})

    def modules(self) -> dict[str, Any]:
        return dict((self.data.get("modules") or {}).get("modules") or {})

    def row_for_state_key(self, state_key: str) -> dict[str, Any] | None:
        for row in self.rows():
            if row.get("state_key") == state_key:
                return row
        return None


def _fail(reason: str, *, variable: str | None, path: str, extra: Mapping[str, Any] | None = None) -> IncompatibleSettings:
    detail: dict[str, Any] = {"reason": reason, "resolved_path": path, "variable": variable}
    if extra:
        detail.update(extra)
    target = f"{variable} ({path})" if variable else path
    return IncompatibleSettings(f"the Vital registry directory is unusable: {target} - {reason}", detail)


def resolve_registry_dir(env: Mapping[str, str] | None = None) -> str:
    """Resolution order of 08 §6.1, with no silent fallback anywhere."""
    environ = os.environ if env is None else env
    override = environ.get(REGISTRY_DIR_ENV)
    if override is not None:
        if not str(override).strip():
            raise _fail(
                f"the variable is set to an empty value; a literal path is required",
                variable=REGISTRY_DIR_ENV,
                path="",
                extra={"value": str(override)},
            )
        if "~" in str(override) or "$" in str(override) or "%" in str(override):
            raise _fail(
                "the value contains an expansion character (~, $ or %); a literal path is required "
                "so a typo cannot silently resolve somewhere else",
                variable=REGISTRY_DIR_ENV,
                path=str(override),
            )
        return os.path.normpath(os.path.abspath(str(override)))
    return os.path.normpath(os.path.abspath(DEFAULT_REGISTRY_DIR))


def _read_json(path: Path, *, variable: str | None, directory: str, logical: str) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise _fail(
            f"{REGISTRY_FILES.get(logical, logical)} cannot be read: {exc}",
            variable=variable,
            path=directory,
            extra={"file": str(path)},
        ) from exc
    try:
        return json.loads(text)
    except ValueError as exc:
        raise _fail(
            f"{REGISTRY_FILES.get(logical, logical)} does not parse as JSON: {exc}",
            variable=variable,
            path=directory,
            extra={"file": str(path)},
        ) from exc


def _check_shape(logical: str, data: Any, *, variable: str | None, directory: str, path: Path) -> None:
    """The provider's schema check: the top-level shape each file must have."""
    def bad(reason: str) -> None:
        raise _fail(reason, variable=variable, path=directory, extra={"file": str(path)})

    if not isinstance(data, dict):
        bad(f"{REGISTRY_FILES[logical]} is not a JSON object")
    if logical == "controls":
        if not isinstance(data.get("controls"), list) or not data["controls"]:
            bad("vital_controls.json has no non-empty 'controls' list")
        if not isinstance(data.get("n_params_total"), int):
            bad("vital_controls.json has no integer 'n_params_total'")
    elif logical == "modules":
        if not isinstance(data.get("modules"), dict) or not data["modules"]:
            bad("vital_modules.json has no non-empty 'modules' object")
    elif logical == "correspondence":
        rows = data.get("rows")
        if not isinstance(rows, list) or not rows:
            bad("vital_correspondence.json has no non-empty 'rows' list")
        for row in rows[:5]:
            if not isinstance(row, dict) or "kind" not in row:
                bad("a correspondence row has no 'kind'")
    elif logical == "laws":
        if not isinstance(data.get("scales"), dict) or not data["scales"]:
            bad("vital_scales_full.json has no non-empty 'scales' object")
    elif logical == "capabilities":
        if not isinstance(data.get("sources"), dict) or not isinstance(data.get("destinations"), dict):
            bad("modulation_vocabulary.json has no 'sources'/'destinations' objects")


def load_registry(*, directory: str | None = None, env: Mapping[str, str] | None = None) -> VitalRegistry:
    """Resolve, validate and read the registry (08 §6.1-§6.2).

    Every failure is a typed `IncompatibleSettings` naming the variable and the
    resolved path; nothing falls back to another directory, and nothing is read
    at import time (this runs on first use only).
    """
    variable = None if directory is not None else REGISTRY_DIR_ENV
    if directory is None:
        environ = os.environ if env is None else env
        variable = REGISTRY_DIR_ENV if environ.get(REGISTRY_DIR_ENV) is not None else None
        resolved = resolve_registry_dir(env)
    else:
        resolved = os.path.normpath(os.path.abspath(str(directory)))
    folder = Path(resolved)
    if not folder.exists():
        raise _fail(
            "the directory does not exist" if variable else "the default directory does not exist",
            variable=variable,
            path=resolved,
        )
    if not folder.is_dir():
        raise _fail("the path is not a directory", variable=variable, path=resolved)

    registry = VitalRegistry(directory=resolved)
    for logical, name in REGISTRY_FILES.items():
        path = folder / name
        if not path.exists():
            raise _fail(
                f"the expected registry file {name} is missing", variable=variable, path=resolved,
                extra={"file": str(path)},
            )
        data = _read_json(path, variable=variable, directory=resolved, logical=logical)
        _check_shape(logical, data, variable=variable, directory=resolved, path=path)
        registry.files[logical] = str(path)
        registry.data[logical] = data
        registry.sources.append({"file": name, "path": str(path), "sha256": _sha256_file(path)})

    meta = (registry.data.get("modules") or {}).get("_meta") or {}
    version = meta.get("vital_version")
    registry.version = str(version) if version else None
    return registry


# ---------------------------------------------------------------------------
# Profile building (07 §1)
# ---------------------------------------------------------------------------


def _control_type(row: dict[str, Any]) -> str:
    return "enum" if row.get("kind") == "enum_index" else "number"


def _law(row: dict[str, Any]) -> dict[str, Any]:
    kind = row.get("kind")
    if kind == "affine":
        return {
            "kind": "affine",
            "params": {
                "scale": 1.0,
                "offset": 0.0,
                "native_min": row.get("native_min"),
                "native_max": row.get("native_max"),
                "correspondence": "norm = (native - native_min) / (native_max - native_min) (registry 'kinds.affine')",
            },
        }
    if kind == "enum_index":
        return {"kind": "enum_index", "params": {}}
    if kind in ("none", "unmeasured"):
        return {"kind": str(kind), "params": {}}
    return {"kind": "unmeasured", "params": {}}


def _evidence(row: dict[str, Any], has_states: bool) -> dict[str, Any]:
    kind = row.get("kind")
    if kind in ("none", "unmeasured"):
        strength = "none"
    elif row.get("how") == "rule" or not has_states:
        strength = "weak"
    else:
        strength = "strong"
    fit = row.get("fit") or {}
    return {"strength": strength, "source": str(fit.get("source") or "vital_correspondence.json")}


def _unit_from_states(states: list[tuple[float, str]]) -> str | None:
    units = {depth_module.display_unit(display) for _norm, display in states if depth_module.display_number(display) is not None}
    if len(units) == 1:
        unit = next(iter(units))
        return unit or None
    return None


def _range_from_row(row: dict[str, Any]) -> dict[str, float] | None:
    low, high = row.get("native_min"), row.get("native_max")
    if isinstance(low, (int, float)) and isinstance(high, (int, float)) and high > low:
        return {"min": float(low), "max": float(high)}
    return None


def _enum_values(states: list[tuple[float, str]]) -> list[dict[str, str]] | None:
    seen: list[str] = []
    for _norm, display in states:
        label = str(display).strip()
        if label and label not in seen:
            seen.append(label)
    if len(seen) < 2:
        return None
    return [{"id": label, "label": label} for label in seen]


def build_profile(registry: VitalRegistry, identity: Mapping[str, Any]) -> dict[str, Any]:
    """Build the [07 §1] profile from the loaded registry and a live identity."""
    rows = registry.rows()
    scales = registry.scales()
    vocabulary = registry.vocabulary()
    modules = registry.modules()

    module_ids = [str(name) for name in modules]
    module_descriptors: list[dict[str, Any]] = []
    controls: list[dict[str, Any]] = []
    units_by_control: dict[str, str | None] = {}
    for row in rows:
        state_key = row.get("state_key")
        if not state_key:
            continue
        name = str(row.get("name") or state_key)
        states = depth_module.states_of(scales, name)
        unit = _unit_from_states(states)
        units_by_control[state_key] = unit
        module_label = str(row.get("module") or "")
        controls.append(
            {
                "id": str(state_key),
                "label": name,
                "module": module_label or None,
                "type": _control_type(row),
                "unit": unit,
                "range": _range_from_row(row),
                "discrete": bool(row.get("discrete")),
                "enum_values": _enum_values(states) if row.get("kind") == "enum_index" else None,
                "aliases": sorted({name, str(row.get("parameter") or ""), str(row.get("control") or "")} - {""} - {str(state_key)}),
                "authority": {"read": "parameter", "write": "parameter"},
                "binding": {
                    "index": row.get("index"),
                    "ident": None,  # host-provided; resolved live (P8)
                    "state_key": str(state_key),
                },
                "law": _law(row),
                "evidence": _evidence(row, bool(states)),
            }
        )
    for name in module_ids:
        module_controls = [
            control["id"] for control in controls if control["module"] == name
        ]
        module_descriptors.append(
            {
                "id": name,
                "kind": str((modules.get(name) or {}).get("type") or "module"),
                "label": name,
                "parent": None,
                "controls": module_controls,
                "state_refs": [],
                "props": {"controls": (modules.get(name) or {}).get("controls")},
            }
        )

    destinations = [str(key) for key in (vocabulary.get("destinations") or {})]
    accepted: dict[str, Any] = {}
    refused: dict[str, str] = {}
    for destination in destinations:
        fit = depth_module.depth_fit(rows, scales, destination)
        if fit.convertible:
            accepted[destination] = fit.native_per_unit
        else:
            refused[destination] = fit.why

    version = registry.version or SUPPORTED_VERSIONS[0]
    compatible = sorted({version, *SUPPORTED_VERSIONS})

    return {
        "profile_schema_version": 1,
        "provider": "vital",
        "identity": {
            "plugin_id": "vital",
            "vendor": "Vital Audio",
            "format": _VST3_FORMAT,
            "fx_name": str(identity.get("fx_name") or VITAL_FX_NAME),
            "class_id": str(identity.get("class_id") or VITAL_CLASS_ID),
            "module": str(identity.get("module") or VITAL_MODULE),
            "compatible_versions": compatible,
            "fingerprint": {"param_count": identity.get("param_count")},
        },
        "provenance": {
            "generated_utc": None,  # filled by Core
            "generator": "vital_provider/registry",
            "sources": [{"file": row["file"], "sha256": row["sha256"]} for row in registry.sources],
            "measured_on": {
                "host": "reaper",
                "version": version,
                "evidence": ["b4_state_blob.json", "vital_correspondence.json", "vital_scales_full.json"],
                "registry_dir": registry.directory,
            },
        },
        "modules": module_descriptors,
        "controls": controls,
        "values": {
            "kinds": ["affine", "enum_index", "exponential", "identity", "none", "unmeasured"],
            "unit_traps": ["%"],
        },
        "modulation": {
            "supported": True,
            "identity": "slot",
            "slots": codec_module.ROUTE_SLOTS,
            "sources": [str(key) for key in (vocabulary.get("sources") or {})],
            "destinations": destinations,
            "pairs_with_precedent": {
                str(key): value for key, value in list((vocabulary.get("pairs") or {}).items())[:200]
            },
            "source_family_rules": {
                family: {"members": members} for family, members in (vocabulary.get("families") or {}).items()
            },
            "families": dict(vocabulary.get("families") or {}),
            "vocabulary_rules": dict(vocabulary.get("rules") or {}),
            "depth_model": {
                "kind": "per_destination_fit",
                "accepted": len(accepted),
                "refused": len(refused),
                "refused_destinations": sorted(refused)[:80],
                "table": {key: value for key, value in list(accepted.items())[:400]},
                "evidence": "vital_scales_full.json measured state lists (ported fit)",
            },
            "write_channel": "state",
            "fields": ["amount", "bipolar", "stereo", "power", "bypass", "line_mapping"],
            "line_mapping_write_supported": False,
            "structural_limits": {"one_route_per_state_write": True},
        },
        "state": {
            "supported": True,
            "codec": codec_module.VITAL_CODEC_NAME,
            "derived_leaves": list(codec_module.DERIVED_LEAVES),
            "structural_keys": [
                "settings.modulations",
                "settings.wavetables",
                "settings.sample",
                "settings.custom_warps",
                "settings.lfos",
                "settings.random_values",
            ],
            "structural_ops": [
                {
                    "id": "route_create",
                    "target": codec_module.ROUTE_TABLE_KEY,
                    #: The codec can express it and the host can perform it (P8 measured a
                    #: route write live), but the layer *refuses* it today because the rule
                    #: that governs it cannot be evaluated. A capability description that
                    #: said `verified: true` without saying that was a claim the gate
                    #: contradicts (independent audit, 2026-10-11).
                    "verified": True,
                    "available": False,
                    "available_reason": "route_into_off_filter is declared but not evaluated; "
                    "the layer refuses a created pair before any write",
                    "decided_by": "dependencies_rules._guard_route_pair",
                    "limits": ["one route per state write"],
                }
            ],
            "capture_scope": {
                "channels": ["document", "routes", "parameters"],
                "parameters": {"scope": "full_vector", "ident_required": True},
                "restore_order": ["document", "parameters"],
                "verify_order": ["document", "parameters"],
                "root": codec_module.MUTABLE_ROOT,
            },
            "restore_points": "deferred_v1",
        },
        "dependencies": [
            {
                "id": rule["id"],
                "kind": rule["kind"],
                "informational": bool(rule.get("informational", False)),
                "message": rule["message"],
                "evidence": rule.get("evidence"),
                "evaluated": not rule.get("needs_live_flags", False),
            }
            for rule in rules_module.RULES
        ],
        "write_order": ["state", "parameters", "verify"],
        "verification": {
            "tolerances": {"state.float": 1e-5, "parameter.normalized": 1e-3},
            "comparators": {"enum": "exact", "number": "abs_le"},
            "timing": {"settle_s": 0.4, "retry_after_s": 2.0, "retry_count": 1},
        },
        "limits": [
            "VST2 build not supported",
            "only the measured version(s) are accepted; a live version outside the set refuses writes",
            "one route per state write",
            "no line_mapping writes",
            "a control write is issued only when the host's parameter name at the declared index "
            "matches the control (verified live on every write; a mismatch refuses)",
            "the plugin re-serializes its own sample blob (settings.sample.samples): that leaf "
            "is declared plugin-derived, is never written, and is excluded from a document "
            "restore with a named gap",
            "the rule table's live-flag rules are declared but not evaluated (see the P7 status note)",
            "creating a modulation route is refused: the rule route_into_off_filter cannot be "
            "evaluated (no measured destination families, and the filter on/off flag is live "
            "state), so a plan that creates a (source, destination) pair refuses before any write",
        ],
        "x_provider": {
            "registry_dir": registry.directory,
            "rule_table": [rule["id"] for rule in rules_module.RULES],
            "rules_not_evaluated": [row["rule"] for row in rules_module.rules_not_evaluated()],
        },
    }
