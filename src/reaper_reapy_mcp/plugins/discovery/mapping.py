"""Range and conversion-shape analysis (P26 §8).

The input is what the host's formatter printed at known normalized positions;
the output is (a) a set of **competing** model fits with their errors on points
they never saw and (b) a range resolution that obeys the precedence rules of
`evidence.resolve_range`.

Design decisions worth stating, because they are what keeps the result honest:

- **nothing is accepted from visual similarity.** A candidate model is accepted
  only when its error on the held-out points is inside the resolution the printed
  text itself can resolve; the resolution is computed from the samples
  (`text.printed_resolution`), not assumed.
- **complexity is a tie-break, not a score.** When a simple and a complex model
  both fit inside the resolution, the simpler one is chosen and the complex one
  stays in the artifact as a competing fit.
- **a fitted display law is not a write law.** The fit describes how the plugin
  *prints*; the profile's conversion laws live in the plugin's own value space.
  The engine therefore labels every fit with the space it was measured in and maps
  to a profile law kind only where the mapping is exact - a display law of a kind
  the profile vocabulary does not have stays unmapped rather than approximated.
- **a truncated range is recorded as a conflict.** The precedence rule never lets
  a weak source narrow a strong one; a narrow display reading of a wider declared
  range is a conflict row, not a new bound.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from .evidence import Claim, resolve_range, strength_for
from .text import parse_display, printed_resolution
from .types import MappingAnalysis, MappingFit, RangeAnalysis

__all__ = [
    "MODEL_KINDS",
    "fit_mapping",
    "analyze_range",
    "numeric_samples",
    "display_bounds",
]

#: Candidate shapes, cheapest first. The order is the tie-break order.
MODEL_KINDS: tuple[str, ...] = (
    "identity",
    "affine",
    "power",
    "exponential",
    "logarithmic",
    "piecewise_affine",
    "table",
)

#: Fit kinds the profile's own law vocabulary can express exactly.
_PROFILE_LAW_KINDS = {
    "identity": "identity",
    "affine": "affine",
    "power": "power",
    "exponential": "exponential",
}

_COMPLEXITY = {
    "identity": 0,
    "affine": 2,
    "power": 3,
    "exponential": 3,
    "logarithmic": 3,
    "piecewise_affine": 5,
    "table": 99,
}

_POWER_GRID = tuple(round(0.05 * (1.18**index), 6) for index in range(60))  # 0.05 .. ~700
_EXP_GRID = tuple(round(-12.0 + 0.05 * index, 6) for index in range(481))  # -12 .. +12
_LOG_SHIFT_GRID = tuple(round(10.0 ** (-6.0 + 0.25 * index), 12) for index in range(25))  # 1e-6 .. 1


@dataclass
class _Point:
    t: float
    y: float


def numeric_samples(positions: Sequence[float], texts: Sequence[str]) -> list[_Point]:
    """The usable (position, printed value) pairs; non-numeric texts are dropped."""
    points: list[_Point] = []
    for position, text in zip(positions, texts):
        parsed = parse_display(text)
        if parsed.numeric and parsed.value is not None:
            points.append(_Point(t=float(position), y=float(parsed.value)))
    return sorted(points, key=lambda point: point.t)


def display_bounds(positions: Sequence[float], texts: Sequence[str]) -> tuple[float, float] | None:
    """The numeric extremes the *display* walked, in display space."""
    points = numeric_samples(positions, texts)
    if not points:
        return None
    values = [point.y for point in points]
    return min(values), max(values)


def _split(points: Sequence[_Point], *, holdout: int = 2) -> tuple[list[_Point], list[_Point]]:
    """Deterministic train/test split: every k-th interior point is held out."""
    if len(points) < 4:
        return list(points), []
    stride = max(2, round(len(points) / max(1, holdout)))
    train: list[_Point] = [points[0], points[-1]]
    test: list[_Point] = []
    for index, point in enumerate(points[1:-1], start=1):
        if index % stride == 0:
            test.append(point)
        else:
            train.append(point)
    return sorted(train, key=lambda item: item.t), sorted(test, key=lambda item: item.t)


def _least_squares(basis: Sequence[Sequence[float]], values: Sequence[float]) -> list[float] | None:
    """Solve the 2-parameter normal equations for a basis of two functions."""
    s11 = s12 = s22 = b1 = b2 = 0.0
    for row, value in zip(basis, values):
        phi1, phi2 = row
        s11 += phi1 * phi1
        s12 += phi1 * phi2
        s22 += phi2 * phi2
        b1 += phi1 * value
        b2 += phi2 * value
    determinant = s11 * s22 - s12 * s12
    if abs(determinant) < 1e-18:
        return None
    a = (b1 * s22 - b2 * s12) / determinant
    b = (s11 * b2 - s12 * b1) / determinant
    if not (math.isfinite(a) and math.isfinite(b)):
        return None
    return [a, b]


def _fit_shape(points: Sequence[_Point], shape: Callable[[float], float]) -> tuple[Callable[[float], float], dict[str, Any]] | None:
    """Fit ``y = a * shape(t) + b`` by least squares; the shape is fixed here."""
    basis = [[shape(point.t), 1.0] for point in points]
    solution = _least_squares(basis, [point.y for point in points])
    if solution is None:
        return None
    a, b = solution
    return (lambda t: a * shape(t) + b), {"scale": a, "offset": b}


def _ternary_minimise(
    evaluate: Callable[[float], float],
    low: float,
    high: float,
    *,
    current: float,
    current_error: float,
    iterations: int = 90,
) -> tuple[float, float]:
    """Local minimisation of a unimodal 1-D error between two grid points.

    A shape family whose parameter is only identified to a grid ratio cannot be
    fitted to the precision a printed display resolves; this turns the grid hit
    into a fit. The incumbent is kept, so a refinement can never make the answer
    worse than the scan that found it.
    """
    best_parameter, best_error = current, current_error
    for _ in range(iterations):
        if not high > low:
            break
        third = (high - low) / 3.0
        left = low + third
        right = high - third
        left_error = evaluate(left)
        right_error = evaluate(right)
        if left_error < best_error:
            best_parameter, best_error = left, left_error
        if right_error < best_error:
            best_parameter, best_error = right, right_error
        if left_error <= right_error:
            high = right
        else:
            low = left
    return best_parameter, best_error


def _errors(
    model: Callable[[float], float],
    points: Sequence[_Point],
    *,
    budget: Callable[[float], float] | None = None,
) -> tuple[float, float, float | None] | None:
    """``(mean absolute error, worst absolute error, worst error / budget)``.

    The third figure is what acceptance is judged on when a budget is available:
    a display that spans orders of magnitude cannot be judged by one absolute
    tolerance, and the printed digits say how much slack each value deserves.
    """
    if not points:
        return None
    residuals: list[float] = []
    worst_ratio: float | None = None
    for point in points:
        try:
            predicted = model(point.t)
        except (ValueError, OverflowError, ZeroDivisionError):
            return None
        if not math.isfinite(predicted):
            return None
        error = abs(predicted - point.y)
        residuals.append(error)
        if budget is not None:
            allowance = budget(point.y)
            if allowance > 0.0:
                ratio = error / allowance
                worst_ratio = ratio if worst_ratio is None else max(worst_ratio, ratio)
    return sum(residuals) / len(residuals), max(residuals), worst_ratio


def _monotone(values: Sequence[float], *, tolerance: float) -> bool:
    rising = all(values[i + 1] >= values[i] - tolerance for i in range(len(values) - 1))
    falling = all(values[i + 1] <= values[i] + tolerance for i in range(len(values) - 1))
    return bool(rising or falling)


def fit_mapping(
    positions: Sequence[float],
    texts: Sequence[str],
    *,
    tolerance: float | None = None,
    resolution: dict[str, Any] | None = None,
) -> MappingAnalysis:
    """Fit the candidate display laws and choose one (or none)."""
    points = numeric_samples(positions, texts)
    resolution_row = dict(resolution or {})
    if tolerance is None:
        computed = printed_resolution([point.y for point in points], [text for text in texts if parse_display(text).numeric])
        resolution_row = computed.to_dict()
        tolerance = computed.tolerance
    analysis = MappingAnalysis(resolution=resolution_row)
    if len(points) < 3:
        analysis.reason = "insufficient_points"
        analysis.notes.append("fewer than three numeric samples; no shape can be tested on held-out points")
        return analysis
    values = [point.y for point in points]
    if max(values) - min(values) <= (tolerance or 0.0):
        analysis.reason = "formatter_constant"
        analysis.notes.append("every sampled value prints the same number")
        return analysis
    ascending = values[-1] > values[0]
    analysis.ascending = ascending
    train, test = _split(points)
    monotone = _monotone(values, tolerance=float(tolerance or 0.0) / 2.0)
    budget = _budget_from(resolution_row)
    candidates: list[tuple[str, Callable[[float], float], dict[str, Any]]] = []

    candidates.append(("identity", lambda t: t, {}))
    affine = _fit_shape(train, lambda t: t)
    if affine:
        candidates.append(("affine", affine[0], affine[1]))

    def _scan_kind(kind: str, shape_of: Callable[[float], Callable[[float], float]], grid: Sequence[float], label: str) -> None:
        """Scan the shape parameter, then refine it locally.

        The grid is deliberately coarse (a 1.18 ratio) because its job is to
        *identify* a family, not to fit it: a parameter 5% off leaves a residual far
        larger than the few parts per million a printed display can resolve, so the
        fit would be rejected and a lookup table would win - a shape the control
        really has, replaced by a table it did not need. The ternary refinement
        inside the winning grid bracket is what makes the fit as good as the shape
        allows.
        """
        def error_of(parameter: float) -> float:
            fitted = _fit_shape(train, shape_of(parameter))
            if fitted is None:
                return math.inf
            errors = _errors(fitted[0], train)
            if errors is None:
                return math.inf
            return errors[0]

        best_parameter: float | None = None
        best_error = math.inf
        best_index = 0
        for index, parameter in enumerate(grid):
            error = error_of(parameter)
            if error < best_error:
                best_parameter, best_error, best_index = parameter, error, index
        if best_parameter is None:
            return
        low = grid[max(0, best_index - 1)]
        high = grid[min(len(grid) - 1, best_index + 1)]
        if high > low:
            best_parameter, best_error = _ternary_minimise(
                error_of, low, high, current=best_parameter, current_error=best_error
            )
        fitted = _fit_shape(train, shape_of(best_parameter))
        if fitted is None:
            return
        params = dict(fitted[1])
        params[label] = best_parameter
        candidates.append((kind, fitted[0], params))
    _scan_kind("power", lambda p: (lambda t: t**p if t > 0 else 0.0), _POWER_GRID, "exponent")
    _scan_kind("exponential", lambda k: (lambda t: math.exp(k * t)), _EXP_GRID, "rate")
    _scan_kind("logarithmic", lambda c: (lambda t: math.log(t + c)), _LOG_SHIFT_GRID, "shift")

    # Piecewise: one kink, each side affine. The kink is only chosen at an
    # observed position - an invented breakpoint would be a guess.
    if len(points) >= 6 and monotone:
        best_piece: tuple[float, dict[str, Any]] | None = None
        for split in range(2, len(points) - 2):
            left = [point for point in points if point.t <= points[split].t]
            right = [point for point in points if point.t >= points[split].t]
            left_fit = _fit_shape(left, lambda t: t)
            right_fit = _fit_shape(right, lambda t: t)
            if not left_fit or not right_fit:
                continue
            errors = []
            for group, fit in ((left, left_fit), (right, right_fit)):
                group_errors = _errors(fit[0], group)
                if group_errors is None:
                    errors = []
                    break
                errors.append(group_errors[0])
            if not errors:
                continue
            score = sum(errors) / len(errors)
            if best_piece is None or score < best_piece[0]:
                best_piece = (
                    score,
                    {
                        "breakpoint": float(points[split].t),
                        "left": left_fit[1],
                        "right": right_fit[1],
                    },
                )
        if best_piece is not None:
            breakpoint = best_piece[1]["breakpoint"]
            left_params = best_piece[1]["left"]
            right_params = best_piece[1]["right"]

            def piecewise(t: float) -> float:
                params = left_params if t <= breakpoint else right_params
                return params["scale"] * t + params["offset"]

            candidates.append(("piecewise_affine", piecewise, best_piece[1]))

    # Table: exact samples, usable only as a lookup (highest complexity).
    table_points = list(points)

    def table(t: float) -> float:
        if t <= table_points[0].t:
            return table_points[0].y
        if t >= table_points[-1].t:
            return table_points[-1].y
        for index in range(1, len(table_points)):
            if table_points[index].t >= t:
                before, after = table_points[index - 1], table_points[index]
                span = after.t - before.t
                if span <= 0:
                    return after.y
                share = (t - before.t) / span
                return before.y + share * (after.y - before.y)
        return table_points[-1].y  # pragma: no cover - guarded above

    candidates.append(("table", table, {"points": len(table_points)}))

    fits: list[MappingFit] = []
    for kind, model, params in candidates:
        train_errors = _errors(model, train)
        test_errors = _errors(model, test, budget=budget)
        if train_errors is None:
            continue
        fit = MappingFit(
            kind=kind,
            params={key: value for key, value in params.items()},
            train_error=train_errors[0],
            test_error=(test_errors[0] if test_errors else None),
            max_error=max((test_errors[1] if test_errors else train_errors[1]), train_errors[1]),
            n_train=len(train),
            n_test=len(test),
            monotone=monotone,
            complexity=_COMPLEXITY.get(kind, 10),
            profile_law_kind=_PROFILE_LAW_KINDS.get(kind),
            budget_ratio=(test_errors[2] if test_errors else None),
        )
        fits.append(fit)

    tolerance_value = float(tolerance or 0.0)
    if tolerance_value <= 0.0:
        analysis.fits = fits
        analysis.reason = "printed_precision_unknown"
        return analysis
    # Acceptance: the error on points the fit never saw must sit inside what the
    # printed text can resolve, and a conversion law has to be invertible.
    def _within(fit: MappingFit) -> bool:
        if fit.monotone is False:
            return False
        if fit.n_test > 0:
            if fit.budget_ratio is not None:
                return fit.budget_ratio <= 1.0
            return fit.test_error is not None and fit.test_error <= tolerance_value
        error = fit.train_error if fit.train_error is not None else fit.test_error
        return error is not None and error <= tolerance_value

    usable = [fit for fit in fits if _within(fit)]
    for fit in usable:
        fit.accepted = True
    if not usable:
        analysis.fits = fits
        analysis.reason = "model_unstable"
        if not monotone:
            analysis.notes.append("the printed values are not monotone in the position, so no law can be inverted")
        else:
            best = min(
                (fit.test_error if fit.test_error is not None else fit.train_error or math.inf) for fit in fits
            )
            analysis.notes.append(
                f"the best candidate still misses by {best!r}, outside the printed tolerance {tolerance_value!r}"
            )
        return analysis
    usable.sort(
        key=lambda fit: (
            fit.complexity,
            (fit.test_error if fit.test_error is not None else fit.train_error) or 0.0,
        )
    )
    chosen = usable[0]
    analysis.fits = fits
    analysis.chosen = chosen
    analysis.notes.append(
        f"chosen {chosen.kind!r}: worst error on {chosen.n_test} held-out point(s) = "
        f"{chosen.test_error!r} within printed tolerance {tolerance_value!r}"
    )
    if chosen.budget_ratio is not None:
        analysis.notes.append(f"worst error as a share of the printed budget: {chosen.budget_ratio:.3f}")
    if len(usable) > 1:
        analysis.notes.append(
            "competing admissible fits: " + ", ".join(f"{fit.kind}({fit.complexity})" for fit in usable[1:])
        )
    return analysis


def _budget_from(resolution: Mapping[str, Any]) -> Callable[[float], float] | None:
    """A per-value allowance from a recorded resolution row, if it is usable."""
    if not isinstance(resolution, Mapping):
        return None
    absolute = resolution.get("tolerance")
    relative = resolution.get("relative_term")
    mode = str(resolution.get("relative_mode") or "absolute")
    if not isinstance(absolute, (int, float)) or float(absolute) <= 0.0:
        return None
    if isinstance(relative, (int, float)) and float(relative) > 0.0 and mode != "absolute":
        if mode == "ratio":
            def ratio_budget(value: float) -> float:
                magnitude = float(relative) * abs(float(value))
                return magnitude if magnitude > 0.0 else float(absolute)

            return ratio_budget

        def magnitude_budget(value: float) -> float:
            return max(float(absolute), float(relative) * abs(float(value)))

        return magnitude_budget

    def flat_budget(value: float) -> float:
        return float(absolute)

    return flat_budget


def analyze_range(
    *,
    normalized_range: tuple[float, float] = (0.0, 1.0),
    declared_native: tuple[float, float] | None = None,
    display: tuple[float, float] | None = None,
    sampled_display: tuple[float, float] | None = None,
    native_metadata: tuple[float, float] | None = None,
    accepted: tuple[float, float] | None = None,
    unit: str | None = None,
) -> RangeAnalysis:
    """Resolve the range claims of one control under the precedence rules."""
    claims: list[Claim] = []
    if accepted is not None:
        claims.append(
            Claim(
                value=list(accepted),
                source_kind="accepted",
                source="setter/storage experiment",
                strength=strength_for("accepted"),
                detail={"space": "accepted", "note": "the host stored and read back values in this interval"},
            )
        )
    if native_metadata is not None:
        claims.append(
            Claim(
                value=list(native_metadata),
                source_kind="declared",
                source="format adapter native metadata",
                strength=strength_for("declared"),
                detail={"space": "native"},
            )
        )
    if declared_native is not None:
        claims.append(
            Claim(
                value=list(declared_native),
                source_kind="declared",
                source="host parameter range",
                strength=strength_for("declared"),
                detail={"space": "native"},
            )
        )
    if display is not None:
        claims.append(
            Claim(
                value=list(display),
                source_kind="measured",
                source="host formatter over the sampled grid",
                strength=strength_for("measured", checked=True),
                detail={"space": "display", "unit": unit},
            )
        )
    if sampled_display is not None:
        claims.append(
            Claim(
                value=list(sampled_display),
                source_kind="derived",
                source="observed control positions in this run",
                strength=strength_for("derived"),
                detail={"space": "display", "note": "a sample of where the control was seen, not a bound"},
            )
        )
    # The host's normalized 0..1 addressing space is a fact about *addressing*, not
    # a claim about the parameter's value range, so it is recorded but never
    # competes: letting it participate would outrank every measured display range
    # (it is a host declaration) and silently publish 0..1 as the value range.
    addressing = Claim(
        value=list(normalized_range),
        source_kind="declared",
        source="host normalized space",
        strength=strength_for("declared"),
        detail={"space": "normalized", "note": "every host parameter is addressed in 0..1"},
    )
    analysis = RangeAnalysis(claims=[claim.to_dict() for claim in claims] + [addressing.to_dict()])
    resolved, conflict = resolve_range("range", claims)
    if conflict is not None:
        analysis.conflict = conflict
    if resolved is None:
        analysis.reason = "api_does_not_expose"
        analysis.notes.append("no source answered a value range for this control")
        return analysis
    low, high = resolved
    analysis.chosen = [low, high]
    strongest = None
    for claim in claims:
        value = claim.value
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            continue
        if abs(float(value[0]) - low) <= 1e-12 and abs(float(value[1]) - high) <= 1e-12:
            strongest = claim
            break
    analysis.space = str((strongest.detail or {}).get("space")) if strongest is not None else None
    if analysis.space == "display":
        analysis.notes.append(
            "the bounds are the extremes the host's formatter printed; they are a display-space range, "
            "not proof of the interval the plugin stores values in"
        )
    return analysis
