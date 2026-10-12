"""Discrete-state analysis: enums, toggles, ladders and ramps (P26 §9).

The brief's traps are the specification here:

- **a small number of distinct strings is not proof of an enum** - a continuous
  control with a coarse printed resolution repeats its text. So a repeated
  numeric text whose values still follow a fitted law is classified as a
  *continuous* control with display rounding, not as an enumeration;
- **``step = 1`` is not proof of an enum** - the host reports it for toggles,
  for integers and for controls whose engine rounds; it is one signal among
  several and never decides alone;
- **a ramp is not an enum** - the P23/P24 finding: a control whose display is a
  faithful monotone ramp had been read as "257 members" because the *sweep's own
  grid* looked like members. The criterion (many distinct positions, faithful
  numbers, strictly monotone, one law fits the held-out points) is implemented
  once, here, and a banded set must fail it;
- **a boundary found on a sampled grid is an UPPER BOUND**, so the analysis
  records which positions are merely bracketed by the grid and refines by
  bisection only when a sampler is available and the budget allows.
"""

from __future__ import annotations

from typing import Any, Sequence

from .text import (
    change_points,
    distinct_texts,
    is_integer_valued,
    parse_display,
    printed_resolution,
)
from .types import DiscreteAnalysis, MappingAnalysis, StateBand, Transition

__all__ = [
    "DISCRETE_FAMILIES",
    "analyze_discrete",
    "sampled_ramp_evidence",
    "band_edges_from_grid",
]

#: Families this analyzer can publish.
DISCRETE_FAMILIES: tuple[str, ...] = ("enum", "boolean", "integer_ladder", "ramp", "continuous", "unknown")

_EMPTY_LABEL = ""


def band_edges_from_grid(values: Sequence[float], texts: Sequence[str]) -> list[dict[str, Any]]:
    """Bands implied by a sampled grid: each distinct text with the span of
    positions that printed it, plus the bracket where the text changed.

    The edges are the **midpoints between adjacent sampled positions**, so a
    boundary found this way is only known to lie inside the bracket; the row says
    so and the narrowing is the caller's job (see `analyze_discrete`).
    """
    if not values:
        return []
    bands: list[dict[str, Any]] = []
    current_text = texts[0]
    start = float(values[0])
    for index in range(1, len(values)):
        if texts[index] != current_text:
            previous = float(values[index - 1])
            following = float(values[index])
            midpoint = (previous + following) / 2.0
            bands.append(
                {
                    "label": current_text,
                    "lower": start,
                    "upper": midpoint,
                    "bracket": [previous, following],
                    "sampled_from": start,
                    "sampled_to": previous,
                }
            )
            current_text = texts[index]
            start = float(values[index])
    bands.append(
        {
            "label": current_text,
            "lower": start,
            "upper": 1.0,
            "bracket": None,
            "sampled_from": start,
            "sampled_to": float(values[-1]),
        }
    )
    if bands:
        bands[0]["lower"] = 0.0
    return bands


def sampled_ramp_evidence(
    values: Sequence[float],
    texts: Sequence[str],
    *,
    mapping: MappingAnalysis | None,
    resolution_tolerance: float | None,
    min_distinct_share: float = 0.5,
) -> dict[str, Any]:
    """The criterion that keeps a faithful numeric ramp out of the enum family.

    Every clause is required; each one is a *reason* in the returned row, so a
    rejection can be reported instead of silently overruling the family.
    """
    parsed = [parse_display(text) for text in texts]
    reasons: list[str] = []
    numeric = all(item.numeric for item in parsed)
    if not numeric:
        reasons.append("some sampled text is not a number")
    numbers = [item.value for item in parsed if item.value is not None]
    distinct = distinct_texts(texts)
    share = (len(distinct) / len(texts)) if texts else 0.0
    if share < min_distinct_share:
        reasons.append(f"only {share:.2f} of the sampled positions printed a new value")
    monotone = bool(numbers) and (
        all(numbers[i + 1] >= numbers[i] for i in range(len(numbers) - 1))
        or all(numbers[i + 1] <= numbers[i] for i in range(len(numbers) - 1))
    )
    if not monotone:
        reasons.append("the printed numbers are not monotone in the position")
    law_ok = bool(mapping is not None and mapping.chosen is not None)
    if not law_ok:
        reasons.append("no conversion shape fits within the printed resolution")
    elif resolution_tolerance is not None and mapping is not None and mapping.chosen is not None:
        worst = mapping.chosen.test_error if mapping.chosen.test_error is not None else mapping.chosen.train_error
        if worst is not None and worst > resolution_tolerance:
            reasons.append("the fitted shape misses the held-out points outside the printed resolution")
            law_ok = False
    integers_only = bool(numbers) and is_integer_valued(numbers)
    if integers_only and numbers and max(abs(value) for value in numbers) <= 64.0:
        # A small closed set of integers is the *index* pattern an enum displays.
        reasons.append("every printed value is a small integer (an index pattern, not a ramp)")
    return {
        "clauses": {
            "numeric": numeric,
            "distinct_share": share,
            "monotone": monotone,
            "law_fits": law_ok,
            "small_integers_only": integers_only and bool(numbers) and max(abs(value) for value in numbers) <= 64.0,
        },
        "is_ramp": not reasons,
        "reasons": reasons,
    }


def analyze_discrete(
    *,
    index: int,
    positions: Sequence[float],
    texts: Sequence[str],
    mapping: MappingAnalysis | None = None,
    resolution: dict[str, Any] | None = None,
    is_toggle: bool = False,
    host_step: float | None = None,
    store: Any = None,
    name: str | None = None,
    ident: str | None = None,
    refine_budget: int = 0,
) -> DiscreteAnalysis:
    """Classify a control's value structure and (when possible) measure its bands."""
    analysis = DiscreteAnalysis()
    if not texts:
        analysis.family = "unknown"
        analysis.state = "undetermined"
        analysis.reason = "no_observations"
        return analysis
    pairs = sorted(zip((float(position) for position in positions), texts), key=lambda item: item[0])
    values = [pair[0] for pair in pairs]
    ordered_texts = [pair[1] for pair in pairs]
    distinct = distinct_texts(ordered_texts)
    analysis.distinct_count = len(distinct)
    parsed = [parse_display(text) for text in ordered_texts]
    label_share = sum(1 for item in parsed if not item.numeric) / len(parsed)
    tolerance = None
    if resolution:
        tolerance = resolution.get("tolerance")
    if tolerance is None:
        numbers = [item.value for item in parsed if item.value is not None]
        tolerance = printed_resolution(numbers, [text for text in ordered_texts if parse_display(text).numeric]).tolerance

    ramp = sampled_ramp_evidence(values, ordered_texts, mapping=mapping, resolution_tolerance=tolerance)
    unlabelled = [text for text in distinct if text == _EMPTY_LABEL]

    if is_toggle and label_share > 0.0 and len(distinct) <= 3:
        analysis.family = "boolean"
        analysis.state = "sampled_upper_bounds"
        analysis.notes.append("the host reports this parameter as a toggle")
    elif label_share > 0.0:
        analysis.family = "enum"
        analysis.state = "sampled_upper_bounds"
    elif ramp["is_ramp"]:
        numbers = [item.value for item in parsed if item.value is not None]
        integers_only = is_integer_valued(numbers)
        # A ladder is only published when *independent* evidence says the value is
        # quantised. Printing integers is not that evidence: P24 measured that
        # "the samples print integers" describes the sweep, not the display's
        # resolution, and a faithful ramp that happens to print integers would be
        # misread as a member list.
        ladder_evidence = bool(
            host_step is not None and host_step >= 1.0 and integers_only and len(distinct) == len(ordered_texts)
        )
        if ladder_evidence:
            analysis.family = "integer_ladder"
            analysis.state = "sampled_upper_bounds"
            analysis.notes.append(
                f"the host reports a step of {host_step!r} and every sampled value prints as an integer"
            )
        elif len(distinct) < len(ordered_texts):
            analysis.family = "continuous"
            analysis.state = "no_band_structure"
            analysis.notes.append(
                "the printed text repeats over part of the range but its values follow a fitted law: "
                "display rounding, not an enumeration"
            )
            if integers_only:
                analysis.notes.append(
                    "every sampled value prints as an integer; whether the plugin stores a fractional "
                    "request is unmeasured and needs a marked setter experiment"
                )
                analysis.reason = "requires_setter_experiment"
        else:
            analysis.family = "continuous"
            analysis.state = "no_band_structure"
            analysis.notes.append("a faithful monotone numeric ramp")
            if integers_only:
                analysis.notes.append(
                    "every sampled value prints as an integer; whether the plugin stores a fractional "
                    "request is unmeasured and needs a marked setter experiment"
                )
                analysis.reason = "requires_setter_experiment"
    elif len(distinct) <= 2 and not label_share:
        analysis.family = "boolean"
        analysis.state = "sampled_upper_bounds"
        analysis.notes.append("exactly two distinct printed values")
    elif label_share == 0.0 and len(distinct) == len(ordered_texts):
        analysis.family = "continuous"
        analysis.state = "no_band_structure"
    else:
        analysis.family = "unknown"
        analysis.state = "undetermined"
        analysis.reason = "ambiguous_model"
        analysis.notes.extend(ramp["reasons"])

    if analysis.family in ("enum", "boolean") or analysis.family == "integer_ladder":
        bands = band_edges_from_grid(values, ordered_texts)
        analysis.members = [
            StateBand(
                ordinal=ordinal,
                label=band["label"],
                lower=float(band["lower"]),
                upper=float(band["upper"]),
                bracket=(
                    [float(band["bracket"][0]), float(band["bracket"][1])] if band["bracket"] else None
                ),
                unlabelled=band["label"] == _EMPTY_LABEL,
                note=(
                    "the upper edge is bracketed by two sampled positions; the switch point lies "
                    "between them"
                    if band["bracket"]
                    else None
                ),
            )
            for ordinal, band in enumerate(bands)
        ]
        analysis.unlabelled = unlabelled
        transitions: list[Transition] = []
        for ordinal, band in enumerate(bands[1:], start=1):
            previous = bands[ordinal - 1]
            bracket = previous["bracket"]
            transition = Transition(
                ordinal=ordinal,
                lower_label=previous["label"],
                upper_label=band["label"],
                bracket=[float(bracket[0]), float(bracket[1])] if bracket else None,
                state="sampled_upper_bound",
                reason="a sampled grid brackets the switch point; it is not the measured boundary",
            )
            transitions.append(transition)
        analysis.transitions = transitions
        analysis.calls = 0
        if store is not None and refine_budget > 0:
            _refine_transitions(
                analysis,
                store=store,
                index=index,
                name=name,
                ident=ident,
                budget=refine_budget,
            )
        if analysis.family == "boolean" and len(distinct) > 2:
            analysis.notes.append("the host's toggle flag disagrees with the printed value count")
        if analysis.budget_exhausted:
            analysis.state = "budget_exhausted"
    return analysis


def numbered_distinct(values: Sequence[float], texts: Sequence[str]) -> list[str]:
    """Distinct numeric texts (the ladder criterion needs the numeric subset)."""
    seen: list[str] = []
    for text in texts:
        parsed = parse_display(text)
        if not parsed.numeric:
            continue
        if text not in seen:
            seen.append(text)
    return seen


def _carry_bracket(analysis: DiscreteAnalysis, ordinal: int, bracket: list[float] | None) -> None:
    """Copy a narrowed transition bracket onto the band whose upper edge it is.

    Transition ``ordinal`` leaves band ``ordinal - 1``, so that is the band that
    carries the bracket. After a measurement the band carries a *measured* bracket,
    which makes the boundary comparison two measurements rather than a measurement
    against a grid guess.
    """
    if bracket is None or int(ordinal) < 1:
        return
    for member in analysis.members:
        if member.ordinal == int(ordinal) - 1:
            member.bracket = [float(bracket[0]), float(bracket[1])]
            return


def _refine_transitions(
    analysis: DiscreteAnalysis,
    *,
    store: Any,
    index: int,
    name: str | None,
    ident: str | None,
    budget: int,
) -> None:
    """Narrow each grid bracket by bisection, spending at most ``budget`` calls.

    A transition the run could not narrow keeps its grid bracket and says so; the
    analysis is marked ``budget_exhausted`` (incomplete) instead of implying every
    boundary was measured.
    """
    spent = 0
    for transition in analysis.transitions:
        if spent >= budget:
            transition.state = "not_attempted"
            transition.reason = "refinement budget exhausted"
            analysis.budget_exhausted = True
            continue
        if not transition.bracket:
            continue
        low, high = transition.bracket
        low_text, high_text = transition.lower_label, transition.upper_label
        outcome = store.refine(
            index,
            low,
            high,
            low_text=low_text,
            high_text=high_text,
            stage="discrete_refine",
            name=name,
            ident=ident,
        )
        spent += int(outcome.get("calls") or 0)
        transition.calls = int(outcome.get("calls") or 0)
        state = str(outcome.get("state"))
        if state == "exact":
            transition.state = "exact"
            transition.position = float(outcome["position"])
            transition.bracket = [float(item) for item in outcome["bracket"]]
            transition.reason = None
            _carry_bracket(analysis, transition.ordinal, transition.bracket)
        elif state == "bracketed":
            transition.state = "bracketed"
            transition.position = float(outcome.get("position"))
            transition.bracket = [float(item) for item in outcome["bracket"]]
            transition.reason = str(outcome.get("reason") or "")
            _carry_bracket(analysis, transition.ordinal, transition.bracket)
        else:
            transition.state = "unresolved"
            transition.reason = str(outcome.get("reason") or "bisection could not narrow the bracket")
    # Interior verification: one probe inside each band that has room for it.
    for ordinal, band in enumerate(analysis.members):
        if spent >= budget:
            analysis.budget_exhausted = True
            break
        if band.upper - band.lower <= 1e-9:
            continue
        probe = band.lower + (band.upper - band.lower) * 0.5
        text = store.observe(index, probe, stage="discrete_band_interior", name=name, ident=ident)
        spent += 1
        if text is None:
            band.note = "band interior not verified (no text)"
            continue
        if text == band.label:
            band.interior_verified = True
        else:
            band.interior_verified = False
            band.note = f"a probe inside this band printed {text!r} instead of {band.label!r}"
    analysis.calls = spent
    if analysis.budget_exhausted:
        analysis.state = "budget_exhausted"
    else:
        analysis.state = "measured"
        if any(item.state != "exact" for item in analysis.transitions):
            analysis.notes.append("some transitions are bracketed rather than exact")
