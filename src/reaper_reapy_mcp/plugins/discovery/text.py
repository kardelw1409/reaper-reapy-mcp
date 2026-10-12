"""Display-text parsing and printed precision (P26).

A host formatter prints a *string*; everything the engine can learn about a
control's value space starts by parsing that string honestly. The rules here are
deliberate about what is **not** claimed:

- a number with a trailing token is a number plus a *display* unit; whether that
  unit is the plugin's storage unit is a separate question the engine never
  answers from text alone;
- the printed string carries a finite resolution, and the engine computes it
  instead of assuming one. Two independent terms are combined: the printed
  decimal places, and half a float32 ulp at the largest magnitude printed (the
  P24 finding: plugins commonly print a float32 with ``%.6g``, so a "6 decimal
  places" reading overstates what the text can resolve at a steep end);
- text that is not a number is a *label*, not a failure: labels are the primary
  evidence that a control is an enumeration.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

__all__ = [
    "ParsedText",
    "DisplayResolution",
    "parse_display",
    "unit_token_of",
    "printed_resolution",
    "distinct_texts",
    "change_points",
    "is_integer_valued",
    "canonical_number",
]

# A leading optional-sign number: plain, exponent form, or thousands-separated.
# The thousands form requires a NON-ZERO leading group: `0,500` is how a
# comma-decimal locale prints one half, and reading it as five hundred is a
# 1000x error that no display could correct. The ambiguous string is refused
# (a label) instead of guessed - found by the P26 independent review.
_NUMBER_RE = re.compile(
    r"^(?P<number>[+-]?(?:[1-9]\d{0,2}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?)"
    r"\s*(?P<unit>.*)$"
)
# A ratio display: ``1/4``, ``3 / 16``, ``2/1``. Music and audio controls print
# sync ratios this way, and reading ``1/64`` as "the number 1, unit /64" both loses
# the value and turns a numeric ramp into a set of distinct labels (measured: it
# made a tempo-sync control classify as an enumeration).
_FRACTION_RE = re.compile(r"^([+-]?\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)$")
# A unit token: letters, %, degree, per, middle dot and spaces only.
_UNIT_RE = re.compile(r"^[A-Za-z%‰°·*/\s\-]*$")
_NONFINITE_RE = re.compile(r"^[+-]?(?:inf(?:inity)?|nan|∞)\b", re.IGNORECASE)
_DECIMALS_RE = re.compile(r"\.(\d+)")
_EXP_RE = re.compile(r"[eE]([+-]?\d+)")

#: Unicode characters that look like a sign or a space but are not ASCII.
_TRANSLATION = {
    ord("\u2212"): "-",  # minus sign
    ord("\u2013"): "-",  # en dash
    ord("\u00a0"): " ",  # non-breaking space
    ord("\u2009"): " ",  # thin space
    ord("\u202f"): " ",  # narrow no-break space
}


@dataclass
class ParsedText:
    """One display string, split into what it does and does not say."""

    text: str
    kind: str  # numeric | label | empty | nonfinite
    value: float | None = None
    unit_token: str | None = None
    number_text: str | None = None
    decimals: int | None = None
    exponent: int | None = None
    significant_digits: int | None = None
    form: str = "number"  # number | fraction
    ratio: tuple[float, float] | None = None

    @property
    def numeric(self) -> bool:
        return self.kind == "numeric"

    def to_dict(self) -> dict[str, Any]:
        row: dict[str, Any] = {"text": self.text, "kind": self.kind}
        if self.value is not None:
            row["value"] = self.value
        if self.unit_token:
            row["unit_token"] = self.unit_token
        if self.number_text is not None:
            row["number_text"] = self.number_text
        if self.form != "number":
            row["form"] = self.form
        return row


def parse_display(text: Any) -> ParsedText:
    """Parse a display string. Never raises: an odd string is a ``label``."""
    if text is None:
        return ParsedText(text="", kind="empty")
    raw = str(text)
    stripped = raw.translate(_TRANSLATION).strip()
    if not stripped:
        return ParsedText(text=raw, kind="empty")
    if _NONFINITE_RE.match(stripped):
        return ParsedText(text=raw, kind="nonfinite")

    fraction = _FRACTION_RE.match(stripped)
    if fraction is not None:
        numerator = float(fraction.group(1))
        denominator = float(fraction.group(2))
        if denominator == 0.0:
            return ParsedText(text=raw, kind="label")
        digits = len(re.sub(r"[^0-9]", "", fraction.group(1) + fraction.group(2)).lstrip("0")) or 1
        return ParsedText(
            text=raw,
            kind="numeric",
            value=numerator / denominator,
            unit_token=None,
            number_text=stripped,
            decimals=None,
            exponent=None,
            significant_digits=digits,
            form="fraction",
            ratio=(numerator, denominator),
        )

    # A leading sign followed by nothing numeric (e.g. "-") stays a label.
    match = _NUMBER_RE.match(stripped)
    if match is None:
        # Try a trailing number ("~50", "F#1 46.2"): the unit-side text must look
        # like a unit before this is accepted as a number, otherwise a product
        # name containing digits would be read as a parameter value.
        return ParsedText(text=raw, kind="label")
    number_text = match.group("number")
    unit_text = match.group("unit").strip()
    if unit_text and not _UNIT_RE.match(unit_text):
        return ParsedText(text=raw, kind="label")
    try:
        value = float(number_text.replace(",", ""))
    except ValueError:  # pragma: no cover - the regex already guarantees a float
        return ParsedText(text=raw, kind="label")
    if not math.isfinite(value):
        return ParsedText(text=raw, kind="nonfinite")
    decimals = None
    decimals_match = _DECIMALS_RE.search(number_text)
    if decimals_match:
        # An exponent moves the printed places left; the printed letters are what
        # the text can resolve, so the exponent is applied only to the magnitude
        # term (see `printed_resolution`).
        decimals = len(decimals_match.group(1))
    exponent_match = _EXP_RE.search(number_text)
    exponent = int(exponent_match.group(1)) if exponent_match else None
    digits = re.sub(r"[^0-9]", "", number_text.split("e")[0].split("E")[0]).lstrip("0")
    return ParsedText(
        text=raw,
        kind="numeric",
        value=value,
        unit_token=unit_text or None,
        number_text=number_text,
        decimals=decimals,
        exponent=exponent,
        significant_digits=len(digits) or 1,
    )


def canonical_number(value: float) -> str:
    """A stable text form for a float used in evidence and comparisons."""
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return repr(float(value))


def unit_token_of(texts: Iterable[str]) -> str | None:
    """The one trailing unit token shared by every numeric sample, else ``None``.

    Re-derived from the texts (the recorded artifacts also carry a claim; the
    engine compares the two rather than trusting the claim - P22's lesson about
    producer-asserted fields).
    """
    tokens: set[str | None] = set()
    saw_numeric = False
    for text in texts:
        parsed = parse_display(text)
        if not parsed.numeric:
            return None
        saw_numeric = True
        tokens.add(parsed.unit_token)
    if not saw_numeric or len(tokens) != 1:
        return None
    return tokens.pop()


@dataclass
class DisplayResolution:
    """What the printed text can and cannot resolve."""

    decimals: int | None = None
    decimal_term: float | None = None
    float32_term: float | None = None
    relative_term: float | None = None
    relative_mode: str = "absolute"
    tolerance: float | None = None
    max_magnitude: float | None = None
    state: str = "unknown"  # measured | unknown
    reason: str | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        row: dict[str, Any] = {"state": self.state, "relative_mode": self.relative_mode}
        for key in ("decimals", "decimal_term", "float32_term", "relative_term", "tolerance", "max_magnitude"):
            value = getattr(self, key)
            if value is not None:
                row[key] = value
        if self.reason:
            row["reason"] = self.reason
        if self.notes:
            row["notes"] = list(self.notes)
        return row

    @property
    def known(self) -> bool:
        return self.tolerance is not None and self.tolerance > 0.0

    def budget_for(self, value: float) -> float:
        """The slack allowed at one printed value, under this control's rule.

        A single absolute tolerance cannot describe a display that prints a ratio
        or spans orders of magnitude, so the rule is chosen once from the samples
        (`printed_resolution`) and applied per value here.
        """
        base = float(self.tolerance or 0.0)
        if self.relative_term is None:
            return base
        magnitude = float(self.relative_term) * abs(float(value))
        if self.relative_mode == "ratio":
            return magnitude if magnitude > 0.0 else base
        return max(base, magnitude)


def printed_resolution(values: Sequence[float], texts: Sequence[str]) -> DisplayResolution:
    """The display resolution implied by the samples themselves.

    ``tolerance`` is the absolute slack a model fit is allowed: half the finest
    printed place, floored by half a float32 ulp at the largest magnitude the
    samples print. A sample set that prints no decimals at all therefore gets a
    tolerance of 0.5 - a fit cannot be claimed better than the text can say.
    """
    numeric = [parse_display(text) for text in texts]
    if not numeric or any(not item.numeric for item in numeric):
        return DisplayResolution(state="unknown", reason="printed_precision_unknown")
    decimals_seen = [item.decimals for item in numeric if item.decimals is not None]
    decimals = max(decimals_seen) if decimals_seen else 0
    # An exponent shifts the printed places: `1.234e-05` resolves 1e-8, not 1e-3.
    exponents = [item.exponent for item in numeric if item.exponent is not None]
    shift = min(exponents) if exponents else 0
    ratios = [item for item in numeric if item.form == "fraction"]
    decimal_term = None if len(ratios) == len(numeric) else 10.0 ** (-(decimals - shift))
    significant = [item.significant_digits for item in numeric if item.significant_digits]
    relative_term = 0.5 * 10.0 ** (-(max(significant) - 1)) if significant else None
    magnitudes = [abs(float(value)) for value in values if value is not None]
    max_magnitude = max(magnitudes) if magnitudes else 0.0
    min_magnitude = min((value for value in magnitudes if value > 0.0), default=0.0)
    float32_term = 0.5 * _float32_ulp(max_magnitude)
    spans_orders = bool(min_magnitude) and (max_magnitude / max(min_magnitude, 1e-300)) > 100.0
    # Which rule applies is a *property of the display*, and it is recorded, because
    # the two rules are not interchangeable:
    #
    # ``ratio``      the samples print a ratio (`1/64`): the printed digits bound a
    #                *relative* error, and there is no decimal place to divide;
    # ``magnitude``  a numeric display that spans orders of magnitude: the absolute
    #                place is the floor and the relative term is the ceiling of what
    #                the text can resolve;
    # ``absolute``   an ordinary numeric display: only the printed place (and half a
    #                float32 ulp) has anything to say, and a relative term would
    #                silently loosen the budget.
    if ratios and len(ratios) == len(numeric):
        relative_mode = "ratio"
    elif relative_term and spans_orders:
        relative_mode = "magnitude"
    else:
        relative_mode = "absolute"
    if relative_mode == "ratio" and relative_term:
        tolerance = max(float32_term, relative_term * max_magnitude)
    else:
        tolerance = max(0.5 * (decimal_term or 0.0), float32_term)
    resolution = DisplayResolution(
        decimals=None if relative_mode == "ratio" else decimals,
        decimal_term=decimal_term,
        float32_term=float32_term,
        relative_term=relative_term if relative_mode != "absolute" else None,
        relative_mode=relative_mode,
        tolerance=tolerance,
        max_magnitude=max_magnitude,
        state="measured",
        notes=[
            "tolerance = max(half the finest printed place, half a float32 ulp at the largest printed magnitude)",
            f"budget rule: {relative_mode}",
        ],
    )
    if resolution.tolerance <= 0.0:  # pragma: no cover - defensive
        return DisplayResolution(state="unknown", reason="printed_precision_unknown")
    return resolution


def _float32_ulp(value: float) -> float:
    """The spacing of float32 values at ``value`` (0.0 for a zero magnitude)."""
    magnitude = abs(float(value))
    if magnitude == 0.0:
        return 0.0
    exponent = math.floor(math.log2(magnitude))
    return 2.0 ** (exponent - 23)


def distinct_texts(texts: Iterable[str]) -> list[str]:
    """Distinct display strings, in first-seen order (never sorted: the order a
    control walks its members in is itself evidence)."""
    seen: list[str] = []
    for text in texts:
        if text not in seen:
            seen.append(text)
    return seen


def change_points(values: Sequence[float], texts: Sequence[str]) -> list[dict[str, Any]]:
    """Positions where the printed text changes between adjacent samples."""
    rows: list[dict[str, Any]] = []
    for index in range(1, min(len(values), len(texts))):
        if texts[index] != texts[index - 1]:
            rows.append(
                {
                    "between": [float(values[index - 1]), float(values[index])],
                    "from_text": texts[index - 1],
                    "to_text": texts[index],
                }
            )
    return rows


def is_integer_valued(values: Sequence[float], *, tolerance: float = 1e-9) -> bool:
    """Whether every value is an integer (used for ladder/step candidates)."""
    return all(abs(float(value) - round(float(value))) <= tolerance for value in values if value is not None)
