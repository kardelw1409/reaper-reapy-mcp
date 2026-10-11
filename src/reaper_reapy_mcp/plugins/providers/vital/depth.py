"""Depth conversion for Vital activation routes (10 §2.2; port of the measured fit).

The conversion is a provider hook, never a universal formula. The algorithm is
ported from the POC's `adapter/capabilities.py` (`_linear_fit`, `depth_fit`,
`modulation_depth`), which fitted it over the plugin's own measured display
states and recorded the outcome: a depth is accepted where the destination's
display is affine in the normalized parameter within half of its own smallest
step, and **refused with a reason** where it is not.

Refusals carried over from the recorded POC run: eight LFO frequencies,
Volume, Reverb Decay Time and Oscillator 1 Unison Detune - controls a curve or
a decibel taper passes through, where "semitones per amount" does not exist.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

from ...errors import InvalidValue, NoConversion

__all__ = [
    "AMOUNT_RANGE",
    "DepthFit",
    "display_number",
    "display_unit",
    "linear_fit",
    "states_of",
    "percent_controls",
    "depth_fit",
    "modulation_depth",
    "depth_table",
]

#: The amount range the document itself declares (POC: `AMOUNT_RANGE`).
AMOUNT_RANGE = (-1.0, 1.0)

#: Ported from `adapter/capabilities.py`: exponent-aware leading-number parser.
#: A naive `[0-9.]+` scan reads `1.95312ms` as `1.95312` but reads `7.45065e-08`
#: as `7.45065` - five orders of magnitude wrong (the recorded trap).
_NUMBER_RE = re.compile(r"^\s*([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)")


@dataclass
class DepthFit:
    destination: str
    control: str | None
    native_per_unit: float | None
    unit: str
    offset: float | None
    residual: float | None
    quantum: float | None
    convertible: bool
    why: str

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "destination": self.destination,
            "control": self.control,
            "depth_unit": self.unit,
            "native_per_unit_of_amount": (
                round(self.native_per_unit, 9) if self.native_per_unit is not None else None
            ),
            "convertible": self.convertible,
            "why": self.why,
        }
        if self.convertible and self.native_per_unit is not None:
            payload["amount_range"] = list(AMOUNT_RANGE)
            payload["depth_range"] = [
                round(AMOUNT_RANGE[0] * self.native_per_unit, 6),
                round(AMOUNT_RANGE[1] * self.native_per_unit, 6),
            ]
            payload["residual"] = self.residual
            payload["quantum"] = self.quantum
        return payload


def display_number(display: Any) -> float | None:
    """Port of `capabilities.display_number`: the leading number, exponent-aware."""
    if display is None:
        return None
    match = _NUMBER_RE.match(str(display))
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:  # pragma: no cover - the regex guarantees a float
        return None


def display_unit(display: Any) -> str:
    """Port of `capabilities.display_unit`: ``'24 dB'`` -> ``'dB'``, ``'On'`` -> ``''``."""
    text = str(display or "")
    match = _NUMBER_RE.match(text)
    if not match:
        return ""
    return text[match.end():].strip()


def states_of(scales: dict[str, Any], name: str) -> list[tuple[float, str]]:
    """Port of `capabilities._states`: the measured (normalized, display) list."""
    raw = (scales.get(name) or {}).get("states") or []
    out: list[tuple[float, str]] = []
    for entry in raw:
        try:
            out.append((float(entry[0]), str(entry[1])))
        except (TypeError, ValueError, IndexError):
            continue
    out.sort(key=lambda pair: pair[0])
    return out


def percent_controls(scales: dict[str, Any]) -> set[str]:
    """Port of `capabilities.percentage_controls`: derived from the measurement.

    A hand-typed list is exactly how ``Filter 1 Mix = 1.0`` became a live bug, so
    the set is computed from the state lists instead.
    """
    found: set[str] = set()
    for name, scale in scales.items():
        states = (scale or {}).get("states") or []
        if states and str(states[-1][1]).strip().endswith("%"):
            found.add(name)
    return found


def linear_fit(pairs: list[tuple[float, float]]) -> tuple[float, float, float] | None:
    """Port of `capabilities._linear_fit`: least-squares ``value = k*norm + b``."""
    count = len(pairs)
    if count < 3:
        return None
    sx = sum(pair[0] for pair in pairs)
    sy = sum(pair[1] for pair in pairs)
    sxx = sum(pair[0] * pair[0] for pair in pairs)
    sxy = sum(pair[0] * pair[1] for pair in pairs)
    denominator = count * sxx - sx * sx
    if abs(denominator) < 1e-18:
        return None
    k = (count * sxy - sx * sy) / denominator
    b = (sy - k * sx) / count
    return k, b, max(abs(pair[1] - (k * pair[0] + b)) for pair in pairs)


def _row_for(correspondence_rows: Iterable[dict[str, Any]], destination: str) -> dict[str, Any] | None:
    for row in correspondence_rows:
        if row.get("state_key") == destination:
            return row
    return None


def depth_fit(
    correspondence_rows: Iterable[dict[str, Any]],
    scales: dict[str, Any],
    destination: str,
) -> DepthFit:
    """Port of `capabilities.depth_fit`, over registry data instead of globals."""
    row = _row_for(correspondence_rows, destination)
    if row is None:
        return DepthFit(
            destination, None, None, "", None, None, None, False,
            f"{destination!r} is not a measured modulation destination",
        )
    name = row["name"]
    states = states_of(scales, name)
    pairs = [(norm, value) for norm, display in states if (value := display_number(display)) is not None]
    if len(pairs) < 3:
        return DepthFit(
            destination, name, None, "", None, None, None, False,
            "the destination's displays are named states, so a numeric depth has no unit to convert from",
        )
    units = {display_unit(display) for _norm, display in states if display_number(display) is not None}
    unit = units.pop() if len(units) == 1 else ""
    values = sorted({value for _norm, value in pairs})
    gaps = [b - a for a, b in zip(values, values[1:]) if b - a > 0]
    quantum = min(gaps) if gaps else 0.0
    fit = linear_fit(pairs)
    if fit is None:  # pragma: no cover - a degenerate state list
        return DepthFit(
            destination, name, None, unit, None, None, quantum, False,
            "the state list is degenerate; no fit is possible",
        )
    k, b, residual = fit
    bound = 0.5 * quantum + 1e-9
    if residual > bound:
        return DepthFit(
            destination, name, None, unit, b, residual, quantum, False,
            (
                "the display is not affine in normalized: the worst residual of a linear fit "
                f"is {residual:.6g} against half the display's own step ({quantum:.6g}/2), so the "
                "travel per unit amount is not one number"
            ),
        )
    return DepthFit(
        destination, name, round(k, 9), unit, round(b, 9), residual, quantum, True,
        "measured: the display is affine in normalized, within half of its own smallest step",
    )


def modulation_depth(
    correspondence_rows: Iterable[dict[str, Any]],
    scales: dict[str, Any],
    destination: str,
    depth: float,
) -> dict[str, Any]:
    """Port of `capabilities.modulation_depth`: depth in the destination's unit -> amount.

    A depth of exactly 0 is not representable as an amount (``amount = 0`` leaves
    the plugin's own default of 0.5 in force), so the result says so explicitly.
    """
    fit = depth_fit(correspondence_rows, scales, destination)
    if not fit.convertible:
        raise NoConversion(
            f"a depth in native units cannot be converted for {destination!r}",
            detail={
                "why": fit.why,
                "control": fit.control,
                "unit": fit.unit or None,
                "alternatives": [
                    "state the depth as a normalized amount in [-1, 1]",
                    "state the depth as an absolute display value for the control",
                    "choose a destination whose display is affine in normalized",
                ],
            },
        )
    amount = float(depth) / float(fit.native_per_unit)
    if abs(amount) > AMOUNT_RANGE[1] + 1e-9:
        raise InvalidValue(
            f"a depth of {depth}{(' ' + fit.unit) if fit.unit else ''} needs an amount of "
            f"{amount:.6g}, outside the document's {AMOUNT_RANGE}",
            detail={
                "destination": destination,
                "max_depth": fit.native_per_unit * AMOUNT_RANGE[1],
                "unit": fit.unit or None,
                "why": "the document's declared amount range is -1..+1",
            },
        )
    return {
        "destination": destination,
        "control": fit.control,
        "requested_depth": depth,
        "unit": fit.unit,
        "amount": round(amount, 9),
        "is_a_set": float(depth) == 0.0,
        "writes_zero": float(depth) == 0.0,
        "note": (
            "a depth of 0 must be written as amount 0.0 explicitly; omitting the field leaves "
            "the plugin's default 0.5 in force"
            if float(depth) == 0.0
            else "forward travel for a positive depth; pass a negative depth for the opposite direction"
        ),
    }


def depth_table(
    correspondence_rows: Iterable[dict[str, Any]],
    scales: dict[str, Any],
    destinations: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Which destinations convert and which refuse, with the recorded reason."""
    accepted: dict[str, Any] = {}
    refused: dict[str, Any] = {}
    if destinations is None:
        destinations = [str(row.get("state_key")) for row in correspondence_rows if row.get("state_key")]
    for destination in destinations:
        fit = depth_fit(correspondence_rows, scales, destination)
        if fit.convertible:
            accepted[destination] = fit.native_per_unit
        else:
            refused[destination] = fit.why
    return {"accepted": accepted, "refused": refused, "n_accepted": len(accepted), "n_refused": len(refused)}
