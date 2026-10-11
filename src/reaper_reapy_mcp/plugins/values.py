"""Typed value resolution and law application (contract 05 §4; 10 §3).

Core dispatches on the law KIND - the universal mechanics - and treats
``law.params``, ``binding`` and ``range`` as opaque provider data. A refusal is
a first-class answer: an unmeasured law is never approximated, an enum never
interpolates, and a unit trap is refused instead of silently reinterpreted.
"""

from __future__ import annotations

import math
from typing import Any

from .errors import InvalidValue, NoConversion

__all__ = [
    "LAW_KINDS",
    "law_kind",
    "direction",
    "strict_bool",
    "unit_trap",
    "validate",
    "to_native",
    "to_normalized",
    "from_native",
    "normalized_to_native",
    "display_to_write_space",
]

LAW_KINDS = ("affine", "enum_index", "exponential", "identity", "none", "power", "unmeasured")

_NUMBER_TYPES = (int, float)


def strict_bool(value: Any, name: str = "value") -> bool:
    """A real boolean, or a refusal.

    A truthiness test would be a **fail-open** here: a JSON falsy non-boolean
    (``0``, ``""``, ``[]``) is how a client says "no", and ``bool()`` would turn
    it into "yes, apply the mutation". Only ``True`` and ``False`` are accepted;
    everything else is refused with the observed value in the detail.
    """
    if isinstance(value, bool):
        return value
    raise InvalidValue(
        f"`{name}` must be a boolean",
        detail={"argument": name, "value": value, "type": type(value).__name__},
    )


def law_kind(control: dict[str, Any]) -> str:
    law = control.get("law") or {}
    return str(law.get("kind") or "identity")


def _law_params(control: dict[str, Any]) -> dict[str, Any]:
    return dict((control.get("law") or {}).get("params") or {})


def _range(control: dict[str, Any]) -> tuple[float | None, float | None]:
    bounds = control.get("range") or {}
    low, high = bounds.get("min"), bounds.get("max")
    return (float(low) if low is not None else None, float(high) if high is not None else None)


def _enum_members(control: dict[str, Any]) -> list[str]:
    return [str(member.get("id")) for member in control.get("enum_values") or []]


def direction(control: dict[str, Any]) -> str:
    """The measured conversion direction of a numeric control's law.

    ``range`` always carries the numeric extremes (``min <= max``) and the law
    maps ``normalized 0 -> range.max`` \u2192 ``normalized 1 -> range.min`` when the
    direction is **descending**; ``normalized 0 -> range.min`` when ascending.
    Present only where a provider *measured* the direction from the control's own
    sweep: seven frequency controls of the first provider run from their maximum
    at normalized 0 down to their minimum at 1 (measured 2026-10-11), and the
    previous build refused them outright rather than inventing an inverted
    mapping.
    """
    raw = str(_law_params(control).get("direction") or "ascending").strip().lower()
    return "descending" if raw == "descending" else "ascending"


def _enum_positions(control: dict[str, Any]) -> list[float] | None:
    """The **measured** normalized position of each enum member, when declared.

    An enum whose bands are uniform converts with ``index / (n - 1)``; one whose
    bands are not (the plugin measured here: switch points at 0, 11/256, 32/256,
    54/256, ... ) needs the members' own measured positions, or the uniform
    arithmetic lands in the neighbouring band and reports success. Present only
    where a provider fitted the band midpoints from a measured sweep; absent for
    every law the registry declares itself, so nothing that works today changes.
    """
    params = _law_params(control)
    raw = params.get("positions")
    members = _enum_members(control)
    if not isinstance(raw, (list, tuple)) or len(raw) != len(members) or len(raw) < 2:
        return None
    positions: list[float] = []
    for item in raw:
        if isinstance(item, bool) or not isinstance(item, _NUMBER_TYPES):
            return None
        positions.append(float(item))
    return positions


def unit_trap(control: dict[str, Any], value: Any) -> None:
    """A ``%``-unit control receiving ``1.0`` means 1 percent (05 §4.7)."""
    if control.get("unit") == "%" and isinstance(value, _NUMBER_TYPES) and not isinstance(value, bool):
        if float(value) == 1.0:
            raise InvalidValue(
                "a 1.0 on a percent-unit control means 1 percent; pass the value in percent",
                detail={"control": control.get("id"), "unit": "%", "value": value, "trap": True},
            )


def validate(control: dict[str, Any], value: Any) -> None:
    """Type/range/enum validation before any conversion (05 §6)."""
    unit_trap(control, value)
    ctype = control.get("type")
    if ctype == "enum":
        members = _enum_members(control)
        if str(value) not in members:
            raise InvalidValue(
                f"unknown enum member {value!r}", detail={"control": control.get("id"), "legal": members}
            )
        return
    if ctype == "bool":
        if isinstance(value, bool) or str(value).lower() in ("on", "off", "true", "false"):
            return
        raise InvalidValue(
            f"expected a boolean, got {value!r}", detail={"control": control.get("id")}
        )
    if ctype == "state_ref":
        return
    if isinstance(value, bool) or not isinstance(value, _NUMBER_TYPES):
        raise InvalidValue(
            f"expected a number, got {value!r}", detail={"control": control.get("id"), "type": ctype}
        )
    low, high = _range(control)
    if low is not None and high is not None and not (low <= float(value) <= high):
        raise InvalidValue(
            f"value {value!r} outside the declared range",
            detail={"control": control.get("id"), "range": {"min": low, "max": high}},
        )


def to_native(control: dict[str, Any], value: Any) -> float:
    """Display-space value -> provider native value through the declared law."""
    validate(control, value)
    ctype = control.get("type")
    if ctype == "enum":
        return float(_enum_members(control).index(str(value)))
    if ctype == "bool":
        if isinstance(value, bool):
            return float(value)
        return 1.0 if str(value).lower() in ("on", "true") else 0.0
    if ctype == "state_ref":
        # structural values are carried verbatim; they are not numbers
        return value  # type: ignore[return-value]
    kind = law_kind(control)
    if kind in ("none", "unmeasured"):
        raise NoConversion(
            f"control {control.get('id')!r} declares no law ({kind!r}) - conversion is refused",
            detail={"control": control.get("id"), "law": kind},
        )
    number = float(value)
    if kind == "affine":
        params = _law_params(control)
        return float(params.get("scale", 1.0)) * number + float(params.get("offset", 0.0))
    return number


def to_normalized(control: dict[str, Any], native: Any) -> float:
    """Native value -> the 0..1 host parameter value."""
    ctype = control.get("type")
    if ctype == "enum":
        members = _enum_members(control)
        index = int(native)
        positions = _enum_positions(control)
        if positions is not None:
            return positions[max(0, min(index, len(positions) - 1))]
        return index / max(1, len(members) - 1)
    if ctype == "bool":
        return float(bool(native))
    if ctype == "state_ref":
        return 1.0 if native else 0.0
    low, high = _range(control)
    kind = law_kind(control)
    descending = direction(control) == "descending"
    if kind == "power":
        base = float(_law_params(control).get("base") or 0.0)
        exponent = float(_law_params(control).get("exponent") or 0.0)
        offset = float(_law_params(control).get("offset") or 0.0)
        shifted = float(native) - offset
        if base > 0.0 and exponent > 0.0 and shifted >= 0.0:
            spine = (shifted / base) ** (1.0 / exponent)
            return (1.0 - spine) if descending else spine
        raise NoConversion(
            f"control {control.get('id')!r} declares a power law without a usable base/exponent",
            detail={"control": control.get("id"), "law": kind, "base": base, "exponent": exponent},
        )
    if kind == "exponential" and low and high and low > 0 and high != low:
        number = float(native)
        if number <= 0.0:
            raise NoConversion(
                f"control {control.get('id')!r} declares an exponential law whose values are "
                "positive; a non-positive native value has no normalized position",
                detail={"control": control.get("id"), "law": kind, "native": number, "min": low},
            )
        spine = math.log(number / low) / math.log(high / low)
        return (1.0 - spine) if descending else spine
    if low is not None and high is not None and high != low:
        spine = (float(native) - low) / (high - low)
        return (1.0 - spine) if descending else spine
    return float(native)


def _enum_switch_points(control: dict[str, Any]) -> list[float] | None:
    """The members' measured band starts, when a provider fitted them.

    The declared uniform mapping assumes equally wide bands, so a value can be
    resolved by rounding. A **measured** band map is not equally wide, and a value
    sitting exactly at a switch point belongs to the member that *starts* there -
    measured on this plugin: normalized 0.0625 formats as "24dB", which begins at
    0.0625, while the midpoint the write uses is 0.125. Inverting by nearest
    midpoint read that value as the member *below* it (5 of 64 grid points on the
    filter styles, found by an independent review of 2026-10-11).
    """
    params = _law_params(control)
    raw = params.get("switch_points")
    members = _enum_members(control)
    if not isinstance(raw, (list, tuple)) or len(raw) != len(members) or len(raw) < 2:
        return None
    points: list[float] = []
    for item in raw:
        if isinstance(item, bool) or not isinstance(item, _NUMBER_TYPES):
            return None
        points.append(float(item))
    if any(later < earlier for earlier, later in zip(points, points[1:])):
        return None
    return points


def normalized_to_native(control: dict[str, Any], normalized: float) -> Any:
    ctype = control.get("type")
    if ctype == "enum":
        members = _enum_members(control)
        switch_points = _enum_switch_points(control)
        if switch_points is not None:
            index = 0
            for candidate, point in enumerate(switch_points):
                if float(normalized) >= point:
                    index = candidate
            return float(max(0, min(index, len(members) - 1)))
        positions = _enum_positions(control)
        if positions is not None:
            nearest = min(range(len(positions)), key=lambda i: abs(positions[i] - float(normalized)))
            return float(nearest)
        # Uniform bands: bands of width 1/(n-1) centred on i/(n-1), so the switch
        # between member i and i+1 sits at (i+0.5)/(n-1) and the member that
        # *starts* at a boundary owns it. Half-UP rounding is what states that;
        # Python's `round()` is half-to-even, which read normalized 0.5 on a
        # two-member control as "Off" while the host itself formats it as "On"
        # (measured 2026-10-11 by the host-display channel: `chorus_on`,
        # `compressor_on` and every other 2..11-member uniform enum whose grid
        # point landed exactly on a boundary).
        index = int(math.floor(float(normalized) * max(1, len(members) - 1) + 0.5))
        return float(max(0, min(index, len(members) - 1)))
    if ctype == "bool":
        return 1.0 if float(normalized) >= 0.5 else 0.0
    if ctype == "state_ref":
        return bool(normalized)
    low, high = _range(control)
    kind = law_kind(control)
    descending = direction(control) == "descending"
    if kind == "power":
        base = float(_law_params(control).get("base") or 0.0)
        exponent = float(_law_params(control).get("exponent") or 0.0)
        offset = float(_law_params(control).get("offset") or 0.0)
        if base > 0.0 and exponent > 0.0:
            spine = float(normalized) if not descending else 1.0 - float(normalized)
            return offset + base * spine ** exponent
        raise NoConversion(
            f"control {control.get('id')!r} declares a power law without a usable base/exponent",
            detail={"control": control.get("id"), "law": kind, "base": base, "exponent": exponent},
        )
    spine = (1.0 - float(normalized)) if descending else float(normalized)
    if kind == "exponential" and low and high and low > 0 and high != low:
        return low * (high / low) ** spine
    if low is not None and high is not None and high != low:
        return low + spine * (high - low)
    return float(normalized)


def from_native(control: dict[str, Any], native: Any) -> Any:
    """Native value -> display-space value (the inverse of ``to_native``)."""
    ctype = control.get("type")
    if ctype == "enum":
        members = _enum_members(control)
        index = int(native)
        if 0 <= index < len(members):
            return members[index]
        return None
    if ctype == "bool":
        return bool(native)
    if ctype == "state_ref":
        return native
    kind = law_kind(control)
    number = float(native)
    if kind == "affine":
        params = _law_params(control)
        scale = float(params.get("scale", 1.0)) or 1.0
        return (number - float(params.get("offset", 0.0))) / scale
    return number


def display_to_write_space(control: dict[str, Any], value: Any) -> dict[str, Any]:
    """The one conversion the write uses; verification re-uses the same one."""
    native = to_native(control, value)
    normalized = None if control.get("type") == "state_ref" else to_normalized(control, native)
    return {"display": value, "native": native, "normalized": normalized}
