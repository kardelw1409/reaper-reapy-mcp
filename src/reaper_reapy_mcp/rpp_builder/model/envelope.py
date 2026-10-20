"""Envelopes and automation items - MODEL ONLY until F5.

No reference with a track `VOLENV`/`PANENV` exists (RPP_FORMAT.md section 9.6),
so this module deliberately contains no writer. The types live here now so the
model is a superset of the format from the start (`PLAN` section 5, rule 1) and
F5 only adds grammar rows, not model churn.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EnvelopePoint:
    """One `PT` line: position, value and the shape tokens that follow."""

    pos_s: float
    value: float
    shape: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class Envelope:
    """A track or parameter envelope - unmeasured, not writable yet."""

    kind: str  # "volume" | "pan" | "parameter"
    points: tuple[EnvelopePoint, ...] = ()
    visible: bool = False
