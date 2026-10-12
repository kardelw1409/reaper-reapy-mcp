"""One requested control value, resolved into the row the write path consumes.

Why this is its own module: **two independent scenarios share one vocabulary**.

- a **display** request names what a human reads ("Sine", 120.0 Hz, 250 ms) and
  needs a *confirmed* conversion into the host's 0..1 space - a control whose law
  was never verified cannot honour it, and inventing one is exactly the defect the
  layer exists to prevent;
- a **normalized** request names the host's own parameter value (``0.0..1.0``).
  It needs **no** law at all: the value is the space REAPER itself writes in, so
  a control the artifact could not model is still writable *by explicit request*.

P28's write path refused the second case as if it were the first: a batch
containing one law-less control was refused whole, which is right for a display
request and wrong for a caller who states the normalized value itself.

Three rules keep that honest:

1. the mode is **explicit** - a bare scalar keeps its historical meaning (display
   space) and a normalized request must say so, so no caller can drift into the
   host space by accident;
2. "no semantics were claimed" is **reported** as such (``display: None`` and a
   named reason), never rendered as a display value that merely happens to equal
   the normalized number;
3. a control whose discrete members are known (``enum``/``bool``) **refuses** a
   normalized request and names the members, because a raw position inside a
   labelled band is not a value anybody can verify afterwards.

Both providers resolve through this one function, so the request vocabulary is
identical whether the profile came from a provider or from a discovery artifact.
"""

from __future__ import annotations

from typing import Any, Mapping

from .errors import InvalidValue, NoConversion

__all__ = ["VALUE_MODES", "normalize_request_mode", "resolve_control_value"]

#: The value modes a control entry may name, in the order they are offered.
VALUE_MODES = ("display", "native", "normalized")

#: Controls whose members are *known labels*: a raw normalized position inside a
#: band would be unverifiable (the read-back could not say which label the plugin
#: chose), so these refuse and point at the label instead.
_LABELLED_TYPES = ("enum", "bool", "state_ref")


def _problem(control_id: Any, code: str, message: str, **detail: Any) -> dict[str, Any]:
    return {"control": control_id, "code": code, "message": message, "detail": detail}


def _number(value: Any, where: str, control_id: Any) -> float:
    """A real, finite number (a bool is *not* one - it is a different request)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidValue(
            f"{where} must be a number for this control",
            detail={"control": control_id, "argument": where, "type": type(value).__name__},
        )
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise InvalidValue(
            f"{where} must be finite",
            detail={"control": control_id, "argument": where, "value": str(value)},
        )
    return number


def normalize_request_mode(request: Mapping[str, Any]) -> str:
    """The batch's unresolved-control policy: ``refuse`` (default) or ``skip``.

    ``refuse`` is the historical behaviour and stays the default: a batch whose
    pre-flight finds a problem writes **nothing**, and the refusal names every
    offender. ``skip`` is an explicit opt-in for the case the caller wants the
    rest of a batch applied while the refused entries are reported per control -
    it never turns a refusal into a silent omission: every skipped control is
    returned with its own typed problem.
    """
    value = request.get("on_unresolved", "refuse")
    if isinstance(value, bool) or not isinstance(value, str):
        raise InvalidValue(
            "`on_unresolved` must be 'refuse' or 'skip'",
            detail={"argument": "on_unresolved", "value": str(value)},
        )
    mode = value.strip().lower()
    if mode not in ("refuse", "skip"):
        raise InvalidValue(
            f"unknown `on_unresolved` mode {value!r}",
            detail={"argument": "on_unresolved", "accepted": ["refuse", "skip"]},
        )
    return mode


def resolve_control_value(
    control: Mapping[str, Any],
    value: Any,
    *,
    values_module: Any = None,
    coerce: Any = None,
) -> dict[str, Any]:
    """Resolve one control's requested value; return ``{"row"|"problem"}``.

    The row keeps the shape the transaction expects (``control``/``display``/
    ``native``/``normalized``/``channel``/``binding``) and adds ``value_space`` so
    the response can say **which space was written** and whether a display claim
    exists at all.

    ``coerce`` is an optional provider hook applied to a **display** payload
    before conversion (the providerless path uses it for its one narrow
    coercion: a boolean word for a two-member enum). It is never applied to a
    normalized request - that value is already in the space the host writes.
    """
    if values_module is None:  # imported here: `values` imports this module's errors
        from . import values as values_module  # type: ignore[no-redef]

    control_id = control.get("id")
    binding = dict(control.get("binding") or {})
    if str((control.get("authority") or {}).get("write") or "") != "parameter":
        return {
            "problem": _problem(
                control_id,
                "UnsupportedControl",
                "this control is not writable through the host parameter channel",
                authority=dict(control.get("authority") or {}),
            )
        }

    mode = "display"
    payload = value
    if isinstance(value, Mapping):
        named = [key for key in VALUE_MODES if key in value]
        unknown = [key for key in value if key not in VALUE_MODES]
        if unknown:
            return {
                "problem": _problem(
                    control_id,
                    "InvalidValue",
                    f"unknown value key(s) {sorted(unknown)!r}",
                    accepted=list(VALUE_MODES),
                    control=str(control_id),
                )
            }
        if len(named) != 1:
            return {
                "problem": _problem(
                    control_id,
                    "InvalidValue",
                    "name exactly one value mode",
                    named=named or None,
                    accepted=list(VALUE_MODES),
                    control=str(control_id),
                )
            }
        mode, payload = named[0], value[named[0]]

    if mode == "display" and isinstance(payload, str):
        # A **labelled state** of a control that is otherwise numeric: measured
        # live on Surge, whose oscillator volumes print "-inf dB", "-72.00 dB", ...
        # (a 17-state ladder whose minimum is not a number). The label is real
        # evidence - the artifact printed it at a measured position - so a request
        # naming it is honoured by writing *that* position, and the read-back text
        # is the confirmation. A label nobody measured is refused, never guessed.
        states = list(control.get("labelled_states") or [])
        match = next(
            (
                state
                for state in states
                if str(state.get("id")) == payload or str(state.get("label")) == payload
            ),
            None,
        )
        if match is not None:
            return {
                "row": {
                    "control": dict(control),
                    "display": payload,
                    "native": None,
                    "normalized": float(match["position"]),
                    "channel": "parameter",
                    "binding": binding,
                    "value_space": "display",
                    "display_claim": {
                        "available": True,
                        "reason": "labelled_state",
                        "note": (
                            "the label was resolved to the position the artifact measured it at "
                            f"({match.get('basis')}); the read-back text says whether the plugin "
                            "now holds that state"
                        ),
                    },
                }
            }
        if states:
            # The control HAS a labelled state list and the name is not in it: that
            # is a different answer from "this is not a number", and the options are
            # what a caller needs.
            return {
                "problem": _problem(
                    control_id,
                    "UnsupportedControl",
                    f"unknown state {payload!r} for this control",
                    states=[str(state.get("id")) for state in states[:24]],
                    reason="unknown_labelled_state",
                )
            }

    if mode == "normalized":
        if str(control.get("type")) in _LABELLED_TYPES:
            members = [
                str((item or {}).get("id")) for item in (control.get("enum_values") or [])
            ]
            return {
                "problem": _problem(
                    control_id,
                    "InvalidValue",
                    (
                        "this control's states are labelled; address them by label, because a raw "
                        "normalized position inside a band cannot be verified afterwards"
                    ),
                    type=control.get("type"),
                    members=members[:24] or None,
                )
            }
        try:
            normalized = _number(payload, "normalized", control_id)
        except InvalidValue as exc:
            return {"problem": _problem(control_id, exc.code, exc.message, **exc.detail)}
        if not 0.0 <= normalized <= 1.0:
            return {
                "problem": _problem(
                    control_id,
                    "InvalidValue",
                    "`normalized` must lie in the host's own 0..1 parameter space",
                    normalized=normalized,
                    reason="outside_host_normalized_space",
                )
            }
        return {
            "row": {
                "control": dict(control),
                "display": None,
                "native": None,
                "normalized": normalized,
                "channel": "parameter",
                "binding": binding,
                "value_space": "normalized",
                "display_claim": {
                    "available": False,
                    "reason": "explicit_normalized_request",
                    "note": (
                        "the caller wrote in the host's own space, so this layer claims no "
                        "physical meaning for the value; `plugin_read` reports the plugin's own "
                        "text for whatever it now holds"
                    ),
                },
            }
        }

    # display and native both go through the confirmed conversion, which is the
    # only reason a law is required at all
    if not values_module.has_display_law(control):
        # `to_normalized` ends in an identity fallback for a control with neither a
        # law nor a declared range, so without this guard a `native` request on a
        # law-less control would be written as if the plugin's own value *were* the
        # normalized one - an assumption, not a conversion. Say so, and name the
        # mode that needs no law at all.
        return {
            "problem": _problem(
                control_id,
                "NoConversion",
                (
                    f"control {control_id!r} declares no verified law "
                    f"({str((control.get('law') or {}).get('kind') or 'none')!r}): a {mode!r} "
                    "value cannot be converted, and no conversion is invented"
                ),
                law=str((control.get("law") or {}).get("kind") or "none"),
                remedy=(
                    "state the value in the host's own space instead: "
                    '{"normalized": <0..1>}'
                ),
            )
        }
    if mode == "display" and coerce is not None:
        payload = coerce(control, payload)
    try:
        if mode == "native":
            native = _number(payload, "native", control_id)
            normalized = values_module.to_normalized(control, native)
        else:
            native = values_module.to_native(control, payload)
            normalized = values_module.to_normalized(control, native)
    except (InvalidValue, NoConversion) as exc:
        return {"problem": _problem(control_id, exc.code, exc.message, **dict(exc.detail or {}))}
    if normalized is None:
        return {
            "problem": _problem(
                control_id,
                "NoConversion",
                "this control has no normalized write target",
                law=str((control.get("law") or {}).get("kind")),
            )
        }
    return {
        "row": {
            "control": dict(control),
            "display": payload,
            "native": native,
            "normalized": float(normalized),
            "channel": "parameter",
            "binding": binding,
            "value_space": mode,
            "display_claim": {"available": True, "reason": None, "note": None},
        }
    }
