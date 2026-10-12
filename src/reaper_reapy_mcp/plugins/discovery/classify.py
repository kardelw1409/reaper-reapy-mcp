"""Parameter classification (P26 §9).

The classifier combines the *independent* evidence a run collected - the host's
toggle flag and step sizes, the shape of the printed text, the band structure,
the fitted conversion shape - into one class with an explicit confidence and an
explicit list of the reasons that decided it. It never invents a class from a
single signal the brief warns about:

- a small set of distinct strings is not an enumeration when the values still
  follow a fitted law (coarse display rounding);
- ``step = 1`` is corroborating evidence, never the deciding one;
- a numeric ramp whose display is faithful to the position is ``ramp_like``, not
  an enumeration with as many members as the sampling grid had points.

Every rule that fires appends a reason string, so an artifact reviewer can see
*why* a control landed in a class instead of re-deriving it.
"""

from __future__ import annotations

from typing import Any, Sequence

from .text import is_integer_valued, parse_display
from .types import Classification, DiscreteAnalysis, MappingAnalysis, StepSizes

__all__ = ["CLASS_KINDS", "classify_control"]

#: Class vocabulary. The list is open for extension: a new class is a new string
#: with its own rule, never a change to the consumer contract.
CLASS_KINDS: tuple[str, ...] = (
    "continuous",
    "integer_like",
    "boolean",
    "enum",
    "ramp_like",
    "stepped",
    "unknown",
)


def classify_control(
    *,
    positions: Sequence[float],
    texts: Sequence[str],
    discrete: DiscreteAnalysis | None = None,
    mapping: MappingAnalysis | None = None,
    steps: StepSizes | None = None,
    resolution: dict[str, Any] | None = None,
) -> Classification:
    """Classify one control from the collected evidence."""
    reasons: list[str] = []
    alternatives: list[str] = []
    parsed = [parse_display(text) for text in texts]
    numbers = [item.value for item in parsed if item.value is not None]
    label_share = (sum(1 for item in parsed if not item.numeric) / len(parsed)) if parsed else 0.0
    distinct = len(set(texts))
    family = discrete.family if discrete is not None else "unknown"
    law = mapping.chosen.kind if (mapping is not None and mapping.chosen is not None) else None
    tolerance = (resolution or {}).get("tolerance")
    toggle = bool(steps.is_toggle) if steps is not None else False
    step_value = float(steps.step) if steps is not None and steps.reported else None
    integers_only = bool(numbers) and is_integer_valued(numbers)
    evidence: dict[str, Any] = {
        "host_is_toggle": toggle,
        "host_step_reported": bool(steps.reported) if steps is not None else False,
        "host_step": step_value,
        "distinct_texts": distinct,
        "samples": len(texts),
        "label_share": round(label_share, 4),
        "integers_only": integers_only,
        "fitted_law": law,
        "printed_tolerance": tolerance,
        "state_family": family,
    }

    def publish(kind: str, confidence: str, evidence_rows: dict[str, Any] | None = None) -> Classification:
        return Classification(
            kind=kind,
            confidence=confidence,
            reasons=reasons or ["no rule fired"],
            alternatives=[item for item in alternatives if item != kind],
            evidence={**evidence, **(evidence_rows or {})},
        )

    if not texts:
        reasons.append("no observation was recorded for this control")
        return publish("unknown", "none")

    # 1. boolean - the host declares a toggle, or exactly two labels were printed.
    if toggle and distinct <= 3:
        reasons.append("the host reports this parameter as a toggle (is_toggle)")
        if distinct == 2:
            reasons.append("exactly two distinct display values were sampled")
        return publish("boolean", "strong", {"band_source": "host_toggle"})
    if label_share > 0.0 and distinct == 2:
        # Two distinct labels are a two-state control *if* enough positions were
        # sampled to see the bands. With two samples they are also what the first two
        # members of a multi-member enumeration look like, so the weaker class is
        # published and the sample count is the recorded reason (found by the P26
        # independent review).
        if len(texts) >= 3:
            reasons.append("exactly two distinct non-numeric labels were sampled")
            if step_value == 1.0:
                reasons.append("the host reports step = 1.0, consistent with a two-state control")
            alternatives.append("enum")
            return publish("boolean", "medium")
        reasons.append(
            f"only {len(texts)} position(s) were sampled and they print two different labels: a two-state "
            "control and the first two members of an enumeration are indistinguishable from this evidence"
        )
        alternatives.append("boolean")
        return publish("enum", "weak", {"samples": len(texts)})

    # 2. enum - member labels, or an index pattern with no usable law.
    if label_share > 0.0 and distinct >= 2:
        reasons.append(
            f"{int(label_share * len(parsed))} of {len(parsed)} sampled positions printed a "
            "non-numeric label, so no numeric conversion shape can describe the display"
        )
        confidence = "strong" if (discrete is not None and discrete.members and any(m.interior_verified for m in discrete.members)) else "medium"
        if discrete is not None and discrete.members:
            evidence["bands"] = len(discrete.members)
            evidence["bands_verified"] = sum(1 for m in discrete.members if m.interior_verified)
            evidence["members_are_a_lower_bound"] = True
            reasons.append(
                f"{len(discrete.members)} member(s) were seen over {len(texts)} sampled positions: the "
                "count is a lower bound, not the enumeration's size"
            )
        if step_value is not None and step_value not in (0.0,):
            reasons.append(f"the host reports step = {step_value!r}")
        return publish("enum", confidence)
    if law is None and integers_only and numbers and max(abs(value) for value in numbers) <= 64.0 and distinct <= 8:
        # A numeric display is never *proof* of an enumeration: a control that
        # rounds, quantises or indexes all print integers. The class published is
        # therefore the weaker numeric one, with `enum` kept as an alternative -
        # the evidence to separate them is a labelled display (handled above) or a
        # marked write test (which this stage does not do).
        reasons.append(
            "the display prints a small set of integers and no conversion shape fits them: "
            "a coarsely quantised numeric display, or an enumeration that prints indices"
        )
        alternatives.append("enum")
        if step_value == 1.0:
            reasons.append("the host reports step = 1.0 (corroborating, not deciding)")
        return publish("stepped", "weak", {"requires": "setter_experiment"})
    if family == "integer_ladder":
        reasons.append("the sampled values are integers and follow a fitted shape")
        if step_value is not None:
            reasons.append(f"the host reports step = {step_value!r}")
        reasons.append("whether the plugin stores a fractional request is unmeasured")
        return publish("integer_like", "medium", {"requires": "setter_experiment"})

    # 3. numeric families.
    if law is not None:
        repeated = distinct < len(texts)
        if repeated:
            reasons.append(
                f"{len(texts) - distinct} sampled position(s) repeated the previous printed value "
                "while a conversion shape still fits: display rounding, not a discrete control"
            )
            alternatives.append("enum")
            return publish("stepped", "medium", {"law": law})
        if law in ("identity", "affine"):
            reasons.append(
                "the printed value is a faithful monotone ramp of the normalized position "
                f"(fitted {law!r} within {tolerance!r})"
            )
            return publish("ramp_like", "medium", {"law": law})
        reasons.append(f"a conversion shape fits the printed values on held-out points ({law!r})")
        if step_value not in (None, 0.0):
            reasons.append(f"the host reports step = {step_value!r}, which does not make the display discrete")
            alternatives.append("stepped")
        if integers_only:
            reasons.append("every sampled value prints as an integer")
            alternatives.append("integer_like")
        return publish("continuous", "medium", {"law": law})

    # 4. nothing usable.
    if discrete is not None and discrete.reason:
        reasons.append(f"the discrete analysis reported {discrete.reason!r}")
    for note in (discrete.notes if discrete is not None else [])[:2]:
        reasons.append(note)
    if not integers_only and distinct == len(texts):
        alternatives.append("continuous")
    return publish("unknown", "none")
