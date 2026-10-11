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

import copy
import hashlib
import json
import math
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
    "derive_measured_law",
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


#: The smallest number of measured states a derivable law may be built from. The
#: precedent (`depth.py::depth_fit`) refuses under three pairs, and the same floor
#: is right here: two points always fit a line, so two points prove nothing.
_MIN_DERIVATION_STATES = 3

#: How many labelled members a derived enum may carry. Above this the "labels" are
#: display text, not a member set, and the honest answer is a refusal.
_MAX_DERIVED_ENUM_MEMBERS = 40

#: The rounding the registry's sweep carries (change points stored to about seven
#: decimals). A band boundary comparison must allow for it, or a *correct* mapping
#: is reported as wrong: measured on the 257-member dB-step enums, where the sweep
#: stores 0.0117188 for 3/256 and the uniform arithmetic asks for 0.01171875.
_SWEEP_NORMALIZED_TOLERANCE = 1e-5


def _measured_pairs(states: list[tuple[float, str]]) -> list[tuple[float, float]] | None:
    """Every measured state as ``(normalized, numeric display)`` - or ``None``.

    ``None`` means **at least one display is not a number**, which is the signal
    that decides between the enum and the numeric path. It is deliberately
    all-or-nothing: the POC's leading-number parser reads ``"2x Octave"`` as
    ``2.0``, so a per-state filter would let a labelled control such as
    `Oscillator 1 Stack Style` (13 labels, 2 of which start with a digit) into the
    numeric path, where a free exponent fit would then be meaningless (the
    independent audit of 2026-10-11 measured exactly that leak).
    """
    pairs: list[tuple[float, float]] = []
    for norm, display in states:
        number = depth_module.display_number(display)
        if number is None:
            return None
        pairs.append((float(norm), float(number)))
    return pairs


def _measurement_quantum(values: list[float]) -> float:
    """The display's own smallest strictly-positive step (measured)."""
    distinct = sorted(set(values))
    gaps = [b - a for a, b in zip(distinct, distinct[1:]) if b - a > 0]
    return min(gaps) if gaps else 0.0


def _norm_ordered_endpoints(pairs: list[tuple[float, float]]) -> tuple[float, float] | None:
    """The display at the lowest / highest measured normalized - in that order.

    Not ``(min, max)`` of the values: the range is the *conversion spine*
    (``values.py`` maps ``low`` to normalized 0 and ``high`` to normalized 1), so
    the endpoints must follow the measurement's direction. A descending control
    would need an inverted range, which the Core contract does not express yet
    (``validate`` bounds-checks ``low <= value <= high``), so descending maps are
    refused here rather than smuggled in - see `derive_measured_law`.
    """
    if len(pairs) < 2:
        return None
    ordered = sorted(pairs, key=lambda pair: pair[0])
    low, high = ordered[0][1], ordered[-1][1]
    if high <= low:
        return None
    return low, high


def _band_spans(states: list[tuple[float, str]]) -> list[tuple[str, float, float]] | None:
    """Each measured member's own band, in the order the measurement meets it.

    The sweep records **change points**, so a member's first sample *is* the
    boundary where the neighbouring member ends: a band is
    ``[change_point[i], change_point[i+1])``. The last band has no recorded end,
    and its width is mirrored from the band before it (for a two-state switch the
    sweep has exactly two change points, so mirroring is what puts "On" at
    ``[0.5, 1.0]`` rather than ending it at the 0.5 switch point).

    Returns ``None`` for a sweep that carries fewer than two distinct labels (no
    band structure to read).
    """
    points: list[tuple[float, str]] = []
    for norm, display in sorted(states, key=lambda pair: pair[0]):
        label = str(display).strip()
        if points and points[-1][1] == label:
            continue
        points.append((float(norm), label))
    if len(points) < 2:
        return None
    spans: list[tuple[str, float, float]] = []
    for index, (start, label) in enumerate(points):
        if index + 1 < len(points):
            end = points[index + 1][0]
        else:
            width = start - points[index - 1][0] if index else 1.0
            end = min(1.0, start + width)
        spans.append((label, start, end))
    return spans


def _band_positions(states: list[tuple[float, str]], members: list[str]) -> list[float] | None:
    """One measured normalized position per member: the middle of its own band.

    The enum conversion in Core assumes uniformly spaced members. For
    ``Oscillator N Distortion Type`` that assumption is false and measurably so:
    the sweep shows 11 bands whose switch points run 0, 11/256, 32/256, 54/256,
    76/256, 98/256 ... with double-width gaps, and the uniform mapping
    (``index / (n - 1)``) lands 5 of the 11 members in the *neighbouring* band
    (audit of 2026-10-11, measured).

    The position must be **interior** to the band, not at its switch point
    (``_band_spans`` explains why the last band is mirrored).
    """
    spans = _band_spans(states)
    if spans is None:
        return None
    # the derived path feeds these positions to the *same* enum conversion, so it
    # needs the same guard the declared path has: one band per member, in order.
    # Without it a sweep whose label repeats in two non-adjacent bands (A, B, A)
    # would collapse in the mapping below and borrow the last A's midpoint for the
    # first (a value no band of the member owns).
    if [label for label, _start, _end in spans] != list(members):
        return None
    positions = {label: round((start + end) / 2.0, 6) for label, start, end in spans}
    if set(positions) != set(members):
        return None
    return [positions[member] for member in members]


def _declared_enum_positions(
    members: list[str], states: list[tuple[float, str]]
) -> dict[str, Any] | None:
    """The measured band positions for a **declared** ``enum_index`` row, when the
    measurement contradicts the uniform assumption - or ``None`` when it does not.

    **Why this exists (measured 2026-10-11, live).** The profile trusted the
    registry's `enum_index` declaration, whose mapping is ``index / (n - 1)``:
    uniform bands. The plugin's own sweeps say the bands are anything but uniform -
    `Filter 1 Style`'s "24dB" occupies ``[0.0625, 0.1875)`` while the uniform
    mapping puts it at **0.25**, inside "Notch Blend"'s band. The consequence was
    the defect this session had to chase by hand: writing ``filter_1_style =
    "24dB"`` *landed in "Notch Blend"* and the layer's own read-back agreed
    ("24dB!", because the same wrong law ran in both directions) - only the host's
    formatted value, an independent channel, disagreed.

    The correction is **additive and fail-closed**: it is attached only when the
    derivation produces exactly one position per declared member and at least one
    member's uniform position falls *outside* that member's own measured band. A
    uniform enum whose mapping is already right (the two-state switches, whose
    uniform 0.0/1.0 sits in ``[0, 0.5)``/``[0.5, 1.0]``) is left byte-identical -
    so nothing that works today can change.

    The count of members that *would* be mis-selected by the uniform mapping is
    carried in the profile as evidence: it is the size of the defect being fixed.
    """
    if len(members) < 2:
        return None
    raw_spans = _band_spans(states)
    if raw_spans is None:
        return None
    # A sweep can end with an *unlabelled* tail (measured: `Filter 1 Style`'s
    # sixth state, at 0.5625, has an empty display string). That region names no
    # member, so it is not a write target - but it is recorded, not hidden: the
    # labelled spans are what the positions come from, and the gap is reported.
    spans = [span for span in raw_spans if span[0]]
    unlabelled_tail = len(raw_spans) - len(spans)
    if len(spans) != len(members) or [label for label, _s, _e in spans] != list(members):
        return None
    positions = [round((start + end) / 2.0, 6) for _label, start, end in spans]
    n = len(members)
    uniform = [index / (n - 1) for index in range(n)]
    # The sweep stores change points rounded to about seven decimals, so a uniform
    # position can sit on a boundary by a *rounding* artefact (measured: the
    # 257-member dB-step enums where ``3/256`` differs from the stored boundary by
    # 5e-8). A member counts as outside only when it is outside beyond that
    # artefact - otherwise a correct mapping would be "corrected" for nothing.
    outside: list[str] = []
    for index, (label, start, end) in enumerate(spans):
        value = uniform[index]
        if not (start - _SWEEP_NORMALIZED_TOLERANCE <= value <= end + _SWEEP_NORMALIZED_TOLERANCE):
            outside.append(f"{label}@{value:g} outside [{start:g}, {end:g})")
    if not outside:
        return None
    return {
        "positions": positions,
        #: The members' own band starts, for the *read* inversion. The host labels a
        #: value exactly at a switch point as the member that starts there (measured:
        #: normalized 0.0625 formats as "24dB", not as the member below it), so the
        #: read path must use these boundaries, not the distance to a midpoint.
        "switch_points": [round(start, 9) for _label, start, _end in spans],
        "positions_source": (
            "measured band midpoints of vital_scales_full.json: the declared enum_index law is "
            "uniform (index / (n - 1)) and the measurement is not"
        ),
        "n_states": len(states),
        "declared_mapping": "uniform index / (n - 1)",
        "members_the_declared_mapping_puts_outside_their_band": len(outside),
        "first_outside": outside[:3],
        "unlabelled_spans_in_the_sweep": unlabelled_tail,
    }


def _members_in_norm_order(states: list[tuple[float, str]]) -> list[str] | None:
    """The distinct labels in the order the measurement meets them."""
    members: list[str] = []
    for _norm, display in sorted(states, key=lambda pair: pair[0]):
        label = str(display).strip()
        if label and label not in members:
            members.append(label)
    return members or None


def derive_measured_law(row: dict[str, Any], states: list[tuple[float, str]]) -> dict[str, Any] | None:
    """A law fitted to the control's **own measured sweep**, or ``None``.

    Called only for rows whose declared ``kind`` is ``none``/``unmeasured`` - i.e.
    rows for which ``values.to_native`` refuses *every* conversion today. The
    derivation can therefore only add capability; the rows that already declare a
    law (`affine`/`enum_index`, 582 of them) are never touched (audit finding:
    "strictly additive", 2026-10-11).

    The measurement (`vital_scales_full.json`) is the registry's own 257-point
    sweep of the control, recorded as ``[normalized, display-string]`` change
    points; the fit's acceptance bound is the same one `depth.py::depth_fit` uses
    for modulation depth - the worst residual of the fit must stay inside half of
    the display's own smallest step - so a curve the declared vocabulary cannot
    express is **refused**, never approximated.

    Order of decision (each with its guard, all evidence from the sweep):

    1. any non-numeric display -> a labelled member set (`enum_index` with the
       measured member order and the measured band positions);
    2. numeric and non-decreasing -> an affine fit inside the bound;
    3. numeric, positive, non-decreasing, low > 0 -> an exponential fit;
    4. numeric, non-negative, non-decreasing, base = max > 0, exponent > 0 with at
       least two distinct *non-zero* points -> a **power** fit (the measured
       taper: `Oscillator N Level` is exactly `display = normalized ** 2`, and
       `Oscillator N Unison Detune` is `100 * normalized ** 2`);
    5. anything else (descending, mixed labels, too few states, no fit) -> ``None``
       (the honest refusal stays).
    """
    if len(states) < 2:
        return None

    pairs = _measured_pairs(states)
    if pairs is None:
        members = _members_in_norm_order(states)
        if members is None or not (2 <= len(members) <= _MAX_DERIVED_ENUM_MEMBERS):
            return None
        positions = _band_positions(states, members)
        if positions is None:
            return None
        return {
            "kind": "enum_index",
            "type": "enum",
            "discrete": True,
            "enum_values": [{"id": member, "label": member} for member in members],
            "range": None,
            "params": {
                "positions": positions,
                "positions_source": "measured band midpoints of vital_scales_full.json",
                "n_states": len(states),
            },
        }

    # a *numeric* law is a curve, and two points always fit one: three states is
    # the floor `depth.py::depth_fit` already uses for the same reason
    if len(states) < _MIN_DERIVATION_STATES:
        return None

    values = [value for _norm, value in pairs]
    quantum = _measurement_quantum(values)
    bound = 0.5 * quantum + 1e-9
    ordered = sorted(pairs, key=lambda pair: pair[0])
    if any(b[1] < a[1] for a, b in zip(ordered, ordered[1:])):
        # descending (or non-monotone) display: the range/spine model in
        # `values.py` cannot state that direction yet, so refuse instead of
        # writing an inverted mapping that a round-trip would not reveal
        return None
    endpoints = _norm_ordered_endpoints(pairs)
    if endpoints is None:
        return None
    low, high = endpoints

    fit = depth_module.linear_fit(pairs)
    if fit is not None:
        slope, intercept, residual = fit
        if residual <= bound:
            return {
                "kind": "affine",
                "type": "number",
                "enum_values": None,
                "range": {"min": low, "max": high},
                "params": {
                    "scale": 1.0,
                    "offset": 0.0,
                    "measured_slope": round(float(slope), 9),
                    "measured_intercept": round(float(intercept), 9),
                    "fit_residual": round(float(residual), 9),
                    "fit_bound": round(float(bound), 12),
                    "source": "vital_scales_full.json (measured sweep fit)",
                    "n_states": len(states),
                },
            }

    if low > 0.0 and high > 0.0:
        residual = max(
            abs(value - low * (high / low) ** norm) for norm, value in pairs
        )
        if residual <= bound:
            return {
                "kind": "exponential",
                "type": "number",
                "enum_values": None,
                "range": {"min": low, "max": high},
                "params": {
                    "scale": 1.0,
                    "offset": 0.0,
                    "fit_residual": round(float(residual), 9),
                    "fit_bound": round(float(bound), 12),
                    "source": "vital_scales_full.json (measured sweep fit)",
                    "n_states": len(states),
                },
            }

    base = max(values)
    # the exponent is fitted on the strictly-interior points: norm 0 has no
    # logarithm and norm 1 pins the base by construction (`math.log(1.0) == 0.0`
    # is a ZeroDivisionError, measured 2026-10-11 while building this fit)
    exponent_points = [
        (norm, value) for norm, value in pairs if 0.0 < norm < 1.0 and value > 0.0
    ]
    distinct_values = sorted({value for _norm, value in exponent_points})
    if base > 0.0 and len(exponent_points) >= 2 and len(distinct_values) >= 2:
        exponent = sum(
            math.log(value / base) / math.log(norm) for norm, value in exponent_points
        ) / len(exponent_points)
        if exponent > 0.0:
            residual = max(abs(value - base * norm**exponent) for norm, value in pairs)
            if residual <= bound:
                return {
                    "kind": "power",
                    "type": "number",
                    "enum_values": None,
                    "range": {"min": low, "max": high},
                    "params": {
                        "base": float(base),
                        "exponent": round(float(exponent), 9),
                        "fit_residual": round(float(residual), 9),
                        "fit_bound": round(float(bound), 12),
                        "source": "vital_scales_full.json (measured sweep fit)",
                        "n_states": len(states),
                    },
                }
    return None


def _evidence(
    row: dict[str, Any], has_states: bool, *, measured: dict[str, Any] | None = None
) -> dict[str, Any]:
    kind = row.get("kind")
    if measured is not None:
        # the law now comes from the control's own measured sweep, so the evidence
        # is as strong as this registry gets - and names its source
        return {
            "strength": "strong",
            "source": str(measured.get("params", {}).get("source") or "vital_scales_full.json"),
        }
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


#: Where the live corrections below were measured (the artifact beside this one).
_LIVE_CALIBRATION_SOURCE = (
    "live REAPER formatted-display calibration 2026-10-11 "
    "(tests/plugins/live/evidence/p18_calibration__20261011T114948Z__c91845f34b01.json)"
)

#: The `voice_transpose` measurement lives in its own run: it needed a written
#: value *and* a render to see the +10 semitones the declared range produced.
_VOICE_TRANSPOSE_SOURCE = (
    "live voice_transpose calibration 2026-10-11 "
    "(tests/plugins/live/evidence/p19_voice_transpose_space__20261011T134428Z__41f60fa0816b.json: "
    "written 24/0/-24/12/-12/-6 -> the plugin displayed +48/+10/-29/+29/-10/0)"
)


def _live_measured_corrections() -> dict[str, dict[str, Any]]:
    """Rows whose registry-declared range/law a **live host measurement**
    contradicted (2026-10-11, Vital 1.5.5, track 1 / fx 0).

    The registry's ranges come from the *observed preset corpus*
    (``phase2b_native_ranges.json``), so they truncate every parameter at the
    largest value any preset ever used. A live calibration - writing known
    host-normalized values through the classic ``set_fx_param`` and reading the
    plugin's own formatted display - measured these disagreements:

    * ``portamento_time`` stops at -0.476 in the registry (0.72 s) although the
      parameter runs to +4 (16 s): measured *0.000976562 s at 0.0, 0.125 s at
      0.5, 0.900531 s at 0.7035, 6.06286 s at 0.9 and 16 s at 1.0* - every anchor
      is exactly ``2 ** (-10 + 14 * normalized)`` (the plugin's own kExponential
      base-2 family). A write of -3.3 under the truncated range therefore landed
      at 0.9005 s instead of 0.1 s: a factor of nine, and the acceptance render
      measured the resulting slow crawl before the fix.
    * ``portamento_slope``: measured *-4 at 0.25, +4 at 0.75, +8 at 1.0* -> the
      range is -8..+8, not -4.5..+8.
    * envelope attack/decay/release: measured *0.125 s at 0.25, 2.00002 s at
      0.5, 10.1251 s at 0.75, 32.0003 s at 1.0* -> ``seconds = 32.0003 *
      normalized ** 4`` (the plugin's kQuartic family), not the declared linear
      0..2.378 s. A declared "0.35 s" decay actually rendered 15 ms.
    * ``volume``: measured *-80 dB at 0.0, -36.99 dB at 0.25, -19.1747 dB at
      0.5, -5.50449 dB at 0.75, +6.02 dB at 1.0* ->
      ``dB = -80 + 86.02 * normalized ** 0.5`` (the plugin's kSquareRoot
      family); the registry's 0..7399.44 range is nonsense (a corpus artifact)
      and its linear law cannot express the curve at all. The power law
      therefore carries an optional ``offset`` (here -80 dB).
    * ``voice_transpose``: the registry declares the **corpus** range -36..24,
      so the profile maps a caller's value into normalized ``(value + 36) / 60``
      and the corpus's neutral is nowhere near the parameter's. Measured on the
      live instance (2026-10-11, `p19_voice_transpose_space__*`): writing 24, 0,
      -24, 12, -12, -6 read the plugin's own display **+48, +10, -29, +29, -10,
      0 semitones** - i.e. ``display = 1.6 * value + 9.6``, the display's own
      ±48 with its zero at value -6. The declared range therefore made every
      restore that wrote the documented neutral ``0.0`` play **+10 semitones
      sharp** (measured: a route-free table, in tune at 0.04-0.06 st before the
      write). Corrected to the plugin's own space: the caller's value **is** the
      displayed semitone offset, ``range -48..+48`` (zero at normalized 0.5).

    Only measured rows are listed; every other row keeps the registry's
    declaration (strictly additive - never a guessed mapping).
    """
    table: dict[str, dict[str, Any]] = {}
    for index in range(1, 7):
        for section in ("attack", "decay", "release"):
            table[f"env_{index}_{section}"] = {
                "law": {
                    "kind": "power",
                    "params": {
                        "base": 32.0003,
                        "exponent": 4.0,
                        "source": _LIVE_CALIBRATION_SOURCE,
                    },
                },
                "range": {"min": 0.0, "max": 32.0003},
                "source": _LIVE_CALIBRATION_SOURCE,
            }
    table["portamento_time"] = {
        "law": {
            "kind": "exponential",
            "params": {"scale": 1.0, "offset": 0.0, "source": _LIVE_CALIBRATION_SOURCE},
        },
        "range": {"min": 2.0**-10, "max": 2.0**4},
        "source": _LIVE_CALIBRATION_SOURCE,
    }
    table["portamento_slope"] = {
        "law": {
            "kind": "affine",
            "params": {
                "scale": 1.0,
                "offset": 0.0,
                "native_min": -8.0,
                "native_max": 8.0,
                "correspondence": "display = -8 + 16 * normalized (measured: -4 at 0.25, +8 at 1.0)",
                "source": _LIVE_CALIBRATION_SOURCE,
            },
        },
        "range": {"min": -8.0, "max": 8.0},
        "source": _LIVE_CALIBRATION_SOURCE,
    }
    table["volume"] = {
        "law": {
            "kind": "power",
            "params": {
                "base": 86.02,
                "exponent": 0.5,
                "offset": -80.0,
                "source": _LIVE_CALIBRATION_SOURCE,
            },
        },
        "range": {"min": -80.0, "max": 6.02},
        "source": _LIVE_CALIBRATION_SOURCE,
    }
    table["voice_transpose"] = {
        "law": {
            "kind": "affine",
            "params": {
                "scale": 1.0,
                "offset": 0.0,
                "native_min": -48.0,
                "native_max": 48.0,
                "correspondence": (
                    "the caller's value IS the displayed semitone offset: measured +48 at "
                    "normalized 1.0, 0 at 0.5, -29 at 0.2 (and the declared-space values 24/0/"
                    "-24/12/-12/-6 read +48/+10/-29/+29/-10/0)"
                ),
                "source": _VOICE_TRANSPOSE_SOURCE,
            },
        },
        "range": {"min": -48.0, "max": 48.0},
        "source": _VOICE_TRANSPOSE_SOURCE,
    }
    return table


#: Control id -> the correction the live calibration measured (built once).
LIVE_MEASURED_CORRECTIONS: dict[str, dict[str, Any]] = _live_measured_corrections()


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
        declared_law = _law(row)
        declared_range = _range_from_row(row)
        # A declared law is trusted when it can convert: `none`/`unmeasured` refuse
        # everything, and an affine row whose declared bounds are missing or
        # degenerate (``native_min == native_max`` - measured on the 42
        # ``modulation_NN_power`` rows, where the registry's own evidence row says
        # the range is -10..+10) converts as an identity, which is not a law.
        # Both cases are answered from the sweep; a declared law with a usable
        # range is never re-derived.
        needs_measurement = declared_law["kind"] in ("none", "unmeasured") or (
            declared_law["kind"] == "affine" and declared_range is None
        )
        measured = derive_measured_law(row, states) if needs_measurement else None
        #: set only for a declared `enum_index` row whose *measured* bands are not
        #: uniform (see `_declared_enum_positions`); carried into the evidence
        positions_correction: dict[str, Any] | None = None
        if measured is not None:
            law = {"kind": str(measured["kind"]), "params": dict(measured.get("params") or {})}
            control_type = str(measured.get("type") or _control_type(row))
            range_spec = measured.get("range") if measured.get("range") is not None else _range_from_row(row)
            discrete = bool(measured.get("discrete")) or bool(row.get("discrete"))
            enum_values = measured.get("enum_values")
        else:
            law = declared_law
            control_type = _control_type(row)
            range_spec = _range_from_row(row)
            discrete = bool(row.get("discrete"))
            enum_values = _enum_values(states) if row.get("kind") == "enum_index" else None
            # A declared `enum_index` law is uniform by construction; where the
            # measurement shows the plugin's bands are not, the measured positions
            # replace the arithmetic (and *only* the positions: the declared kind
            # and the member list stay). Without this, a write lands in the
            # neighbouring band and the read-back agrees with it, because the same
            # arithmetic runs in both directions (measured: `filter_1_style` "24dB"
            # -> "Notch Blend", 2026-10-11).
            positions_correction = None
            if law["kind"] == "enum_index" and enum_values:
                positions_correction = _declared_enum_positions(
                    [str(member["id"]) for member in enum_values], states
                )
                if positions_correction is not None:
                    law = {
                        "kind": str(law["kind"]),
                        "params": {**dict(law.get("params") or {}), **positions_correction},
                    }
        correction = LIVE_MEASURED_CORRECTIONS.get(str(state_key))
        if correction is None:
            evidence = _evidence(row, bool(states), measured=measured)
            if positions_correction is not None:
                evidence = {**evidence, "positions_correction": dict(positions_correction)}
        else:
            # a live host measurement contradicted the registry's declaration:
            # the measurement wins (see `_live_measured_corrections`)
            law = copy.deepcopy(correction["law"])
            range_spec = dict(correction["range"])
            evidence = {"strength": "strong", "source": str(correction["source"])}
        controls.append(
            {
                "id": str(state_key),
                "label": name,
                "module": module_label or None,
                "type": control_type,
                "unit": unit,
                "range": range_spec,
                "discrete": discrete,
                "enum_values": enum_values,
                "aliases": sorted({name, str(row.get("parameter") or ""), str(row.get("control") or "")} - {""} - {str(state_key)}),
                "authority": {"read": "parameter", "write": "parameter"},
                "binding": {
                    "index": row.get("index"),
                    "ident": None,  # host-provided; resolved live (P8)
                    "state_key": str(state_key),
                },
                "law": law,
                "evidence": evidence,
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

    # The two joins `route_into_off_filter` needs to be *decided* rather than
    # refused (2026-10-11). Both come from the registry the profile is built
    # from, never from a typed list:
    #
    # - ``destination_modules``: every vocabulary destination joined to the
    #   correspondence row that carries its ``state_key`` (measured on this
    #   registry: 316 of 316 destinations resolve - the coverage is recorded so a
    #   destination the join misses makes the evaluator fail closed instead of
    #   silently passing);
    # - ``module_switches``: per module, the state key of the row the registry
    #   itself calls a ``Switch`` (15 modules today, e.g. Filter 1 -> filter_1_on).
    module_by_key = {
        str(row.get("state_key")): str(row.get("module"))
        for row in rows
        if row.get("state_key") and row.get("module")
    }
    destination_modules = {dest: module_by_key[dest] for dest in destinations if dest in module_by_key}
    unmapped_destinations = sorted(dest for dest in destinations if dest not in destination_modules)
    module_switches = {
        str(row.get("module")): str(row.get("state_key"))
        for row in rows
        if row.get("state_key")
        and row.get("module")
        and str(row.get("control") or "").strip().lower() == "switch"
    }

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
            "kinds": ["affine", "enum_index", "exponential", "identity", "none", "power", "unmeasured"],
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
            "destination_modules": destination_modules,
            "destination_module_coverage": {
                "mapped": len(destination_modules),
                "total": len(destinations),
                "unmapped": unmapped_destinations[:20],
                "source": "vital_correspondence.json rows joined on state_key (measured for this build)",
            },
            "module_switches": module_switches,
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
                    #: route write live). It **became available** on 2026-10-11 when the
                    #: rule that governs it stopped being a refusal: the destination->module
                    #: join and the projected filter on/off flag are now inputs, so the rule
                    #: is decided (see `dependencies_rules._evaluate_route_pairs`) and only a
                    #: decided violation refuses.
                    "verified": True,
                    "available": True,
                    "available_reason": None,
                    "decided_by": "dependencies_rules._evaluate_route_pairs",
                    "evidence": "P8.4 state-write live evidence + the 2026-10-11 evaluation (destination-module join 316/316; switches from the registry's own Switch rows)",
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
                #: one source of truth: the provider's own EVALUATION table, the same
                #: one its evaluator reads. `needs_live_flags` remains on the measured
                #: rows as data; whether the rule is *decided* is a property of the
                #: evaluator, not of the rule's physics.
                "evaluated": rules_module.rule_evaluated(str(rule["id"])),
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
            "the rule table's live-flag rules `fm_needs_modulator` and "
            "`wave_frame_needs_multiframe_wavetable` are declared but not evaluated (see the P7 "
            "status note); `route_into_off_filter` is evaluated since 2026-10-11 (destination->module "
            "join + the projected module on/off flag), and every undecidable subject still refuses",
        ],
        "x_provider": {
            "registry_dir": registry.directory,
            "rule_table": [rule["id"] for rule in rules_module.RULES],
            "rules_not_evaluated": [row["rule"] for row in rules_module.rules_not_evaluated()],
        },
    }
