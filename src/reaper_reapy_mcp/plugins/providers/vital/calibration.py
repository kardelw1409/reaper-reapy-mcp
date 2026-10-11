"""Host-calibration artifact: what the *host formatter* says about each control.

The registry's sweeps are the plugin's own measured displays recorded once, long
before this layer existed. This module consumes a second, independent channel:
``TrackFX_FormatParamValueNormalized`` - a **read-only** host call that formats
any normalized value the caller passes, without touching the parameter - plus the
host's step/toggle metadata. A preparation probe
(``tests/plugins/live/probe_p22_host_calibration.py``) walks the grid and writes
one artifact; the profile build reads it, and the artifact's bytes are a declared
*source* of the profile, so its hash is part of the cache key.

What the artifact may change in a profile, and the rules that keep that honest:

- **unit** may be filled where the host's own text shows exactly one stable unit
  token and the control declares none. The provenance names the channel, the
  method version and the artifact hash - never "inferred from the name".
- **law/range/enum confirmation**: the host text at every grid point is compared
  with what the profile's own law predicts. All compared points inside each
  state's *printed* precision -> the evidence records a confirmation (an
  independent instrument agreeing with the mapping); any miss -> the evidence
  records a *declined* comparison with the observed offset, and the law keeps the
  strength it had. A near-miss never promotes anything.
- **step / is_toggle** is host metadata, recorded verbatim where the host reports
  it (``TrackFX_GetParameterStepSizes``).
- It can **never** fill a default: measured 2026-10-11 on REAPER 7.53, the host
  exposes no parameter default for this VST3 build (``TrackFX_GetNamedConfigParm
  'param.N.default_value'`` answers 0 for all 775 controls); the artifact records
  that measurement so the gap is a fact with evidence, not an omission.

Nothing here writes to the plugin; nothing here runs at import time; a malformed
artifact is refused by its own loader and the profile says so - it is never
half-applied.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from ...errors import IncompatibleSettings

__all__ = [
    "CALIBRATION_KIND",
    "CALIBRATION_METHOD_VERSION",
    "CALIBRATION_ENV",
    "DEFAULT_CALIBRATION_FILENAME",
    "Calibration",
    "default_calibration_path",
    "resolve_calibration_path",
    "load_calibration",
    "calibration_source_row",
    "apply_calibration",
    "display_precision",
]

#: The artifact's self-description: an unknown kind/version is refused, never guessed.
CALIBRATION_KIND = "vital-host-calibration"
CALIBRATION_METHOD_VERSION = 1

#: Explicit path override; a set-but-empty value is a configuration error.
CALIBRATION_ENV = "REAPER_MCP_VITAL_CALIBRATION"

DEFAULT_CALIBRATION_FILENAME = "vital_host_calibration.json"

#: How many grid points a confirmation needs before it may promote anything.
_MIN_NUMERIC_COMPARISONS = 5
_MIN_ENUM_COMPARISONS = 3

#: Bounds that keep a malformed or hostile artifact from turning into unbounded
#: profile content: the artifact is user-local data, but "user-local" is not a
#: licence to load a 500 MB blob or a 10 MB display string.
_MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
_MAX_SAMPLE_CHARS = 256
_MAX_GRID_POINTS = 4096
_MAX_CONTROLS = 8192
#: A display's printed magnitude beyond this cannot be a real measurement of a
#: plugin parameter; such a sample is treated as *unreadable*, never as a huge
#: tolerance (a hostile "1e999999" would otherwise accept any comparison).
_MAX_DECIMAL_EXPONENT = 30

#: The number at the start of a display, for its printed precision.
_NUMBER_TEXT_RE = re.compile(
    r"^(?P<sign>[+-]?)(?:(?P<int>\d+)(?:\.(?P<frac>\d*))?|\.(?P<frac_only>\d+))(?P<exp>[eE][+-]?\d+)?"
)
_NUMBER_PREFIX_RE = re.compile(r"^\s*([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)")

#: What may follow a number in a *faithful* numeric display: one unit-like token
#: (letters, or the micro/degree/percent/hash signs) and nothing else. Shared with
#: the registry-side rule of the same shape; the calibration side re-derives the
#: token from the samples rather than trusting a claim about them.
_UNIT_TOKEN_RE = re.compile(r"[A-Za-z\u00b5\u00b0#%]+")


def _short_text(value: Any, limit: int = 64) -> str | None:
    """A bounded copy of a string claimed by the artifact.

    Refusal rows travel into the profile, so a token the artifact made up cannot
    be a way to grow the profile without bound.
    """
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "..."


def _unit_token_from_samples(samples: Sequence[str]) -> tuple[str | None, bool, int]:
    """``(token, numeric, non_empty_count)`` derived **from the samples themselves**.

    The artifact states a `unit_token` and a `numeric` flag, and the merge
    *verifies* them here instead of trusting them: a claim that disagrees with the
    text it summarizes is how a wrong unit (and with it the `%`-trap's behaviour)
    could enter a profile while its provenance still said "measured". The rule is
    the registry side's rule: a number followed by nothing, or by exactly one
    unit-like token, in every non-empty sample, with one distinct token overall.
    """
    tokens: set[str] = set()
    non_empty = 0
    for sample in samples:
        text = str(sample).strip()
        if not text:
            continue
        non_empty += 1
        if len(text) > _MAX_SAMPLE_CHARS:
            return None, False, non_empty
        prefix = _NUMBER_PREFIX_RE.match(text)
        if prefix is None:
            return None, False, non_empty
        remainder = text[prefix.end():].strip()
        if remainder and not _UNIT_TOKEN_RE.fullmatch(remainder):
            return None, False, non_empty
        tokens.add(remainder)
    if non_empty == 0:
        return None, False, 0
    if len(tokens) != 1:
        return None, False, non_empty
    token = next(iter(tokens))
    return (token or None), True, non_empty


def display_precision(display: Any) -> float | None:
    """Half the reading resolution of a display's own text, in its units.

    ``"122.573 secs"`` is +/-0.0005, ``"0.0024255 secs"`` is +/-5e-8, ``"128"``
    is +/-0.5. This is the instrument budget a comparison against the host text
    must respect: a value printed to six significant digits cannot confirm a law
    any more precisely than the digits it shows.
    """
    text = str(display).strip()
    prefix = _NUMBER_PREFIX_RE.match(text)
    if prefix is None:
        return None
    number_text = prefix.group(1)
    match = _NUMBER_TEXT_RE.match(number_text)
    if match is None:  # pragma: no cover - the prefix regex guarantees the shape
        return None
    decimals = len(match.group("frac") or match.group("frac_only") or "")
    exponent = int(match.group("exp")[1:]) if match.group("exp") else 0
    if abs(exponent - decimals) > _MAX_DECIMAL_EXPONENT:
        # a magnitude no plugin display prints: unreadable, and emphatically not a
        # huge tolerance ("1e999999" must not accept every comparison, and it must
        # not overflow the power below)
        return None
    return 0.5 * 10.0 ** (exponent - decimals)


def default_calibration_path(env: Mapping[str, str] | None = None) -> Path:
    """Where a preparation run writes the artifact: user-local, never a source tree."""
    environ = os.environ if env is None else env
    root = environ.get("LOCALAPPDATA") or environ.get("XDG_CACHE_HOME")
    base = Path(root) if root else Path.home() / ".cache"
    return base / "reaper_reapy_mcp" / "calibration" / DEFAULT_CALIBRATION_FILENAME


def resolve_calibration_path(
    explicit: str | Path | None = None, env: Mapping[str, str] | None = None
) -> str | None:
    """Explicit path, else the environment override, else the default location.

    Resolution follows the registry's rules (08 §6.1): a value **set** for the
    variable is used verbatim after normalisation (no ``~``/env expansion, so a
    typo cannot silently resolve elsewhere) and an empty value is a configuration
    error, not "unset". ``None`` means "no calibration configured at all"; the
    default location is returned **even when the file does not exist** - a
    preparation run needs to know where to write.
    """
    if explicit is not None:
        return os.path.normpath(os.path.abspath(str(explicit)))
    environ = os.environ if env is None else env
    override = environ.get(CALIBRATION_ENV)
    if override is not None:
        if not str(override).strip():
            raise IncompatibleSettings(
                f"{CALIBRATION_ENV} is set to an empty value; a literal path is required",
                detail={"variable": CALIBRATION_ENV, "value": str(override), "resolved": None},
            )
        return os.path.normpath(os.path.abspath(str(override)))
    return os.path.normpath(os.path.abspath(str(default_calibration_path(environ))))


def _fail(reason: str, path: str, **extra: Any) -> IncompatibleSettings:
    detail: dict[str, Any] = {"reason": reason, "resolved_path": path}
    detail.update(extra)
    return IncompatibleSettings(f"the host calibration artifact is unusable: {reason}", detail)


@dataclass
class Calibration:
    """A loaded, shape-checked artifact with its own bytes' digest."""

    path: str
    sha256: str
    method_version: int
    identity: dict[str, Any]
    grid: list[float]
    controls: dict[str, dict[str, Any]]
    proof: dict[str, Any] = field(default_factory=dict)
    host_default_value: dict[str, Any] = field(default_factory=dict)
    host_param_space: dict[str, Any] = field(default_factory=dict)
    generated_utc: str | None = None

    def source_row(self) -> dict[str, Any]:
        return {"file": Path(self.path).name, "sha256": self.sha256}

    def matches_identity(self, identity: Mapping[str, Any]) -> bool:
        """Whether this artifact was measured on the *build* in front of us.

        A calibration measured on another build must never describe this one; the
        comparison uses the fingerprint fields the cache key already trusts.
        """
        for name in ("fx_name", "class_id", "module", "param_count"):
            if self.identity.get(name) != identity.get(name):
                return False
        return True


def load_calibration(path: str | Path) -> Calibration:
    """Read and shape-check the artifact; every failure is a typed refusal."""
    resolved = os.path.normpath(os.path.abspath(str(path)))
    candidate = Path(resolved)
    try:
        size = candidate.stat().st_size
    except OSError as exc:
        raise _fail(f"the file cannot be read: {type(exc).__name__}: {exc}", resolved) from exc
    if size > _MAX_ARTIFACT_BYTES:
        raise _fail(
            f"the artifact is {size} bytes, beyond the {_MAX_ARTIFACT_BYTES}-byte bound", resolved
        )
    try:
        data = candidate.read_bytes()
    except OSError as exc:
        raise _fail(f"the file cannot be read: {type(exc).__name__}: {exc}", resolved) from exc
    try:
        payload = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise _fail(f"the file does not parse as UTF-8 JSON: {exc}", resolved) from exc
    if not isinstance(payload, dict):
        raise _fail("the artifact is not a JSON object", resolved)
    if payload.get("kind") != CALIBRATION_KIND:
        raise _fail(f"kind {payload.get('kind')!r} is not {CALIBRATION_KIND!r}", resolved)
    version = payload.get("method_version")
    if version != CALIBRATION_METHOD_VERSION:
        raise _fail(
            f"method_version {version!r} is not {CALIBRATION_METHOD_VERSION!r}; a newer/older "
            "producer must be re-run rather than mis-read",
            resolved,
        )
    identity = payload.get("identity")
    if not isinstance(identity, dict) or not identity:
        raise _fail("the identity block is missing", resolved)
    grid = payload.get("grid")
    if not isinstance(grid, list) or len(grid) < 2:
        raise _fail("the normalized grid is missing or has fewer than 2 points", resolved)
    if len(grid) > _MAX_GRID_POINTS:
        raise _fail(f"the grid has {len(grid)} points, beyond the {_MAX_GRID_POINTS} bound", resolved)
    grid_points: list[float] = []
    for item in grid:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise _fail(f"the grid carries a non-number point: {item!r}", resolved)
        value = float(item)
        if not math.isfinite(value):
            raise _fail(f"the grid carries a non-finite point: {item!r}", resolved)
        if not 0.0 <= value <= 1.0:
            raise _fail(f"the grid point {item!r} is outside the normalized 0..1 range", resolved)
        grid_points.append(value)
    if any(later < earlier for earlier, later in zip(grid_points, grid_points[1:])):
        raise _fail("the grid is not non-decreasing", resolved)
    controls = payload.get("controls")
    if not isinstance(controls, dict) or not controls:
        raise _fail("the controls block is missing or empty", resolved)
    if len(controls) > _MAX_CONTROLS:
        raise _fail(f"the artifact carries {len(controls)} controls, beyond the bound", resolved)
    checked: dict[str, dict[str, Any]] = {}
    for control_id, entry in controls.items():
        if not isinstance(entry, dict):
            raise _fail(f"control {control_id!r} is not an object", resolved, control=control_id)
        samples = entry.get("samples")
        if not isinstance(samples, list) or len(samples) != len(grid_points):
            raise _fail(
                f"control {control_id!r} has {0 if not isinstance(samples, list) else len(samples)} "
                f"samples for a {len(grid_points)}-point grid",
                resolved,
                control=control_id,
            )
        for sample in samples:
            if not isinstance(sample, str):
                raise _fail(
                    f"control {control_id!r} has a non-string sample: {sample!r}", resolved,
                    control=control_id,
                )
            if len(sample) > _MAX_SAMPLE_CHARS:
                raise _fail(
                    f"control {control_id!r} has a sample longer than {_MAX_SAMPLE_CHARS} chars",
                    resolved,
                    control=control_id,
                )
        record = dict(entry)
        record["samples"] = list(samples)
        record["unit_token"] = entry.get("unit_token") or None
        record["numeric"] = bool(entry.get("numeric"))
        record["descending"] = bool(entry.get("descending"))
        steps = entry.get("steps")
        if isinstance(steps, dict):
            numbers: dict[str, float] = {}
            for name in ("step", "small", "large"):
                raw = steps.get(name) or 0.0
                if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                    raise _fail(
                        f"control {control_id!r} step.{name} is not a number: {raw!r}",
                        resolved,
                        control=control_id,
                    )
                value = float(raw)
                if not math.isfinite(value):
                    raise _fail(
                        f"control {control_id!r} step.{name} is not finite: {raw!r}",
                        resolved,
                        control=control_id,
                    )
                numbers[name] = value
            record["steps"] = {
                **numbers,
                "is_toggle": bool(steps.get("is_toggle")),
                "reported": bool(steps.get("reported")),
            }
        checked[str(control_id)] = record
    return Calibration(
        path=resolved,
        sha256=hashlib.sha256(data).hexdigest(),
        method_version=int(version),
        identity={str(k): v for k, v in identity.items()},
        grid=grid_points,
        controls=checked,
        proof=dict(payload.get("read_only_proof") or {}),
        host_default_value=dict(payload.get("host_default_value") or {}),
        host_param_space=dict(payload.get("host_param_space") or {}),
        generated_utc=(str(payload.get("generated_utc")) if payload.get("generated_utc") else None),
    )


def calibration_source_row(calibration: Calibration) -> dict[str, Any]:
    return calibration.source_row()


def _predict_native(control: Mapping[str, Any], normalized: float, values_module: Any) -> float:
    """The profile's own law: ``normalized -> native``."""
    return float(values_module.normalized_to_native(control, float(normalized)))


def _confirmation_record(
    control: Mapping[str, Any],
    entry: Mapping[str, Any],
    calibration: Calibration,
    values_module: Any,
) -> dict[str, Any] | None:
    """Compare the host's printed grid against the profile's law, state by state.

    Numeric controls are tested against two *parameter-free* display-space
    hypotheses - the native space itself and the percent convention (a control
    whose native range is 0..1 while the host prints 0..100) - and the outcome is
    one of four states:

    - ``confirmed``: every compared point sits inside its own printed precision
      under one exact hypothesis (the one that matched is recorded, so the
      display convention becomes a *measured* fact per control rather than an
      assumption);
    - ``consistent_within_step``: points miss their printed digits but stay inside
      the host's own display granularity (the smallest positive gap between the
      values it printed across the grid) - a weaker, still measured statement, and
      it promotes nothing;
    - ``declined``: outside both - the profile's space genuinely disagrees with the
      host's text. A constant difference across every point is recorded as a
      ``display_offset_candidate`` (a candidate, never applied);
    - ``insufficient_points``: too few comparable samples to say anything.
    """
    kind = str((control.get("law") or {}).get("kind") or "identity")
    ctype = str(control.get("type"))
    if ctype not in ("enum", "number") or kind in ("none", "unmeasured"):
        return None
    if control.get("range") is None and kind != "enum_index":
        return None
    samples: Sequence[str] = entry.get("samples") or []
    if len(samples) != len(calibration.grid):
        return None
    channel = {
        "kind": "host_formatted_display",
        "call": "TrackFX_FormatParamValueNormalized",
        "method_version": calibration.method_version,
        "artifact": calibration.source_row(),
        "write_effect": "none (the parameter is never set; measured proof in the artifact)",
    }

    if ctype == "enum":
        members = [str(member.get("id")) for member in control.get("enum_values") or []]
        comparisons = 0
        mismatches: list[dict[str, Any]] = []
        for normalized, text in zip(calibration.grid, samples):
            host_text = str(text).strip()
            if not host_text:
                continue
            try:
                predicted = values_module.normalized_to_native(control, float(normalized))
            except Exception as exc:  # noqa: BLE001 - a law that cannot convert declines
                return {
                    "state": "declined",
                    "reason": f"the profile's law cannot convert this control: {type(exc).__name__}",
                    "channel": channel,
                }
            index = int(round(float(predicted)))
            predicted_text = members[index] if 0 <= index < len(members) else None
            comparisons += 1
            if predicted_text != host_text:
                mismatches.append(
                    {"normalized": round(float(normalized), 6), "predicted": predicted_text, "host": host_text}
                )
        if comparisons < _MIN_ENUM_COMPARISONS:
            return {
                "state": "insufficient_points",
                "comparisons": comparisons,
                "needed": _MIN_ENUM_COMPARISONS,
                "channel": channel,
            }
        if not mismatches:
            return {"state": "confirmed", "comparisons": comparisons, "channel": channel}
        return {
            "state": "declined",
            "comparisons": comparisons,
            "mismatches": len(mismatches),
            "examples": mismatches[:3],
            "channel": channel,
        }

    # numeric: collect the comparable points once
    points: list[tuple[float, float, float, str]] = []  # (grid, host value, precision, text)
    for normalized, text in zip(calibration.grid, samples):
        host_text = str(text).strip()
        if not host_text:
            continue
        host_number = values_module_number(host_text)
        if host_number is None:
            continue
        number = float(host_number)
        if not math.isfinite(number):
            # `float("1e999999")` is `inf`: a sample the parser cannot bound is a
            # sample that cannot be compared, not an overflow to propagate into the
            # evidence (and from there into non-standard JSON)
            continue
        points.append(
            (
                float(normalized),
                number,
                float(display_precision(host_text) or 0.0),
                host_text,
            )
        )
    if len(points) < _MIN_NUMERIC_COMPARISONS:
        return {
            "state": "insufficient_points",
            "comparisons": len(points),
            "needed": _MIN_NUMERIC_COMPARISONS,
            "channel": channel,
        }

    try:
        hypotheses = {
            "native_space": {
                normalized: _predict_native(control, normalized, values_module)
                for normalized, _value, _precision, _text in points
            },
            "percent_space": {
                normalized: _predict_native(control, normalized, values_module) * 100.0
                for normalized, _value, _precision, _text in points
            },
        }
    except Exception as exc:  # noqa: BLE001 - a law that cannot convert declines
        return {
            "state": "declined",
            "reason": f"the profile's law cannot convert this control: {type(exc).__name__}",
            "channel": channel,
        }

    matched: list[str] = []
    for name, predicted_by_grid in hypotheses.items():
        if all(
            abs(value - predicted_by_grid[normalized]) <= precision + 1e-12
            for normalized, value, precision, _text in points
        ):
            matched.append(name)
    if matched:
        return {
            "state": "confirmed",
            "comparisons": len(points),
            "display_space": matched[0],
            "display_spaces_consistent": matched,
            "channel": channel,
        }

    # the host's own granularity: the smallest positive gap between the values it
    # printed across the grid (measured, not chosen)
    distinct = sorted({value for _n, value, _p, _t in points})
    gaps = [b - a for a, b in zip(distinct, distinct[1:]) if b - a > 0]
    step = min(gaps) if gaps else 0.0
    predicted_native = hypotheses["native_space"]
    residuals = [
        (normalized, value - predicted_native[normalized], value, precision, text)
        for normalized, value, precision, text in points
    ]
    worst = max(residuals, key=lambda row: abs(row[1]))
    if step > 0.0 and all(abs(difference) <= step for _n, difference, _v, _p, _t in residuals):
        return {
            "state": "consistent_within_step",
            "comparisons": len(points),
            "step": step,
            "worst": {
                "normalized": round(worst[0], 6),
                "difference": round(worst[1], 12),
                "display": worst[4],
            },
            "channel": channel,
        }

    mismatches = [
        {
            "normalized": round(normalized, 6),
            "predicted": round(predicted_native[normalized], 12),
            "host": value,
            "difference": round(difference, 12),
            "tolerance": precision,
        }
        for normalized, difference, value, precision, _text in residuals
        if abs(difference) > precision + 1e-12
    ]
    record: dict[str, Any] = {
        "state": "declined",
        "comparisons": len(points),
        "mismatches": len(mismatches),
        "examples": mismatches[:3],
        "channel": channel,
    }
    differences = [difference for _n, difference, _v, _p, _t in residuals]
    ordered = sorted(differences)
    median = ordered[len(ordered) // 2]
    residual_after = max(abs(value - median) for value in differences)
    worst_precision = max((precision for _n, _v, precision, _t in points), default=0.0)
    if residual_after <= max(worst_precision, 1e-9):
        record["display_offset_candidate"] = {
            "offset": round(median, 12),
            "spread": round(max(differences) - min(differences), 12),
            "worst_precision": worst_precision,
            "state": "candidate",
            "note": (
                "a constant offset explains every comparison within the printed "
                "precision: the host prints in a different display space than the "
                "profile's native space; NOT applied (no conversion change without "
                "the provider's own authority)"
            ),
        }
    return record


def values_module_number(text: str) -> float | None:
    """The numeric value of a display text, through the provider's own parser."""
    from . import depth as depth_module  # noqa: PLC0415 - same package, keeps imports light

    return depth_module.display_number(text)


def apply_calibration(
    controls: list[dict[str, Any]],
    calibration: Calibration,
    *,
    values_module: Any,
    exclude_from_unit_fill: Sequence[str] = (),
) -> dict[str, Any]:
    """Merge the artifact into freshly built controls, in place; return the report.

    Strictly additive and fail-closed: a control the artifact does not name, a
    control whose samples are not numeric, a control with an existing unit, or a
    control on the exclusion list is left exactly as it was.
    """
    report: dict[str, Any] = {
        "units_filled": [],
        "units_declined": [],
        "confirmations_confirmed": [],
        "confirmations_within_step": [],
        "confirmations_declined": [],
        "confirmations_insufficient": [],
        "steps_recorded": 0,
        "display_offset_candidates": [],
    }
    excluded = {str(item) for item in exclude_from_unit_fill}
    for control in controls:
        control_id = str(control.get("id"))
        entry = calibration.controls.get(control_id)
        if entry is None:
            continue
        steps = entry.get("steps") if isinstance(entry.get("steps"), dict) else None
        if steps and steps.get("reported"):
            control["host_steps"] = {
                "step": steps["step"],
                "small": steps["small"],
                "large": steps["large"],
                "is_toggle": bool(steps["is_toggle"]),
                "source": {
                    "kind": "host_parameter_step_sizes",
                    "call": "TrackFX_GetParameterStepSizes",
                    "method_version": calibration.method_version,
                    "artifact": calibration.source_row(),
                },
            }
            report["steps_recorded"] += 1
        token = entry.get("unit_token")
        if token and control.get("unit") is None and control.get("type") == "number":
            # the claim is **re-derived from the samples** before it is used: a
            # `unit_token` that disagrees with the text it summarizes would put a
            # wrong unit - and with it the `%`-trap's behaviour - into a profile
            # whose provenance still said "measured"
            derived_token, derived_numeric, non_empty = _unit_token_from_samples(entry["samples"])
            if derived_token != token or derived_numeric != bool(entry.get("numeric")):
                report["units_declined"].append(
                    {
                        "control": control_id,
                        "reason": "claim_disagrees_with_samples",
                        "claimed": {
                            "unit_token": _short_text(token),
                            "numeric": bool(entry.get("numeric")),
                        },
                        "derived": {
                            "unit_token": _short_text(derived_token),
                            "numeric": derived_numeric,
                        },
                    }
                )
            elif control_id in excluded:
                report["units_declined"].append({"control": control_id, "reason": "excluded_source"})
            elif non_empty < 3:
                report["units_declined"].append({"control": control_id, "reason": "too_few_samples"})
            else:
                control["unit"] = str(token)
                control["unit_source"] = {
                    "kind": "host_formatted_display",
                    "call": "TrackFX_FormatParamValueNormalized",
                    "method_version": calibration.method_version,
                    "samples": non_empty,
                    "evidence": "measured",
                    "artifact": calibration.source_row(),
                    "note": (
                        "one stable unit token across every non-empty grid sample, "
                        "re-derived from the samples by the consumer"
                    ),
                }
                report["units_filled"].append(control_id)
        confirmation = _confirmation_record(control, entry, calibration, values_module)
        if confirmation is None:
            continue
        state = confirmation["state"]
        if state == "confirmed":
            evidence = dict(control.get("evidence") or {})
            evidence["strength_before"] = str(evidence.get("strength") or "none")
            evidence["strength"] = "strong"
            evidence["confirmation"] = confirmation
            control["evidence"] = evidence
            report["confirmations_confirmed"].append(control_id)
        elif state == "consistent_within_step":
            # a weaker, still measured statement: the law agrees with the host to
            # within the host's own display granularity, but not to its printed
            # digits. Recorded; promotes nothing.
            evidence = dict(control.get("evidence") or {})
            evidence["confirmation"] = confirmation
            control["evidence"] = evidence
            report["confirmations_within_step"].append(control_id)
        elif state == "declined":
            evidence = dict(control.get("evidence") or {})
            evidence["confirmation"] = confirmation
            control["evidence"] = evidence
            report["confirmations_declined"].append(control_id)
            if confirmation.get("display_offset_candidate"):
                report["display_offset_candidates"].append(control_id)
        else:
            evidence = dict(control.get("evidence") or {})
            evidence["confirmation"] = confirmation
            control["evidence"] = evidence
            report["confirmations_insufficient"].append(control_id)
    report["counts"] = {
        "units_filled": len(report["units_filled"]),
        "units_declined": len(report["units_declined"]),
        "confirmations_confirmed": len(report["confirmations_confirmed"]),
        "confirmations_within_step": len(report["confirmations_within_step"]),
        "confirmations_declined": len(report["confirmations_declined"]),
        "confirmations_insufficient": len(report["confirmations_insufficient"]),
        "steps_recorded": report["steps_recorded"],
        "display_offset_candidates": len(report["display_offset_candidates"]),
    }
    return report
