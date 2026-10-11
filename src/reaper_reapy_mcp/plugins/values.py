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
    "strict_bool",
    "unit_trap",
    "validate",
    "to_native",
    "to_normalized",
    "from_native",
    "normalized_to_native",
    "display_to_write_space",
]

LAW_KINDS = ("affine", "enum_index", "exponential", "identity", "none", "unmeasured")

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
        return index / max(1, len(members) - 1)
    if ctype == "bool":
        return float(bool(native))
    if ctype == "state_ref":
        return 1.0 if native else 0.0
    low, high = _range(control)
    kind = law_kind(control)
    if kind == "exponential" and low and high and low > 0 and high != low:
        return math.log(float(native) / low) / math.log(high / low)
    if low is not None and high is not None and high != low:
        return (float(native) - low) / (high - low)
    return float(native)


def normalized_to_native(control: dict[str, Any], normalized: float) -> Any:
    ctype = control.get("type")
    if ctype == "enum":
        members = _enum_members(control)
        index = round(float(normalized) * max(1, len(members) - 1))
        return float(max(0, min(index, len(members) - 1)))
    if ctype == "bool":
        return 1.0 if float(normalized) >= 0.5 else 0.0
    if ctype == "state_ref":
        return bool(normalized)
    low, high = _range(control)
    kind = law_kind(control)
    if kind == "exponential" and low and high and low > 0 and high != low:
        return low * (high / low) ** float(normalized)
    if low is not None and high is not None and high != low:
        return low + float(normalized) * (high - low)
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
