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
import re
import struct
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
    "registry_source_rows",
    "load_registry",
    "derive_measured_law",
    "derive_law_candidate",
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

    def sweep_steps(self) -> int | None:
        """How many steps the registry's sweep walked (``vital_scales_full.json``).

        The file records it (`steps: 256`, so 257 sampled positions) and the number
        is the structural evidence `_sampled_ramp` needs: a control whose display
        changed at *every* step is a ramp, one that changed at 49 of 256 is a
        banded member set. Absent (a foreign registry), the predicate declines.
        """
        steps = (self.data.get("laws") or {}).get("steps")
        if isinstance(steps, bool) or not isinstance(steps, int) or steps < 1:
            return None
        return int(steps)

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


def registry_source_rows(
    *, directory: str | None = None, env: Mapping[str, str] | None = None
) -> list[dict[str, Any]]:
    """The registry's provenance rows **without parsing the data** (09 §3).

    Same resolution order, same refusals and the same ``{"file", "sha256"}`` rows
    `load_registry` records - but the files are read only to be hashed, so a
    caller that needs the `source_digest` (a cache-key determinant) does not pay
    the ~6.5 MB JSON parse that a profile build performs. A warm cache hit runs
    this instead of `load_registry`.

    `load_registry` stays the authority for the registry *data*. That this
    function's rows equal the rows `load_registry` records is pinned by a test
    against the fixture registry, and a drift is caught at runtime in
    `resolve.resolve_instance_profile`: the profile a build produces must
    re-derive the very key this declaration produced, or the cache write is
    refused with `cache.key_material` named.
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

    rows: list[dict[str, Any]] = []
    for name in REGISTRY_FILES.values():
        path = folder / name
        if not path.exists():
            raise _fail(
                f"the expected registry file {name} is missing",
                variable=variable,
                path=resolved,
                extra={"file": str(path)},
            )
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise _fail(
                f"{name} cannot be read: {exc}", variable=variable, path=resolved,
                extra={"file": str(path)},
            ) from exc
        rows.append({"file": name, "sha256": hashlib.sha256(data).hexdigest()})
    return rows


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

#: The resolution the sweep writes its *positions* at (measured 2026-10-12: six
#: decimals - 1/256 is stored as ``0.003906``). What it costs the fit is not a
#: tolerance but a *measurement*: the sweep is a uniform grid of ``n`` points and
#: the file stores each one rounded, so the position the display was produced at
#: is recoverable exactly (``_exact_sampled_positions``) - where the stored
#: rounding is used as if it were the position, a steep law is measurably wrong.
#: Measured: on ``random_1_frequency`` (the 2**16 ratio law, 128 s ->
#: 0.00195312 s) the steep end moves by ``128 * 16 * ln(2) * 5e-7 = 5.5e-4`` -
#: 11x the state's printed precision, and enough to refuse a law every one of the
#: 257 states obeys.
_SWEEP_POSITION_DECIMALS = 6
_SWEEP_POSITION_QUANTUM = 10.0 ** (-_SWEEP_POSITION_DECIMALS)

#: What may follow a number in a *faithful* numeric display: one unit-like token
#: (letters, or the micro/degree/percent/hash signs) and nothing else. ``"/2"``,
#: ``"x Octave"`` and ``" of 16"`` are labels wearing a number, not a numeric
#: scale.
_UNIT_TOKEN_RE = re.compile(r"[A-Za-z\u00b5\u00b0#%]+")

#: The number at the start of a display, split so its **own printed precision**
#: can be read off the text (``122.573`` carries +/-0.0005, ``128`` +/-0.5).
_NUMBER_TEXT_RE = re.compile(
    r"^(?P<sign>[+-]?)(?:(?P<int>\d+)(?:\.(?P<frac>\d*))?|\.(?P<frac_only>\d+))(?P<exp>[eE][+-]?\d+)?"
)

#: A printed magnitude beyond this cannot be a measurement of a plugin parameter.
_MAX_DECIMAL_EXPONENT = 30


def _scalar_subset(block: Any, names: tuple[str, ...]) -> dict[str, Any]:
    """The named fields of ``block``, keeping only JSON scalars.

    The calibration artifact is a *source*, and a source is data, not a licence to
    paste an arbitrarily large object into the profile: a 500 MB ``read_only_proof``
    would otherwise become 500 MB of validating cache content. Only the fields the
    profile's own consumers read are carried over, and each must be a scalar.
    """
    if not isinstance(block, dict):
        return {}
    subset: dict[str, Any] = {}
    for name in names:
        value = block.get(name)
        if isinstance(value, bool) or isinstance(value, (int, float, str)):
            subset[name] = value
    return subset


def _display_precision(display: Any, remainder: str) -> float | None:
    """Half the reading resolution of the display's *own text*, in its units.

    The sweep records what the plugin printed, and a printed number is exact only
    to the digits it shows: ``"122.573 secs"`` is +/-0.0005, ``"0.0024255
    secs"`` is +/-5e-8. An acceptance bound of *half the display's smallest
    step* is the right instrument model for a narrow range, but it is provably
    wrong for a display spanning five decades: measured 2026-10-11,
    ``random_N_frequency`` (128 s -> 0.0019 s, ratio 2**16) was refused although
    every one of its 257 states sits on the exponential to within the printed
    digits - the residual was the *text's* rounding at magnitude 128 (5e-4), not
    a shape error.
    """
    text = str(display).strip()
    number_text = text[: len(text) - len(remainder)].strip() if remainder else text
    match = _NUMBER_TEXT_RE.match(number_text)
    if match is None:
        return None
    decimals = len(match.group("frac") or match.group("frac_only") or "")
    exponent = int(match.group("exp")[1:]) if match.group("exp") else 0
    if abs(exponent - decimals) > _MAX_DECIMAL_EXPONENT:
        # a magnitude no display prints: unreadable, and not a huge tolerance
        # (the power below would overflow on a hostile "1e999999")
        return None
    return 0.5 * 10.0 ** (exponent - decimals)


def _measurement_entries(
    states: list[tuple[float, str]],
) -> list[tuple[float, float, float]] | None:
    """``(normalized, numeric display, printed precision)`` - or ``None``.

    The faithfulness rule of `_measured_pairs` plus the display's own reading
    precision, which the fit's acceptance bound uses **per state**.
    """
    entries: list[tuple[float, float, float]] = []
    for norm, display in states:
        number = depth_module.display_number(display)
        if number is None:
            return None
        remainder = depth_module.display_unit(display)
        if remainder and not _UNIT_TOKEN_RE.fullmatch(remainder):
            return None
        precision = _display_precision(display, remainder)
        if precision is None:
            return None
        entries.append((float(norm), float(number), float(precision)))
    return entries


def _worst_fit_ratio(
    predict: Any,
    oriented: list[tuple[float, float, float]],
    floor: float,
    storage_term: float = 0.0,
) -> tuple[float, float, float]:
    """``(worst residual, worst residual / its own bound, the largest bound)``.

    Each state is judged against **its own** reading resolution (never below the
    half-step floor, so every row that was accepted before stays accepted), and
    the ratio is what the profile records: it says how close to the instrument's
    limit the fit is, which an absolute residual cannot express across decades.

    Two further terms the display's *text* carries are added by the caller:
    ``storage_term`` (half a float32 ulp - the plugin stores a 32-bit float and
    the sweep's text is a print of it, measured 2026-10-12) and the position the
    text was produced at, which `_exact_sampled_positions` recovers rather than
    approximating. Without the storage term a state can miss by 0.5 print
    quanta *plus* the storage rounding and be refused for the instrument's own
    arithmetic: measured on ``random_1_frequency`` (worst ratio 1.0754 with 1 of
    257 states over, at a state whose position is exact).
    """
    worst_abs = 0.0
    worst_ratio = 0.0
    worst_bound = 0.0
    for norm, value, precision in oriented:
        bound = max(precision, floor, 1e-15) + storage_term
        residual = abs(value - predict(norm))
        worst_abs = max(worst_abs, residual)
        worst_ratio = max(worst_ratio, residual / bound)
        worst_bound = max(worst_bound, bound)
    return worst_abs, worst_ratio, worst_bound


#: A VST3 parameter is a 32-bit float and the plugin's displayed value is a print
#: of one: rounding the *true* value onto the float32 grid costs up to half an ulp,
#: which the printed precision does not cover. Measured 2026-10-12 on
#: ``random_1_frequency``: the state that missed by 7.5 % of its budget is explained
#: by exactly this term plus the print's own half quantum.
_FLOAT32_MANTISSA_BITS = 24


def _float32_ulp(value: float) -> float:
    """The spacing of the float32 grid at ``value`` (0.0 for a non-finite input)."""
    magnitude = abs(float(value))
    if not math.isfinite(magnitude):
        return 0.0
    if magnitude == 0.0:
        return float.fromhex("0x1p-149")
    exponent = math.floor(math.log2(magnitude))
    ulp = 2.0 ** (exponent - (_FLOAT32_MANTISSA_BITS - 1))
    return ulp if math.isfinite(ulp) else 0.0


def _float32_storage_term(values: list[float]) -> float:
    """Half a float32 ulp at the largest printed magnitude (the worst state)."""
    if not values:
        return 0.0
    return 0.5 * _float32_ulp(max(abs(value) for value in values))


def _exact_sampled_positions(
    entries: list[tuple[float, float, float]],
) -> list[tuple[float, float, float]] | None:
    """The sweep's positions as the sampling plan fixes them, or ``None``.

    The sweep walks a uniform grid of ``len(states)`` points and the file stores
    each position rounded to six decimals, so the position a state's display was
    really produced at is ``i / (n - 1)`` - recoverable exactly, not approximately.
    Recognised only when *every* stored position is within half of the storage
    quantum of that grid; otherwise ``None`` and the stored positions are used as
    recorded (fail-closed: an unrecognised sweep gets no correction at all, which
    is what the member enums - whose change points are band boundaries, not grid
    points - need).
    """
    count = len(entries)
    if count < _MIN_DERIVATION_STATES:
        return None
    stored = [float(entry[0]) for entry in entries]
    # Strictly ascending *as recorded*: the plan walks the grid forward, and the
    # enumeration below pairs ``entries`` (not the sorted list) with ``i / (n - 1)`` -
    # an entries list that were only a permutation of the grid would otherwise pair
    # every display with another state's position, silently (reviewer's P24 note;
    # unreachable with the registry's forward sweeps, refused rather than trusted).
    if any(later <= earlier for earlier, later in zip(stored, stored[1:])):
        return None
    half = 0.5 * _SWEEP_POSITION_QUANTUM + 1e-12
    for index, position in enumerate(stored):
        if abs(position - index / (count - 1)) > half:
            return None
    return [
        (index / (count - 1), value, precision)
        for index, (_norm, value, precision) in enumerate(entries)
    ]


def _g6(value: float) -> str:
    """The plugin's own display format: six significant digits (measured)."""
    return "%.6g" % float(value)


def _float32(value: float) -> float:
    """``value`` rounded onto the float32 grid."""
    return struct.unpack("<f", struct.pack("<f", float(value)))[0]


def _printed_text_reproduced(
    predict: Any,
    states: list[tuple[float, str]],
    positions: dict[float, float] | None = None,
) -> dict[str, Any]:
    """How many printed states the law reproduces **digit for digit**.

    The strongest form of the display model this provider can state: the plugin
    stores a float32, prints it with ``%.6g``, and the law predicts both steps. A
    residual says "within the display's resolution"; this says "the same string",
    and is carried as evidence rather than as the acceptance test (a plugin build
    that prints differently would fail the count, not the law).
    """
    matched = 0
    compared = 0
    first_miss: dict[str, Any] | None = None
    for norm, display in states:
        number = depth_module.display_number(display)
        if number is None:
            continue
        remainder = depth_module.display_unit(display)
        text = str(display).strip()
        number_text = text[: len(text) - len(remainder)].strip() if remainder else text
        position = float(norm) if positions is None else positions.get(round(float(norm), 12), float(norm))
        compared += 1
        predicted = _g6(_float32(predict(position)))
        if predicted == number_text:
            matched += 1
        elif first_miss is None:
            first_miss = {
                "normalized": round(position, 9),
                "printed": number_text,
                "predicted": predicted,
            }
    block: dict[str, Any] = {
        "states_compared": compared,
        "states_reproduced": matched,
        "format": "%.6g of the float32 law value",
        # a fit that is *within* the display's budget (see `fit_residual_ratio`) but
        # not exact to the last digit misses a count here without being wrong: the
        # acceptance test is the residual, this is the stronger statement when it holds
        "note": (
            "the same digits come out; a fit within the display's own budget but not "
            "exact to its last digit finishes below `states_compared` without being wrong"
        ),
    }
    if first_miss is not None:
        block["first_miss"] = first_miss
    return block


def _print_model_params(
    predict_spine: Any,
    descending: bool,
    states: list[tuple[float, str]],
    positions: dict[float, float] | None,
) -> dict[str, Any]:
    """The digit-for-digit print check, as a law parameter (see `_printed_text_reproduced`)."""
    predict = (
        (lambda value: predict_spine(1.0 - float(value))) if descending else predict_spine
    )
    return {"print_model": _printed_text_reproduced(predict, states, positions)}


#: A display whose printed number carries no fraction: the *display* steps by one
#: of its own units (``Oscillator 1 Wave Frame`` steps by 1 frame, ``Sample
#: Transpose Quantize`` by 1 of 8192). The sweep's own sampling step is a different
#: quantity and is not a display resolution.
_INTEGER_TEXT_RE = re.compile(r"^[+-]?\d+$")


def _printed_values_are_integers(states: list[tuple[float, str]]) -> bool:
    """Every state prints an integer (the display quantises to 1)."""
    if not states:
        return False
    for _norm, display in states:
        number = depth_module.display_number(display)
        if number is None or float(number) != float(int(number)):
            return False
        remainder = depth_module.display_unit(display)
        text = str(display).strip()
        number_text = text[: len(text) - len(remainder)].strip() if remainder else text
        if not _INTEGER_TEXT_RE.match(number_text):
            return False
    return True


def _display_step_params(states: list[tuple[float, str]]) -> dict[str, Any]:
    """What the sweep *sampled*: integers a whole unit apart - not a display quantum.

    A first version of this recorded ``display_step: 1.0`` ("the display steps by
    1"), which the live read-agreement probe of 2026-10-12 falsified: measured on
    ``Oscillator 1 Wave Frame``, whose 257 sampled states are the integers 0..256,
    the host prints **12.1905** at normalized 0.0476 - the display is continuous and
    the sweep's own step was exactly one unit, so the sweep cannot see below it. A
    sweep cannot establish a display's resolution; only the host formatter, asked
    between the sampled points, can.
    """
    if not _printed_values_are_integers(states):
        return {}
    values = sorted({float(depth_module.display_number(display)) for _norm, display in states})
    smallest = min((b - a for a, b in zip(values, values[1:]) if b > a), default=None)
    return {
        "sampled_display": {
            "printed_values_are_integers": True,
            "smallest_sampled_step": smallest,
            "note": (
                "the sweep's own samples are integers and the smallest step between them "
                "is this; a sweep cannot see below its own step, so this is not the "
                "display's resolution - the host formatter, asked between the sampled "
                "points, is the channel that measures it (see the P24 probe)"
            ),
        }
    }


def _display_step_accuracy(
    residual: float, states: list[tuple[float, str]]
) -> dict[str, Any]:
    """The residual measured in the sweep's **own** sampled step.

    On a display whose sampled values are integers a whole unit apart, the absolute
    residual is not the whole story: a staircase's printed value is a rounding of
    the parameter, so a law that sits half a *sampled* step away is as accurate as
    the sampling can show, while the budget it was judged against can be far larger.
    Measured 2026-10-12: ``sample_transpose_quantize`` fits to 0.498 sampled steps
    against a budget of 15.5 (its sweep sampled every 32nd integer), and
    ``effect_chain_order`` to 0.50004 against a budget of 708.
    """
    if not _printed_values_are_integers(states):
        return {}
    values = sorted({float(depth_module.display_number(display)) for _norm, display in states})
    smallest = min((b - a for a, b in zip(values, values[1:]) if b > a), default=None)
    if not smallest:
        return {}
    return {
        "residual_in_sampled_steps": round(float(residual) / smallest, 9),
        "residual_in_sampled_steps_note": (
            "measured in the sweep's smallest sampled step, not in a display quantum "
            "(see `sampled_display`); the budget the fit was judged against is half "
            "that step, floored by each state's printed precision"
        ),
    }


def _measured_pairs(states: list[tuple[float, str]]) -> list[tuple[float, float]] | None:
    """Every measured state as ``(normalized, numeric display)`` - or ``None``.

    ``None`` means **at least one display is not a faithful number**, which is the
    signal that decides between the enum and the numeric path. It is deliberately
    all-or-nothing: the POC's leading-number parser reads ``"2x Octave"`` as
    ``2.0``, so a per-state filter would let a labelled control such as
    `Oscillator 1 Stack Style` (13 labels, 2 of which start with a digit) into the
    numeric path, where a free exponent fit would then be meaningless (the
    independent audit of 2026-10-11 measured exactly that leak).

    A number is *faithful* only when what follows it is empty or a single
    unit-like token. Measured 2026-10-11: the tempo-division labels ``"4/1",
    "1/2", "1/64"`` parse to ``4.0, 1.0, 1.0`` through the leading-number rule -
    three distinct values for nine states, each label *claiming* to be the number
    it starts with. That is not a numeric scale, so the row takes the label
    (enum) path instead of a meaningless fit; a space-separated suffix such as
    ``"2x Octave"`` is refused for the same reason.
    """
    entries = _measurement_entries(states)
    if entries is None:
        return None
    return [(norm, value) for norm, value, _precision in entries]


#: The law kinds a *measured* derivation may install. `enum_index` is not one of
#: them: a numeric derivation that produced a member list would be a different
#: decision, and the ramp override below is exactly the decision not to take it.
_NUMERIC_LAW_KINDS = ("affine", "power", "exponential")


def _sampled_ramp(states: list[tuple[float, str]], steps: int | None) -> dict[str, Any] | None:
    """Does this sweep prove a declared member list is a **sampling grid**?

    The registry declares 294 rows as ``enum_index``, and for most of them the
    declaration is a measurement: the sweep shows each label repeating across its
    band (``filter_1_style``: six labels over 257 samples; ``chorus_tempo``: eleven
    over 257). For twenty the sweep shows something a member list cannot produce -
    **a faithful number at every sampled position, strictly monotone, all values
    distinct, with a change at every step the sweep walked** (measured 2026-10-12).
    A member set repeats its label across the band it owns; a display that changes
    at *every* one of the sweep's 256 steps and follows one law (see
    `derive_measured_law`) is a numeric ramp sampled on the sweep's grid, and the
    member list built from it is a grid artefact - the AI had to name one of 257
    strings to set a continuous level.

    The *step* requirement is what separates a ramp from a **quantised ladder**:
    ``pitch_bend_range`` shows 49 distinct integers over 49 stored positions, but
    only at 49 of the sweep's 257 positions did anything change - the display is
    constant in between, which is a banded member set (its bands were measured
    exactly, by the artifact) and not a ramp. ``steps`` comes from the registry's
    own record of the sweep; when it is absent the predicate declines.

    Returns the evidence block, or ``None`` when the declaration stands. The caller
    adds the second half of the guard: a *numeric law* must be accepted for the
    sweep, or nothing is overridden - so ``oversampling`` ("1x/2x/4x/8x" on
    non-uniform bands, 4 states, no acceptable fit) keeps its four members. What it
    does **not** decide is whether the control is continuous between the sampled
    positions; the host's own formatter, measured on arbitrary values, is the
    channel that can (P23's artifact, when present, and the P24 read agreement
    probe).
    """
    if len(states) < _MIN_DERIVATION_STATES:
        return None
    #: the sweep records *change points*: one per step is what a ramp produces, and
    #: fewer than the steps it walked means the display held still in between
    if steps is None or len(states) != steps + 1:
        return None
    entries = _measurement_entries(states)
    if entries is None:  # a labelled sweep: the member list is a member list
        return None
    ordered = sorted(entries, key=lambda entry: entry[0])
    strictly_increasing = all(b[1] > a[1] for a, b in zip(ordered, ordered[1:]))
    strictly_decreasing = all(b[1] < a[1] for a, b in zip(ordered, ordered[1:]))
    if not (strictly_increasing or strictly_decreasing):
        # a repeated value between two samples is a band; two labels wearing numbers
        # that swap places are not a scale
        return None
    values = [value for _norm, value, _precision in entries]
    display_params = _display_step_params(states)
    return {
        "sampled_positions": len(entries),
        "sweep_steps": int(steps),
        "direction": "increasing" if strictly_increasing else "decreasing",
        "distinct_values": len({round(value, 12) for value in values}),
        "smallest_measured_step": min(
            b - a for a, b in zip(sorted(values), sorted(values)[1:]) if b > a
        ),
        **display_params,
    }


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


def _band_geometry(
    states: list[tuple[float, str]], members: list[str]
) -> dict[str, list[float]] | None:
    """One measured band per member: its interior midpoint **and** its start.

    The enum conversion in Core assumes uniformly spaced members. For
    ``Oscillator N Distortion Type`` that assumption is false and measurably so:
    the sweep shows 11 bands whose switch points run 0, 11/256, 32/256, 54/256,
    76/256, 98/256 ... with double-width gaps, and the uniform mapping
    (``index / (n - 1)``) lands 5 of the 11 members in the *neighbouring* band
    (audit of 2026-10-11, measured).

    Two numbers per member, because the read path and the write path need
    different ones: the **midpoint** is interior to the band, so a write lands
    inside it; the **start** is the switch point, where the host labels a value
    exactly on a boundary as the member that *begins* there (P19, measured), so
    the read inversion must use it.
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
    return {
        "positions": [positions[member] for member in members],
        "switch_points": [round(start, 9) for _label, start, _end in spans],
    }


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
        geometry = _band_geometry(states, members)
        if geometry is None:
            return None
        positions = geometry["positions"]
        switch_points = geometry["switch_points"]
        return {
            "kind": "enum_index",
            "type": "enum",
            "discrete": True,
            "enum_values": [{"id": member, "label": member} for member in members],
            "range": None,
            "params": {
                "positions": positions,
                # The members' own band *starts*, for the read inversion: the host
                # labels a value exactly at a switch point as the member that
                # begins there (P19, measured), and a derived enum previously
                # carried midpoints only - so a value sitting on a boundary read as
                # the member below it. Measured 2026-10-11 through the host-display
                # channel: `chorus_tempo` at normalized 0.25 formats as "8/1" while
                # the nearest-midpoint inversion read "16/1".
                "switch_points": switch_points,
                "positions_source": "measured band midpoints of vital_scales_full.json",
                "n_states": len(states),
            },
        }

    # a *numeric* law is a curve, and two points always fit one: three states is
    # the floor `depth.py::depth_fit` already uses for the same reason
    if len(states) < _MIN_DERIVATION_STATES:
        return None

    entries = _measurement_entries(states)
    if entries is None:  # pragma: no cover - `pairs` was not None, so this holds
        return None
    #: the positions the displays were really produced at, when the sweep is a
    #: recognised uniform grid (the file stores them to six decimals; see
    #: `_exact_sampled_positions`). Without this a steep law is judged against the
    #: wrong x - measured on `random_N_frequency`: ratio 11.2 refused, 0.95 accepted.
    exact = _exact_sampled_positions(entries)
    position_model = (
        "the sweep's own uniform grid i/(n-1), which every stored position matches "
        "to within the file's six decimals"
        if exact is not None
        else "the stored positions as recorded"
    )
    if exact is not None:
        entries = exact
    #: stored position -> the position the display was produced at, for the
    #: digit-for-digit print check (`_print_model_params`)
    position_map = (
        {
            round(float(states[index][0]), 12): float(exact[index][0])
            for index in range(len(states))
        }
        if exact is not None
        else None
    )
    values = [value for _norm, value, _precision in entries]
    quantum = _measurement_quantum(values)
    floor = 0.5 * quantum + 1e-9
    #: half a float32 ulp at the largest printed magnitude: the plugin prints a
    #: 32-bit float, and a display at magnitude M can miss the *real* value by that
    #: much on top of its own print rounding
    storage_term = _float32_storage_term(values)
    ordered = sorted(entries, key=lambda entry: entry[0])
    non_decreasing = all(b[1] >= a[1] for a, b in zip(ordered, ordered[1:]))
    non_increasing = all(b[1] <= a[1] for a, b in zip(ordered, ordered[1:]))
    if non_decreasing:
        direction = "ascending"
        oriented = ordered
    elif non_increasing:
        # A measured **descending** display (the seven frequency controls: 128 s
        # at normalized 0 down to 0.0019 s at 1). The spine model in `values.py`
        # states the direction explicitly (`law.params.direction`), so the fit
        # runs in spine space (`1 - normalized`) and the returned range keeps
        # `min <= max` as numeric extremes - never an inverted formula that a
        # round-trip would not reveal.
        direction = "descending"
        oriented = [(1.0 - norm, value, precision) for norm, value, precision in ordered]
    else:
        return None  # non-monotone: no single-valued law describes the sweep
    endpoints = _norm_ordered_endpoints([(norm, value) for norm, value, _p in oriented])
    if endpoints is None:
        return None
    low, high = endpoints
    # carrying the direction only when it is not the default keeps every
    # ascending row's profile byte-identical to what it was before
    spine_params = {"direction": direction} if direction == "descending" else {}
    bound_model = (
        "max(printed precision of each state, half the measured display step) + half a float32 "
        "ulp at the largest printed magnitude (the display is a print of a 32-bit float)"
    )
    display_params = _display_step_params(states)
    #: the sampled position is a *measurement*, not an allowance: both the fit and
    #: the residual use it, and the model is recorded with the law
    position_params = {"position_model": position_model}

    def _bound_params(worst_bound: float) -> dict[str, Any]:
        """The budget the residual was judged against, and the terms it sums."""
        return {
            "fit_bound": round(float(worst_bound), 12),
            "fit_bound_terms": {
                "from_the_display": round(float(floor), 12),
                "from_the_float32_storage": round(float(storage_term), 12),
            },
            "fit_bound_model": bound_model,
        }

    def _accepted(predict: Any) -> tuple[bool, float, float, float]:
        worst_abs, worst_ratio, worst_bound = _worst_fit_ratio(
            predict, oriented, floor, storage_term
        )
        return worst_ratio <= 1.0 + 1e-9, worst_abs, worst_ratio, worst_bound

    fit = depth_module.linear_fit([(norm, value) for norm, value, _p in oriented])
    if fit is not None:
        slope, intercept, _residual = fit
        ok, worst_abs, worst_ratio, worst_bound = _accepted(
            lambda t: slope * t + intercept
        )
        if ok:
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
                    "fit_residual": round(float(worst_abs), 12),
                    "fit_residual_ratio": round(float(worst_ratio), 9),
                    **_bound_params(worst_bound),
                    "source": "vital_scales_full.json (measured sweep fit)",
                    "n_states": len(states),
                    **position_params,
                    **_print_model_params(
                        lambda t: slope * t + intercept, direction == "descending", states, position_map
                    ),
                    **display_params,
                    **_display_step_accuracy(worst_abs, states),
                    **spine_params,
                },
            }

    if low > 0.0 and high > 0.0:
        ok, worst_abs, worst_ratio, worst_bound = _accepted(
            lambda t: low * (high / low) ** t
        )
        if ok:
            return {
                "kind": "exponential",
                "type": "number",
                "enum_values": None,
                "range": {"min": low, "max": high},
                "params": {
                    "scale": 1.0,
                    "offset": 0.0,
                    "fit_residual": round(float(worst_abs), 12),
                    "fit_residual_ratio": round(float(worst_ratio), 9),
                    **_bound_params(worst_bound),
                    "source": "vital_scales_full.json (measured sweep fit)",
                    "n_states": len(states),
                    **position_params,
                    **_print_model_params(
                        lambda t: low * (high / low) ** t, direction == "descending", states, position_map
                    ),
                    **display_params,
                    **_display_step_accuracy(worst_abs, states),
                    **spine_params,
                },
            }

    base = max(values)
    # the exponent is fitted on the strictly-interior points: norm 0 has no
    # logarithm and norm 1 pins the base by construction (`math.log(1.0) == 0.0`
    # is a ZeroDivisionError, measured 2026-10-11 while building this fit)
    exponent_points = [
        (norm, value) for norm, value, _p in oriented if 0.0 < norm < 1.0 and value > 0.0
    ]
    distinct_values = sorted({value for _norm, value in exponent_points})
    if base > 0.0 and len(exponent_points) >= 2 and len(distinct_values) >= 2:
        exponent = sum(
            math.log(value / base) / math.log(norm) for norm, value in exponent_points
        ) / len(exponent_points)
        if exponent > 0.0:
            ok, worst_abs, worst_ratio, worst_bound = _accepted(
                lambda t: base * t**exponent
            )
            if ok:
                return {
                    "kind": "power",
                    "type": "number",
                    "enum_values": None,
                    "range": {"min": low, "max": high},
                    "params": {
                        "base": float(base),
                        "exponent": round(float(exponent), 9),
                        "fit_residual": round(float(worst_abs), 12),
                        "fit_residual_ratio": round(float(worst_ratio), 9),
                        **_bound_params(worst_bound),
                        "source": "vital_scales_full.json (measured sweep fit)",
                        "n_states": len(states),
                        **position_params,
                        **_print_model_params(
                            lambda t: base * t**exponent, direction == "descending", states, position_map
                        ),
                        **display_params,
                        **_display_step_accuracy(worst_abs, states),
                        **spine_params,
                    },
                }
    return None


#: The candidate kinds, richest-first: an exponential explains a wide dynamic
#: range, a power a taper from a zero base, an affine a constant slope.
_CANDIDATE_ORDER = ("exponential", "power", "affine")


def derive_law_candidate(states: list[tuple[float, str]]) -> dict[str, Any] | None:
    """The best-fitting law this sweep *nearly* satisfies - recorded, never applied.

    Written for the rows `derive_measured_law` refuses: the fit exists and its
    shape is visible in the sweep, but at least one state misses its own printed
    precision, so the law is **not** installed (``values.to_native`` keeps
    refusing) and the control keeps its ``none``/``unmeasured`` law. What the
    profile gains is the diagnosis: which law was tried, how far outside the
    instrument budget it landed, and at which state - so "no law" becomes "a
    named candidate with a measured miss", and a future provider-calibration step
    knows exactly what to verify.

    Measured 2026-10-11 (``random_1..4_frequency``): the endpoint-anchored
    exponential lands within 5.7e-6 *relative* of every printed state, and every
    state obeys the law within the instrument's own budget once the *stored
    position's* rounding is accounted for (2026-10-12: it is, so the four rows
    gained the law and no longer carry a candidate). The diagnosis stays for the
    rows that are still refused for a real reason - e.g. ``oversampling`` (four
    states ``1x/2x/4x/8x`` on non-uniform bands), where the best family still
    misses a state's printed precision and so the member list is kept.
    """
    entries = _measurement_entries(states)
    if entries is None or len(entries) < _MIN_DERIVATION_STATES:
        return None
    exact = _exact_sampled_positions(entries)
    if exact is not None:
        entries = exact
    values = [value for _norm, value, _precision in entries]
    quantum = _measurement_quantum(values)
    floor = 0.5 * quantum + 1e-9
    storage_term = _float32_storage_term(values)
    ordered = sorted(entries, key=lambda entry: entry[0])
    non_decreasing = all(b[1] >= a[1] for a, b in zip(ordered, ordered[1:]))
    non_increasing = all(b[1] <= a[1] for a, b in zip(ordered, ordered[1:]))
    if non_decreasing:
        direction = "ascending"
        oriented = ordered
    elif non_increasing:
        direction = "descending"
        oriented = [(1.0 - norm, value, precision) for norm, value, precision in ordered]
    else:
        return None
    endpoints = _norm_ordered_endpoints([(norm, value) for norm, value, _p in oriented])
    if endpoints is None:
        return None
    low, high = endpoints

    candidates: dict[str, Any] = {}
    fit = depth_module.linear_fit([(norm, value) for norm, value, _p in oriented])
    if fit is not None:
        slope, intercept, _residual = fit
        candidates["affine"] = (lambda t, m=slope, b=intercept: m * t + b, {})
    if low > 0.0 and high > 0.0:
        candidates["exponential"] = (
            lambda t: low * (high / low) ** t,
            {"scale": 1.0, "offset": 0.0},
        )
    base = max(values)
    if base > 0.0:
        exponent_points = [
            (norm, value) for norm, value, _p in oriented if 0.0 < norm < 1.0 and value > 0.0
        ]
        if len(exponent_points) >= 2 and len({value for _n, value in exponent_points}) >= 2:
            exponent = sum(
                math.log(value / base) / math.log(norm) for norm, value in exponent_points
            ) / len(exponent_points)
            if exponent > 0.0:
                candidates["power"] = (lambda t, e=exponent: base * t**e, {"base": base, "exponent": exponent})
    if not candidates:
        return None

    #: the same two instrument terms the fit test uses, or a refused law and its
    #: recorded diagnosis would disagree about the budget (see `_worst_fit_ratio`
    #: and `_exact_sampled_positions`)
    displays = {round(norm, 12): display for norm, display in states}
    best: dict[str, Any] | None = None
    for kind in _CANDIDATE_ORDER:
        if kind not in candidates:
            continue
        predict, params = candidates[kind]
        worst: dict[str, Any] | None = None
        for norm, value, precision in oriented:
            bound = max(precision, floor, 1e-15) + storage_term
            predicted = predict(norm)
            ratio = abs(value - predicted) / bound
            if worst is None or ratio > worst["ratio"]:
                original_norm = norm if direction == "ascending" else 1.0 - norm
                worst = {
                    "normalized": round(original_norm, 9),
                    "display": displays.get(round(original_norm, 12)),
                    "predicted": round(float(predicted), 9),
                    "precision": precision,
                    "bound": round(float(bound), 12),
                    "ratio": round(float(ratio), 6),
                }
        record = {
            "kind": kind,
            "direction": direction,
            "range": {"min": low, "max": high},
            "params": {**params, "n_states": len(states)},
            "worst_state": worst,
            "refused_by": "the display-precision budget: at least one state misses its own printed precision",
            "source": "vital_scales_full.json (best-effort fit, not installed)",
        }
        if kind == "affine" and fit is not None:
            record["params"].update(
                {
                    "measured_slope": round(float(fit[0]), 9),
                    "measured_intercept": round(float(fit[1]), 9),
                }
            )
        if best is None or (worst and worst["ratio"] < best["worst_state"]["ratio"]):
            best = record
    return best


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


def build_profile(
    registry: VitalRegistry,
    identity: Mapping[str, Any],
    *,
    calibration: Any = None,
    calibration_row: dict[str, Any] | None = None,
    calibration_note: str | None = None,
    enum_boundaries: Any = None,
    enum_boundaries_row: dict[str, Any] | None = None,
    enum_boundaries_note: str | None = None,
) -> dict[str, Any]:
    """Build the [07 §1] profile from the loaded registry and a live identity.

    ``calibration`` is a loaded host-calibration artifact (`calibration.py`); when
    present its bytes are a declared **source** (so the cache key covers it) and
    the merges it justifies are applied with per-field provenance. When a
    calibration file exists but could not be used, ``calibration_row`` still
    carries its hash into the sources and ``calibration_note`` says why nothing
    was applied - a half-applied artifact is never a silent state.
    """
    from . import calibration as calibration_module  # noqa: PLC0415 - one-way, lazy
    from . import enum_boundaries as enum_boundaries_module  # noqa: PLC0415 - one-way, lazy
    from . import source_facts as source_facts_module  # noqa: PLC0415 - one-way, lazy
    from ... import values as values_module  # noqa: PLC0415

    rows = registry.rows()
    scales = registry.scales()
    vocabulary = registry.vocabulary()
    modules = registry.modules()

    module_ids = [str(name) for name in modules]
    module_descriptors: list[dict[str, Any]] = []
    controls: list[dict[str, Any]] = []
    #: every range widened to the plugin's own parameter bounds (see
    #: `source_facts.widen_to_source_bounds`), for the profile's `value_bounds` block
    value_bounds_records: list[dict[str, Any]] = []
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
        #
        # A declared ``enum_index`` is a third case, added 2026-10-12: the
        # declaration was built from "distinct display strings at 257 sample
        # points", which a *ramp* also produces. Where the sweep shows a strictly
        # monotone faithful number at every sampled position, the member list is
        # that grid's artefact and the row is answered from the sweep too - but
        # only if a numeric law is then accepted (see `_sampled_ramp`).
        sampled_ramp = (
            _sampled_ramp(states, registry.sweep_steps())
            if declared_law["kind"] == "enum_index" and states
            else None
        )
        needs_measurement = (
            declared_law["kind"] in ("none", "unmeasured")
            or (declared_law["kind"] == "affine" and declared_range is None)
            or sampled_ramp is not None
        )
        measured = derive_measured_law(row, states) if needs_measurement else None
        if measured is not None and sampled_ramp is not None \
                and str(measured.get("kind")) not in _NUMERIC_LAW_KINDS:
            # fail-closed: contradicting a declaration requires a *numeric* law.
            # `_sampled_ramp` already excludes the labelled branch, so this guards
            # a future derivation change rather than today's data.
            measured = None
        #: set for a row whose sweep nearly fits a law the display-precision
        #: budget refused (see `derive_law_candidate`): recorded as a named
        #: diagnosis, never installed - `values.to_native` keeps refusing
        law_candidate: dict[str, Any] | None = (
            derive_law_candidate(states) if needs_measurement and measured is None else None
        )
        #: set when a *declared enum* survived a numeric derivation attempt, i.e. the
        #: sweep showed a monotone number at every sampled position but no law of the
        #: vocabulary fitted it inside the instrument's budget. The member list is
        #: then the right model and the reason is carried in the evidence - `law_candidate`
        #: may not say it, because the control *does* convert (as an enum): measured on
        #: `oversampling` ("1x/2x/4x/8x", four states on non-uniform bands, best family
        #: 1.54x the budget), where a candidate on a converting enum is a validator
        #: contradiction.
        numeric_model_declined: dict[str, Any] | None = (
            {
                "why": (
                    "the sweep shows a faithful number at every sampled position with no "
                    "repeat, but no law of the vocabulary fits it within the instrument's "
                    "own budget: the declared member list is kept"
                ),
                "measured": dict(sampled_ramp),
                "best_candidate": law_candidate,
            }
            if sampled_ramp is not None and measured is None
            else None
        )
        if numeric_model_declined is not None:
            # the control keeps converting as an enum, so it is not a `law_candidate`
            law_candidate = None
        #: set only for a declared `enum_index` row whose *measured* bands are not
        #: uniform (see `_declared_enum_positions`); carried into the evidence
        positions_correction: dict[str, Any] | None = None
        #: set when the row's declared member list was replaced by a measured
        #: numeric law (`_sampled_ramp`); carried into the evidence
        enum_override: dict[str, Any] | None = None
        if measured is not None:
            law = {"kind": str(measured["kind"]), "params": dict(measured.get("params") or {})}
            control_type = str(measured.get("type") or _control_type(row))
            range_spec = measured.get("range") if measured.get("range") is not None else _range_from_row(row)
            if sampled_ramp is not None:
                # the *measurement* decided the shape, so the declaration's own
                # `discrete` flag is not evidence about it
                discrete = bool(measured.get("discrete"))
                enum_override = {
                    "declared_kind": "enum_index",
                    "declared_members": int(row.get("n_states") or len(states)),
                    "measured_states": int(sampled_ramp["sampled_positions"]),
                    "measured_display": (
                        "a faithful number at every sampled position, strictly "
                        f"{sampled_ramp['direction']} "
                        f"({sampled_ramp['distinct_values']} distinct values)"
                    ),
                    "law": str(measured["kind"]),
                    "range_from_the_sweep": measured.get("range"),
                    "range_from_the_declaration": dict(declared_range) if declared_range else None,
                    "range_disagrees_with_the_declaration": bool(
                        declared_range is None
                        or measured.get("range") is None
                        or float(declared_range.get("min", 0.0))
                        != float(measured["range"].get("min", 0.0))
                        or float(declared_range.get("max", 0.0))
                        != float(measured["range"].get("max", 0.0))
                    ),
                    "why": (
                        "a labelled member set repeats each label across the band it owns; a "
                        "display that changes at every sampled position and follows one law "
                        "within the display's own printing precision is a ramp sampled on the "
                        "sweep's grid - naming one of its members would mean naming a sample"
                    ),
                    "what_this_does_not_decide": (
                        "whether the control is continuous between the sampled positions; the "
                        "host's own formatter, read at arbitrary values, is the channel that "
                        "can decide it (the P23 artifact when present, the P24 probe live)"
                    ),
                }
                #: the two controls the plugin's own table declares as an integer ladder
                #: (`sample_transpose_quantize`, `effect_chain_order`): 257 sampled
                #: positions cannot see a step, but the *source* can - `ValueDetails::kIndexed`
                #: with a span of 10,000s, whose `convertToEngineValue` rounds every
                #: value the host write channel stores. The law stays the fitted affine
                #: one; the step says where its values live.
                ladder = source_facts_module.INTEGER_LADDERS.get(str(state_key))
                if ladder is not None:
                    discrete = True
                    law["params"] = {
                        **dict(law.get("params") or {}),
                        "quantization": {
                            "step": float(ladder["step"]),
                            "origin": float(ladder["min"]),
                            "rounding": "half_away_from_zero",
                            "source": source_facts_module.CORRESPONDENCE["source"],
                        },
                    }
                    enum_override["integer_ladder"] = {
                        "state_bounds": {"min": ladder["min"], "max": ladder["max"]},
                        "step": ladder["step"],
                        "span_explanation": ladder["span_explanation"],
                        "vst3_continuity": ladder["vst3_continuity"],
                        "plugin_rounding": ladder["plugin_rounding"],
                        "consequence": (
                            "a fractional request is stored rounded, and the display - which "
                            "rounds before printing - is an integer: the step is in the engine, "
                            "not in the formatter"
                        ),
                    }
            else:
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
        #: set when the plugin's own parameter table holds *wider* bounds than the
        #: declared range (see `source_facts.widen_to_source_bounds`): a correction of
        #: the same class as `LIVE_MEASURED_CORRECTIONS`, but for the 70 rows where a
        #: 900-preset corpus range truncated the parameter. Only ever widens.
        bounds_record: dict[str, Any] | None = None
        if correction is None:
            if measured is None and control_type == "number" and range_spec is not None:
                bounds_record = source_facts_module.widen_to_source_bounds(
                    str(state_key), range_spec
                )
                if bounds_record is not None:
                    range_spec = {"min": bounds_record["min"], "max": bounds_record["max"]}
                    value_bounds_records.append({"control": str(state_key), **bounds_record})
            evidence = _evidence(row, bool(states), measured=measured)
            if positions_correction is not None:
                evidence = {**evidence, "positions_correction": dict(positions_correction)}
            if enum_override is not None:
                evidence = {**evidence, "enum_declaration_overridden": dict(enum_override)}
            if numeric_model_declined is not None:
                evidence = {
                    **evidence,
                    "numeric_model_declined": dict(numeric_model_declined),
                }
            if bounds_record is not None:
                fit = row.get("fit") or {}
                fit_bounds = {
                    "min": fit.get("nmin"),
                    "max": fit.get("nmax"),
                    "source": fit.get("source"),
                }
                agrees = bool(
                    isinstance(fit_bounds["min"], (int, float))
                    and isinstance(fit_bounds["max"], (int, float))
                    and abs(float(fit_bounds["min"]) - bounds_record["min"]) <= 1e-6
                    and abs(float(fit_bounds["max"]) - bounds_record["max"]) <= 1e-6
                )
                display = source_facts_module.DISPLAY_FACTS.get(str(state_key)) or {}
                # The registry's own fit is the second authority, and where it does not
                # agree numerically the reason is a *space*: the fit was taken in the
                # display's units (a `*_pan` fit of -100..100 against a state range of
                # -1..1 with `display_multiply: 100`). Name which of the two it is
                # instead of reporting a bare disagreement.
                transform = float(display.get("multiply") or 1.0)
                fit_in_display_space = bool(
                    not agrees
                    and transform != 1.0
                    and isinstance(fit_bounds["min"], (int, float))
                    and isinstance(fit_bounds["max"], (int, float))
                    and abs(float(fit_bounds["min"]) - bounds_record["min"] * transform) <= 1e-6 * transform
                    and abs(float(fit_bounds["max"]) - bounds_record["max"] * transform) <= 1e-6 * transform
                )
                evidence = {
                    **evidence,
                    "value_bounds": {
                        "declared_before": dict(bounds_record["declared_before"]),
                        "declared_after": {"min": bounds_record["min"], "max": bounds_record["max"]},
                        "widened": dict(bounds_record["widened"]),
                        "why": (
                            "the declared range came from a 900-preset corpus (native_min/max); "
                            "the plugin's own parameter table declares wider bounds, so the "
                            "corpus range truncated the parameter"
                        ),
                        "source": source_facts_module.CORRESPONDENCE["source"],
                        "correspondence": (
                            "the source's display model reproduces the live host formatter at "
                            "both ends (and at all sampled points) - see source_facts.CORRESPONDENCE"
                        ),
                        "registry_fit": {
                            **fit_bounds,
                            "agrees_with_the_source": agrees,
                            **(
                                {
                                    "fit_units": "display space",
                                    "agrees_after_the_display_transform": True,
                                }
                                if fit_in_display_space
                                else {}
                            ),
                        },
                        "evidence_level": (
                            "state-space bounds: the plugin's own declared parameter bounds, "
                            "with the display text at both ends corroborated by the live "
                            "formatter. Not a claim that writes across the range were "
                            "exercised"
                        ),
                        "value_space": {
                            "units": unit,
                            "display": {
                                "multiply": display.get("multiply"),
                                "scale": display.get("scale"),
                                "invert": display.get("invert"),
                                "post_offset": display.get("post_offset"),
                                "text_at_0": display.get("text_at_0"),
                                "text_at_1": display.get("text_at_1"),
                                "live_text_at_0": display.get("live_text_at_0"),
                                "live_text_at_1": display.get("live_text_at_1"),
                            },
                            "note": (
                                "the range is in the plugin's state space; the display text is "
                                "that transform's rendering of it, which is why the raw numbers "
                                "differ (the P24 range census compared the two spaces)"
                            ),
                        },
                    },
                }
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
                **({"law_candidate": law_candidate} if law_candidate else {}),
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

    source_rows = [{"file": row["file"], "sha256": row["sha256"]} for row in registry.sources]
    if calibration_row is not None:
        source_rows.append(dict(calibration_row))
    if enum_boundaries_row is not None:
        source_rows.append(dict(enum_boundaries_row))
    calibration_block: dict[str, Any] = {
        "state": (
            "applied"
            if calibration is not None
            else ("refused" if calibration_note else "absent")
        ),
        "applied": calibration is not None,
        "artifact": dict(calibration_row) if calibration_row is not None else None,
        "method_version": int(calibration.method_version) if calibration is not None else None,
        "grid_points": len(calibration.grid) if calibration is not None else None,
        # only *known scalar* fields travel into the profile: the artifact is data,
        # but a wholesale copy of its blocks would let an oversized blob become
        # unbounded profile content that still validates
        "proof": _scalar_subset(
            calibration.proof if calibration is not None else {},
            ("values_unchanged", "chunk_sha256_unchanged", "watched_points", "statement"),
        ),
        "host_param_space": _scalar_subset(
            calibration.host_param_space if calibration is not None else {},
            ("call", "min", "max", "mid", "uniform_0_1", "probed", "note"),
        ),
        "host_default_value": _scalar_subset(
            calibration.host_default_value if calibration is not None else {},
            ("call", "answered", "probed", "available", "evidence"),
        ),
        "refusals": [calibration_note] if calibration_note else [],
        "counts": {},
    }
    if calibration is not None:
        report = calibration_module.apply_calibration(
            controls, calibration, values_module=values_module
        )
        calibration_block["counts"] = dict(report.get("counts") or {})
        calibration_block["units_filled"] = list(report.get("units_filled") or [])
        # the *refusals* travel too: a reader must be able to see why a unit was
        # not filled, not only that the profile has none
        calibration_block["units_declined"] = list(report.get("units_declined") or [])
        calibration_block["confirmations_declined"] = list(
            report.get("confirmations_declined") or []
        )
        calibration_block["display_offset_candidates"] = list(
            report.get("display_offset_candidates") or []
        )

    # The measured enum band boundaries (P23). Applied *after* the calibration
    # merge, because that merge fills units/metadata while this one rewrites the
    # numbers the two conversion directions use - and it never touches a law kind
    # or a member list, only the band geometry of an `enum_index` control.
    enum_block: dict[str, Any] = {
        "state": (
            "applied"
            if enum_boundaries is not None
            else ("refused" if enum_boundaries_note else "absent")
        ),
        "applied": enum_boundaries is not None,
        "artifact": dict(enum_boundaries_row) if enum_boundaries_row is not None else None,
        "method_version": (
            int(enum_boundaries.method_version) if enum_boundaries is not None else None
        ),
        "tolerance": enum_boundaries.tolerance if enum_boundaries is not None else None,
        "call": "TrackFX_FormatParamValueNormalized (read-only: formats a value the caller passes)",
        "evidence_levels": {
            "exact": (
                "the transition lies between two adjacent IEEE doubles: `hi` is the smallest "
                "double the host formats as the member that begins there. The claim is about "
                "the bracket's *ends* - what the host prints exactly at and just below the "
                "boundary - and each band's interior is verified separately "
                "(`band_interiors_verified`)"
            ),
            "exact_at": (
                "a round candidate value was proved exact in two calls: the candidate itself "
                "formats as the upper member and the double below it as the lower one"
            ),
            "interval": "the transition is bracketed to the method's tolerance",
            "coarse_interval": (
                "bracketed wider than the tolerance because the call budget ran out: the "
                "residual error is the bracket's full width"
            ),
            "confirmed_within": (
                "the profile's own boundary reproduced both sides of the display within "
                "epsilon; the merge only accepts it when the bracket contains that boundary"
            ),
            "continuous_display": (
                "the host prints a value between adjacent members: the control has no band "
                "structure (its member list came from a swept sampling grid)"
            ),
            "undetermined": "no measurement of this control's bands exists",
        },
        "counts_note": {
            "controls_without_band_structure": (
                "artifact-derived: how many artifact rows show a display between members, "
                "whether or not the profile still models the control as an enum"
            ),
            "controls_not_enums_any_more": (
                "profile-derived: artifact rows whose control a measured numeric law now "
                "models (`not_enums_any_more`), so there are no members to place bands on"
            ),
            "confirmations_withheld": (
                "profile-derived: converted controls the artifact could not confirm because "
                "it did not measure every transition as a display between members; the "
                "numeric law still comes from the sweep, so the model stands"
            ),
        },
        "refusals": [enum_boundaries_note] if enum_boundaries_note else [],
        "counts": {},
    }
    if enum_boundaries is not None:
        enum_report = enum_boundaries_module.apply_enum_boundaries(controls, enum_boundaries)
        control_by_id = {str(control.get("id")): control for control in controls}
        enum_block["counts"] = dict(enum_report.get("counts") or {})
        enum_block["controls_refined"] = [
            row["control"] for row in enum_report.get("attached") or [] if row.get("outcome") == "refined"
        ]
        enum_block["refused_controls"] = list(enum_report.get("refusals") or [])
        enum_block["without_band_structure"] = [
            {
                **row,
                "profile_model": {
                    "type": str(
                        (control_by_id.get(str(row.get("control"))) or {}).get("type")
                    ),
                    "law": str(
                        (
                            ((control_by_id.get(str(row.get("control"))) or {}).get("law") or {})
                            .get("kind")
                        )
                    ),
                },
            }
            for row in enum_report.get("unresolved_band_structures") or []
        ]
        enum_block["not_enums_any_more"] = list(enum_report.get("not_enums_any_more") or [])
        enum_block["confirmations_withheld"] = list(
            enum_report.get("confirmations_withheld") or []
        )
        enum_block["positions_moved"] = list(enum_report.get("positions_moved") or [])
        enum_block["unlabelled_regions"] = list(enum_report.get("unlabelled_regions") or [])

    # The plugin's own parameter bounds (P25). A declared range that a 900-preset
    # corpus truncated is widened to the bounds the plugin itself declares - never
    # narrowed, never shifted - and every widened control carries the before/after
    # with its provenance in `evidence.value_bounds`. The display half of the same
    # facts is recorded with it: the range is in the plugin's **state space** while
    # the host prints a transformed **display space**, and the P24 range census
    # compared the two (51 of its 103 "disagreements" were this artefact alone;
    # the other 52 were the truncations this block fixes).
    value_bounds_block: dict[str, Any] = {
        "state": "applied" if value_bounds_records else "none",
        "source": source_facts_module.CORRESPONDENCE["source"],
        "channel": source_facts_module.CORRESPONDENCE["channel"],
        "correspondence": {
            "samples_compared": source_facts_module.CORRESPONDENCE["samples_compared"],
            "texts_identical": source_facts_module.CORRESPONDENCE["texts_identical"],
            "numbers_equal_unit_label_differs": source_facts_module.CORRESPONDENCE[
                "numbers_equal_unit_label_differs"
            ],
            "numbers_differ": source_facts_module.CORRESPONDENCE["numbers_differ"],
            "absent_from_the_source": list(
                source_facts_module.CORRESPONDENCE["absent_from_the_source"]
            ),
        },
        "rule": (
            "widen only: a declared range that is a strict subset of the plugin's own "
            "bounds is replaced by them; a range that is equal, wider, shifted or absent "
            "is left exactly as it was"
        ),
        "counts": {"controls_widened": len(value_bounds_records)},
        "controls": [str(record["control"]) for record in value_bounds_records],
    }

    return {
        "profile_schema_version": 2,
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
            "sources": source_rows,
            "measured_on": {
                "host": "reaper",
                "version": version,
                "evidence": ["b4_state_blob.json", "vital_correspondence.json", "vital_scales_full.json"],
                "registry_dir": registry.directory,
            },
        },
        "calibration": calibration_block,
        "enum_boundaries": enum_block,
        "value_bounds": value_bounds_block,
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
