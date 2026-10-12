"""One run's observation store and the sampling engine (P26 §6, §8.2, §16).

Two rules the brief states explicitly are enforced *here*, by construction
rather than by convention:

- **one store per run**: the mapping analyzer and the discrete analyzer read the
  same cache, so a position observed by one stage is never re-sampled by another
  (a duplicated formatter call is a measurable defect, and `calls_by_stage`
  would hide it);
- **every sampling path is budgeted and terminating**: `observe` refuses past the
  per-control and total budgets with `budget_exhausted` instead of looping, and
  `refine` bisects until the bracket is one ULP wide or the step budget runs out,
  recording which of the two stopped it.

A store can also be *seeded* from recorded observations (`origin="recorded"`)
with no sampler at all - that is the offline replay path, and it is the same
store the analyzers see, so replay cannot silently take a different route.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from .text import parse_display
from .types import ControlObservations, Sample

__all__ = ["SamplingBudget", "ObservationStore", "grid_positions"]


def grid_positions(points: int, *, inclusive_ends: bool = True) -> list[float]:
    """A uniform normalized grid: ``points`` positions from 0 to 1."""
    if points < 2:
        raise ValueError("a sampling grid needs at least two points")
    step = 1.0 / (points - 1 if inclusive_ends else points)
    values = [round(index * step, 12) for index in range(points)]
    values[-1] = 1.0 if inclusive_ends else values[-1]
    return values


@dataclass
class SamplingBudget:
    """Declared limits. A limit that is hit produces an **incomplete** result,
    never a claim that everything was checked."""

    max_calls: int = 60_000
    max_calls_per_control: int = 4_000
    max_controls: int | None = None
    grid_points: int = 17
    #: Bisection stops at an adjacent-double bracket (the strongest statement the
    #: display channel can make) or when this many steps were spent - and it says
    #: which of the two stopped it.
    bisection_steps: int = 64
    min_bracket: float = 0.0
    adaptive: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_calls": int(self.max_calls),
            "max_calls_per_control": int(self.max_calls_per_control),
            "max_controls": self.max_controls,
            "grid_points": int(self.grid_points),
            "bisection_steps": int(self.bisection_steps),
            "min_bracket": float(self.min_bracket),
            "adaptive": bool(self.adaptive),
        }


class ObservationStore:
    """The single observation cache of one discovery run."""

    def __init__(
        self,
        *,
        sampler: Callable[[int, float], str | None] | None = None,
        budget: SamplingBudget | None = None,
        default_grid_points: int | None = None,
    ) -> None:
        self.sampler = sampler
        self.budget = budget or SamplingBudget()
        self.default_grid_points = int(default_grid_points or self.budget.grid_points)
        self._controls: dict[int, ControlObservations] = {}
        self._cache: dict[tuple[int, float], str] = {}
        self.calls_by_stage: dict[str, int] = {}
        self.calls_by_control: dict[int, int] = {}
        self.limits_hit: list[str] = []
        self.notes: list[str] = []

    # -- bookkeeping -------------------------------------------------------

    @property
    def total_calls(self) -> int:
        return int(sum(self.calls_by_stage.values()))

    def _bucket(self, stage: str) -> None:
        self.calls_by_stage[stage] = int(self.calls_by_stage.get(stage, 0)) + 1

    def _limit(self, label: str) -> None:
        if label not in self.limits_hit:
            self.limits_hit.append(label)

    def control_calls(self, index: int) -> int:
        return int(self.calls_by_control.get(index, 0))

    def budget_left(self, index: int | None = None) -> int:
        remaining = int(self.budget.max_calls) - self.total_calls
        if index is not None:
            remaining = min(remaining, int(self.budget.max_calls_per_control) - self.control_calls(index))
        return max(0, remaining)

    def _entry(self, index: int, *, name: str | None = None, ident: str | None = None) -> ControlObservations:
        entry = self._controls.get(index)
        if entry is None:
            entry = ControlObservations(index=index, name=name, ident=ident, status="no_samples")
            self._controls[index] = entry
        elif name is not None and entry.name is None:
            entry.name = name
        elif ident is not None and entry.ident is None:
            entry.ident = ident
        return entry

    # -- recording ---------------------------------------------------------

    def _key(self, index: int, normalized: float) -> tuple[int, float]:
        """The cache key: the **exact** double that was probed.

        Rounding the key (a tempting way to dedupe a grid) is a correctness bug, not
        an optimisation: two doubles that differ in the 13th decimal share a rounded
        key, so a bisection probe at ``0.375 - 1e-13`` is answered with the text of
        ``0.375``. The bracket bookkeeping then attributes the wrong text to a
        position and the search stops ~9000 ulps early while claiming an
        adjacent-double bracket. Measured on a synthetic threshold, which is why the
        key is exact and a regression test pins it.
        """
        return int(index), float(normalized)

    def record(
        self,
        index: int,
        normalized: float,
        text: Any,
        *,
        origin: str = "host",
        name: str | None = None,
        ident: str | None = None,
    ) -> None:
        """Record an observation that already exists (no host call, any origin)."""
        position = float(normalized)
        rendered = "" if text is None else str(text)
        entry = self._entry(index, name=name, ident=ident)
        key = self._key(index, position)
        if key in self._cache:
            return
        self._cache[key] = rendered
        entry.samples.append(Sample(normalized=position, text=rendered, origin=origin))
        entry.origin = origin if entry.origin == "host" else entry.origin
        if entry.status in ("no_samples", "error"):
            entry.status = "ok"

    def known(self, index: int, normalized: float) -> str | None:
        return self._cache.get(self._key(index, normalized))

    # -- sampling ----------------------------------------------------------

    def observe(
        self,
        index: int,
        normalized: float,
        *,
        stage: str,
        name: str | None = None,
        ident: str | None = None,
        allow_call: bool = True,
    ) -> str | None:
        """The text at a position, sampling only if it is not already known."""
        position = float(normalized)
        cached = self.known(index, position)
        if cached is not None:
            return cached
        if not allow_call or self.sampler is None:
            return None
        if self.budget_left(index) <= 0:
            label = (
                "max_calls_per_control"
                if self.control_calls(index) >= int(self.budget.max_calls_per_control)
                else "max_calls"
            )
            self._limit(label)
            return None
        text = self.sampler(int(index), position)
        self._bucket(stage)
        self.calls_by_control[int(index)] = self.control_calls(index) + 1
        entry = self._entry(index, name=name, ident=ident)
        if text is None:
            entry.status = "formatter_absent"
            entry.reason = "the host printed no text for this value"
            return None
        rendered = str(text)
        self._cache[self._key(index, position)] = rendered
        entry.samples.append(Sample(normalized=position, text=rendered, origin="host"))
        entry.calls += 1
        if entry.status == "no_samples":
            entry.status = "ok"
        return rendered

    def sweep(
        self,
        index: int,
        positions: Iterable[float],
        *,
        stage: str,
        name: str | None = None,
        ident: str | None = None,
    ) -> list[tuple[float, str]]:
        """Sample a list of positions, skipping ones already observed."""
        rows: list[tuple[float, str]] = []
        for position in positions:
            text = self.observe(index, position, stage=stage, name=name, ident=ident)
            if text is None:
                continue
            rows.append((float(position), text))
        return rows

    def refine(
        self,
        index: int,
        low: float,
        high: float,
        *,
        low_text: str,
        high_text: str,
        stage: str,
        name: str | None = None,
        ident: str | None = None,
    ) -> dict[str, Any]:
        """Bisect a text change between two positions that display differently.

        Returns a row describing the outcome. The bracket is reported as
        ``exact`` only when the two ends are adjacent doubles (no representable
        value lies between them), which is the strongest statement the host
        channel can make about a switch point.
        """
        row: dict[str, Any] = {
            "bracket": [float(low), float(high)],
            "low_text": low_text,
            "high_text": high_text,
            "state": "unresolved",
            "calls": 0,
            "steps": 0,
        }
        if not self.budget.adaptive:
            row["state"] = "not_attempted"
            row["reason"] = "adaptive refinement is off"
            return row
        left, right = float(low), float(high)
        left_text, right_text = low_text, high_text
        steps = 0
        while steps < int(self.budget.bisection_steps):
            if math.nextafter(left, right) >= right:
                row.update({"state": "exact", "position": math.nextafter(left, right)})
                break
            if right - left <= float(self.budget.min_bracket):
                row.update({"state": "bracketed", "reason": "bracket reached the declared minimum width"})
                break
            # ``left + (right - left) / 2`` rather than ``(left + right) / 2``: the
            # latter loses bits when the ends are close (the sum rounds away the
            # difference), so it stalls while the bracket is still tens of ulps wide
            # - and a stall must never be reported as an adjacent-double bracket.
            middle = left + (right - left) / 2.0
            if middle in (left, right):
                row.update(
                    {
                        "state": "bracketed",
                        "reason": "the midpoint no longer splits the bracket: the ends are as close as this arithmetic can measure",
                    }
                )
                break
            text = self.observe(index, middle, stage=stage, name=name, ident=ident)
            steps += 1
            if text is None:
                row.update({"state": "unresolved", "reason": "a probe returned no text"})
                break
            if text == left_text:
                left, left_text = middle, text
            elif text == right_text:
                right, right_text = middle, text
            else:
                row.update(
                    {
                        "state": "unresolved",
                        "reason": "a probe printed a third label inside the bracket",
                        "middle_text": text,
                        "middle": middle,
                    }
                )
                break
        else:
            row.update({"state": "bracketed", "reason": "bisection step budget exhausted"})
            self._limit("bisection_steps")
        row["calls"] = steps
        row["bracket"] = [left, right]
        if row["state"] in ("exact", "bracketed"):
            row.setdefault("position", right)
        return row

    # -- exports -----------------------------------------------------------

    def observations(self) -> list[ControlObservations]:
        return [self._controls[index] for index in sorted(self._controls)]

    def entry(self, index: int) -> ControlObservations | None:
        return self._controls.get(int(index))

    def numeric_fraction(self, index: int) -> float:
        entry = self._controls.get(int(index))
        if entry is None or not entry.samples:
            return 0.0
        numeric = sum(1 for sample in entry.samples if parse_display(sample.text).numeric)
        return numeric / len(entry.samples)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_calls": self.total_calls,
            "calls_by_stage": dict(self.calls_by_stage),
            "limits_hit": list(self.limits_hit),
            "budget": self.budget.to_dict(),
            "controls_observed": len(self._controls),
        }
