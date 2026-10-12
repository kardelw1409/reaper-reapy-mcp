"""P23 adaptive read-only enum-boundary discovery - **READ ONLY**; one artifact + one evidence file.

The gap this instrument closes. P22 put band boundaries into the profile from the
registry's own sweeps. Those sweeps are **change-point lists sampled on a grid**
(Vital's scale sweeps: 257 points for most controls, 17 for the filter styles),
so a recorded change point is only an *upper bound* on the true transition, with
the grid's own step as the resolution - measured 2026-10-11: 5,320 of 5,671
internal boundaries sit up to 3.35e-3 away from the uniform arithmetic, and that
whole distance is inside the sweep's own sampling resolution, so the sweep
cannot say which of the two is right. Neither can it be refined by re-reading it.

The host formatter can. ``TrackFX_FormatParamValueNormalized`` accepts an
**arbitrary double** and returns the display text for that value without moving
the parameter, so the transition between two printed states can be located to
whatever precision the caller asks for. This probe does exactly that, per
control, with three honestly distinct evidence levels:

``exact_at``
    a candidate boundary ``r`` was **proved exact**: ``format(r)`` is the upper
    member and ``format(prev_double(r))`` is the lower one, i.e. ``r`` is the
    smallest IEEE double the host formats as the upper member. No tolerance is
    involved in this claim.
``exact``
    bisection reached adjacent doubles (``hi == nextafter(lo)``): the transition
    is between two consecutive doubles, so the claim is again tolerance-free.
``interval`` / ``coarse_interval``
    the transition was bracketed to ``--tolerance`` (default 1e-9) or, when the
    budget ran out, to something wider. Reported with its width.

Safety. The only host calls are ``TrackFX_FormatParamValueNormalized`` (formats a
value the caller passes) plus, for the read-only proof,
``TrackFX_GetParamNormalized`` and ``GetTrackStateChunk``. No setter is called:
no ``TrackFX_SetParam``/``SetParamNormalized``, no state write, no project save.
The proof re-measures 31 watched normalized values and the state-chunk SHA-256
before and after the sweep and writes both verdicts into the artifact.

Run::

    py -3.11 tests/plugins/live/probe_p23_enum_boundaries.py [--out PATH] [--limit N]
        [--tolerance 1e-9] [--max-calls 60000] [--track 0] [--fx 0]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC = REPO_ROOT / "src"
HERE = Path(__file__).resolve().parent
TESTS_DIR = HERE.parent
for _path in (str(SRC), str(TESTS_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from reaper_reapy_mcp.plugins.providers.vital import enum_boundaries as EB  # noqa: E402
from reaper_reapy_mcp.plugins.providers.vital import profile_build as PB  # noqa: E402
from reaper_reapy_mcp.plugins.providers.vital.provider import VitalProvider  # noqa: E402

EVIDENCE_DIR = HERE / "evidence"

#: Rational grids a host is likely to quantize its parameters to. A candidate is
#: only ever *tested* - never assumed - and a failed candidate costs 2 calls.
_RATIONAL_DENOMINATORS: tuple[int, ...] = (16, 64, 100, 128, 256, 1000, 1024)

#: Bisection iterations per transition (bounds the work even when a bracket is
#: wide); the loop also stops on the tolerance and on float exhaustion.
_MAX_BISECTION_STEPS = 64

_NUMBER_RE = re.compile(r"^\s*([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)")


def _number_of(text: Any) -> float | None:
    """The leading number of a display text, or ``None``."""
    if text is None:
        return None
    match = _NUMBER_RE.match(str(text))
    if match is None:
        return None
    try:
        return float(match.group(1))
    except ValueError:  # pragma: no cover - the regex guarantees a float
        return None


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _profile_identity(profile: dict) -> dict:
    identity = profile["identity"]
    return {
        "fx_name": identity.get("fx_name"),
        "class_id": identity.get("class_id"),
        "module": identity.get("module"),
        "param_count": identity.get("fingerprint", {}).get("param_count"),
    }


class Budget:
    """A hard bound on host calls and wall time, shared by every control."""

    def __init__(self, max_calls: int, seconds: float) -> None:
        self.calls = 0
        self.max_calls = max_calls
        self.deadline = time.perf_counter() + seconds

    @property
    def exhausted(self) -> bool:
        return self.calls >= self.max_calls or time.perf_counter() > self.deadline

    def spent(self) -> None:
        self.calls += 1


def _members_of(control: dict) -> list[str]:
    return [str(member.get("id")) for member in (control.get("enum_values") or [])]


def _current_geometry(control: dict, n: int) -> tuple[list[float], str]:
    """The boundaries the profile's own read path uses, and where they came from."""
    params = (control.get("law") or {}).get("params") or {}
    raw = params.get("switch_points")
    if isinstance(raw, list) and len(raw) == n and n >= 2:
        return [float(value) for value in raw], "switch_points"
    return [0.0] + [(index - 0.5) / (n - 1) for index in range(1, n)], "uniform"


def _write_positions(control: dict, n: int) -> list[float]:
    params = (control.get("law") or {}).get("params") or {}
    raw = params.get("positions")
    if isinstance(raw, list) and len(raw) == n:
        return [float(value) for value in raw]
    return [index / max(1, n - 1) for index in range(n)]


def _rational_candidates(target: float) -> list[float]:
    """Round values near ``target`` on the grids a host quantizes to, nearest first."""
    seen: dict[float, None] = {}
    for denominator in _RATIONAL_DENOMINATORS:
        step = 1.0 / denominator
        for multiple in (math.floor(target / step), math.ceil(target / step)):
            value = multiple * step
            if 0.0 <= value <= 1.0:
                seen[round(value, 15)] = None
    return sorted(seen, key=lambda value: (abs(value - target), value))


def _classify_third_label(lower_member: str, upper_member: str, text: Any) -> str:
    """What a display that is *neither* adjacent member means.

    Measured 2026-10-11 on twenty Vital controls (``filter_1_drive``,
    ``lfo_N_phase``, ``mod_wheel``, ...): the "257-member enum" the registry's
    sweep produced is a **sampling artefact**. The sweep took 257 samples of a
    display that is *linear* in the normalized value (at norm 1/512 the host
    prints half of what it prints at 1/256, and the ratio is exactly 2.0), so its
    change points are evenly spaced samples, not band edges. Such a control has no
    band structure to find: between two adjacent "members" the host prints a value
    strictly between them. That is a different finding from a *foreign* label (a
    text that is not a number on the same scale), and the two are reported apart.
    """
    number = _number_of(text)
    low = _number_of(lower_member)
    high = _number_of(upper_member)
    if number is None or low is None or high is None:
        return "foreign_label"
    return "continuous_display" if min(low, high) < number < max(low, high) else "foreign_label"


def discover_control(
    *,
    control_id: str,
    index: int,
    control: dict,
    text_of: Callable[[float], str | None],
    budget: Budget,
    tolerance: float,
    epsilon: float,
    step_of: Callable[[], dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """Locate this control's band structure with format calls only."""
    members = _members_of(control)
    n = len(members)
    current, current_source = _current_geometry(control, n)
    positions = _write_positions(control, n)
    record: dict[str, Any] = {
        "index": index,
        "members": members,
        "n_members": n,
        "current_switch_points": [round(value, 12) for value in current],
        "current_source": current_source,
        "current_positions": [round(value, 12) for value in positions],
        "transitions": [],
        "positions_checked": [],
        "tail": None,
        "unresolved": [],
        "calls": 0,
        "notes": [],
    }
    calls_before = budget.calls

    probe_cache: dict[str, str | None] = {}

    def probe(value: float) -> str | None:
        """Format ``value``, caching repeats (the anchors are all probed already).

        The cache key is the **exact** double (``float.hex``), never a rounded
        value: ``math.nextafter(0.5, -inf)`` rounds to ``0.5`` at 15 decimals, and
        a cache that answers that probe with the 0.5 text would make every
        exactness proof fail while looking like a host property (measured
        2026-10-11: it turned 705 provable boundaries into "consistent" ones).
        """
        key = float(value).hex()
        if key in probe_cache:
            return probe_cache[key]
        if budget.exhausted:
            return None
        result = text_of(value)
        budget.spent()
        probe_cache[key] = result
        return result

    # -- stage 0: one anchor per member (the write target, or a value inside its band)
    anchors: list[float | None] = [None] * n
    for member_index, member in enumerate(members):
        target = positions[member_index]
        before = probe(target)
        if before == member:
            anchors[member_index] = target
            record["positions_checked"].append(
                {"member": member, "position": round(target, 12), "text": before, "ok": True}
            )
            continue
        # the profile's write target does not display this member: search inside
        # the band the profile itself believes in, then the band the *candidate*
        # boundaries imply - bounded, and every attempt is recorded
        band_lo = current[member_index]
        band_hi = current[member_index + 1] if member_index + 1 < n else 1.0
        found: float | None = None
        attempts: list[dict[str, Any]] = [{"value": round(target, 12), "text": before}]
        for candidate in (
            band_lo + (band_hi - band_lo) / 2.0,
            band_lo,
            band_lo + 1e-9,
            band_lo + 1e-6,
            min(1.0, band_hi - 1e-9),
        ):
            if found is not None or budget.exhausted:
                break
            if not (0.0 <= candidate <= 1.0):
                continue
            text = probe(candidate)
            attempts.append({"value": round(candidate, 12), "text": text})
            if text == member:
                found = candidate
        anchors[member_index] = found
        record["positions_checked"].append(
            {
                "member": member,
                "position": round(target, 12),
                "text": before,
                "ok": found == target,
                "band": [round(band_lo, 12), round(band_hi, 12)],
                "anchor": None if found is None else round(found, 12),
                "attempts": attempts[:6],
                "note": (
                    "the profile's write target for this member does not display it"
                    if found is None or found != target
                    else None
                ),
            }
        )
        if found is None:
            record["unresolved"].append(
                {"member": member, "reason": "no value inside the profile's band displays this member"}
            )

    # -- stage 1: the internal transitions, adjacent anchors only
    for member_index in range(1, n):
        entry: dict[str, Any] = {
            "i": member_index,
            "lower_member": members[member_index - 1],
            "upper_member": members[member_index],
            "current": round(current[member_index], 12),
            "calls": 0,
        }
        calls_before_transition = budget.calls
        lower_anchor = anchors[member_index - 1]
        upper_anchor = anchors[member_index]
        if lower_anchor is None or upper_anchor is None:
            entry.update(
                {
                    "state": "unresolved",
                    "reason": "an adjacent member has no anchor whose display is that member",
                }
            )
            record["transitions"].append(entry)
            continue
        if lower_anchor >= upper_anchor:
            entry.update(
                {
                    "state": "unresolved",
                    "reason": "the anchors are not in member order",
                    "anchors": [round(lower_anchor, 12), round(upper_anchor, 12)],
                }
            )
            record["transitions"].append(entry)
            continue

        # (a) a candidate boundary, proved in two calls. The profile's own value is
        # the first candidate: proving it exact is the *strongest* outcome (it
        # upgrades "verified within epsilon" to "no tolerance involved"), and a
        # rational grid candidate is what a host that quantizes its parameters
        # would actually use.
        candidate_values = [current[member_index]]
        for candidate in _rational_candidates(current[member_index]) + _rational_candidates(
            lower_anchor + (upper_anchor - lower_anchor) / 2.0
        ):
            if candidate not in candidate_values:
                candidate_values.append(candidate)
        proved = None
        tested_candidates: list[dict[str, Any]] = []
        for candidate in candidate_values:
            if budget.exhausted or proved is not None:
                break
            if not (lower_anchor <= candidate <= upper_anchor):
                continue
            text = probe(candidate)
            tested_candidates.append({"value": round(candidate, 12), "text": text})
            if text != members[member_index]:
                continue
            below = math.nextafter(candidate, -math.inf)
            text_below = probe(below)
            tested_candidates.append({"value": round(below, 17), "text": text_below})
            if text_below == members[member_index - 1]:
                proved = candidate
        if proved is not None:
            entry.update(
                {
                    "state": "exact_at",
                    "lo": round(math.nextafter(proved, -math.inf), 17),
                    "hi": round(proved, 17),
                    "width": 0.0,
                    "value": round(proved, 12),
                    "lower_text": members[member_index - 1],
                    "upper_text": members[member_index],
                    "tested": tested_candidates[:6],
                    "note": "the smallest double the host formats as the upper member",
                }
            )
            entry["calls"] = budget.calls - calls_before_transition
            record["transitions"].append(entry)
            continue

        # (b) the profile's own boundary: a *bracket* is what the round needs, not
        # an "it agrees to within epsilon". If the boundary reproduces both sides
        # within epsilon, the transition is inside (b-epsilon, b]; bisecting that
        # window costs ~17 calls and turns "consistent" into a located transition.
        boundary = current[member_index]
        text_at = probe(boundary)
        text_below_boundary = probe(max(0.0, boundary - epsilon))
        if text_at == members[member_index] and text_below_boundary == members[member_index - 1]:
            lo_value = max(0.0, boundary - epsilon)
            hi_value = boundary
        elif text_at == members[member_index - 1]:
            # the boundary is *below* the profile's value: search upward
            text_above = probe(min(1.0, boundary + epsilon))
            if text_above != members[member_index]:
                lo_value, hi_value = lower_anchor, upper_anchor
            else:
                lo_value, hi_value = boundary, min(1.0, boundary + epsilon)
        else:
            # the profile's own boundary formats as neither adjacent member: the
            # anchors bracket the transition instead (and a third label inside is
            # classified below)
            lo_value, hi_value = lower_anchor, upper_anchor

        unexpected: list[dict[str, Any]] = []
        steps = 0
        while steps < _MAX_BISECTION_STEPS and not budget.exhausted:
            if hi_value - lo_value <= tolerance:
                break
            middle = lo_value + (hi_value - lo_value) / 2.0
            if middle <= lo_value or middle >= hi_value:
                break  # float exhaustion: the bracket is already adjacent doubles
            text = probe(middle)
            steps += 1
            if text == members[member_index - 1]:
                lo_value = middle
            elif text == members[member_index]:
                hi_value = middle
            else:
                unexpected.append({"value": round(middle, 12), "text": text})
                break
        width = hi_value - lo_value
        if unexpected:
            state = _classify_third_label(members[member_index - 1], members[member_index], unexpected[0]["text"])
        elif hi_value == math.nextafter(lo_value, math.inf):
            state = "exact"
        elif width <= tolerance:
            state = "interval"
        else:
            state = "coarse_interval"
        entry.update(
            {
                "state": state,
                "lo": round(lo_value, 17),
                "hi": round(hi_value, 17),
                "width": round(width, 17),
                "lower_text": members[member_index - 1],
                "upper_text": members[member_index],
                "steps": steps,
                "unexpected": unexpected[:3],
                "bracket_source": (
                    "epsilon window around the profile's boundary"
                    if abs(hi_value - boundary) <= epsilon and lo_value >= boundary - epsilon
                    else "member anchors"
                ),
            }
        )
        entry["calls"] = budget.calls - calls_before_transition
        record["transitions"].append(entry)

    # -- stage 1b: characterize a *continuous* display with its own measured map.
    # "This control has no band structure" is a claim, so it is backed by the
    # measurement that produced it: the display sampled away from the sweep's own
    # grid, the slope fitted to it, and the largest deviation from that line.
    continuous = [entry for entry in record["transitions"] if entry.get("state") == "continuous_display"]
    if continuous and step_of is not None:
        record["host_step_metadata"] = step_of()
    if continuous:
        samples: list[dict[str, Any]] = []
        for fraction in (0.0, 0.25, 0.5, 0.75, 1.0):
            for divisor in (3, 7):
                point = fraction + (1.0 / divisor) * 0.001 if fraction < 1.0 else fraction
                point = min(1.0, max(0.0, point))
                text = probe(point)
                samples.append(
                    {"normalized": round(point, 12), "text": text, "number": _number_of(text)}
                )
        usable = [(row["normalized"], row["number"]) for row in samples if row["number"] is not None]
        fit: dict[str, Any] = {"points": len(usable)}
        if len(usable) >= 3:
            count = len(usable)
            sx = sum(point for point, _v in usable)
            sy = sum(value for _p, value in usable)
            sxx = sum(point * point for point, _v in usable)
            sxy = sum(point * value for point, value in usable)
            denominator = count * sxx - sx * sx
            if abs(denominator) > 1e-18:
                slope = (count * sxy - sx * sy) / denominator
                intercept = (sy - slope * sx) / count
                residual = max(abs(value - (slope * point + intercept)) for point, value in usable)
                fit.update(
                    {
                        "slope": round(slope, 9),
                        "intercept": round(intercept, 9),
                        "max_residual": round(residual, 12),
                        "linear": residual <= 1e-6 * max(1.0, abs(slope)),
                    }
                )
        record["continuous_display"] = {
            "transitions": [entry["i"] for entry in continuous],
            "samples": samples[:10],
            "fit": fit,
            "note": (
                "between adjacent members the host prints a value strictly between them "
                "(a linear ramp sampled on the sweep's grid), so this control has no band "
                "structure: its member list is the sweep's own sample grid"
            ),
        }

    # The interior pass needs the tail measured first: the last member's band ends
    # where the unlabelled region begins, and with the tail unknown that band looks
    # open to 1.0 - which is how the probe first reported all three filter styles as
    # having an unverified interior (the interior it then probed printed the empty
    # string, because 0.71875 lies inside the unlabelled region).
    # -- stage 2: the region above the last member (an unlabelled tail is real:
    # `Filter 1 Style`'s sixth state prints an empty string)
    last_anchor = anchors[n - 1]
    if last_anchor is not None and n >= 2 and not budget.exhausted:
        top = probe(1.0)
        if top is not None and top != members[n - 1]:
            lo_value, hi_value = last_anchor, 1.0
            steps = 0
            while steps < _MAX_BISECTION_STEPS and not budget.exhausted:
                if hi_value - lo_value <= tolerance:
                    break
                middle = lo_value + (hi_value - lo_value) / 2.0
                if middle <= lo_value or middle >= hi_value:
                    break
                text = probe(middle)
                steps += 1
                if text == members[n - 1]:
                    lo_value = middle
                else:
                    hi_value = middle
            record["tail"] = {
                "state": "unlabelled_region",
                "lo": round(lo_value, 17),
                "hi": round(hi_value, 17),
                "width": round(hi_value - lo_value, 17),
                "text": top,
                "steps": steps,
                "note": (
                    "above this value the host prints a text that is not one of the "
                    "profile's members; the member list, not the boundary, is the gap"
                ),
            }
        elif top == members[n - 1]:
            record["tail"] = {"state": "member_to_the_top", "text": top}

    # -- stage 1c: each band's *interior*, verified through the same formatter.
    # The brackets say where the transitions are; they say nothing about what happens
    # between them. A display that spikes inside a band (non-monotone), or one whose
    # printed digits cannot express the band at all, would otherwise be merged as if
    # the interior were the member the boundaries imply. One probe per band, and the
    # control is only mergeable when every interior printed its own member.
    measured_interiors: list[dict[str, Any]] = []
    if all(entry.get("state") in ("exact_at", "exact", "interval", "confirmed_within") for entry in record["transitions"]):
        ends = [0.0]
        ok = True
        for entry in record["transitions"]:
            high = float(entry["hi"])
            if high <= ends[-1]:
                ok = False
                break
            ends.append(high)
        top = 1.0
        tail = record.get("tail") or {}
        if tail.get("state") == "unlabelled_region" and float(tail.get("hi") or 0.0) > ends[-1]:
            top = float(tail["hi"])
        if ok:
            ends.append(top)
            for member_index, member in enumerate(members):
                low, high = ends[member_index], ends[member_index + 1]
                middle = low + (high - low) / 2.0
                text = probe(middle)
                verified = text == member
                measured_interiors.append(
                    {
                        "member": member,
                        "normalized": round(middle, 12),
                        "band": [round(low, 12), round(high, 12)],
                        "text": text,
                        "verified": verified,
                    }
                )
        record["band_interiors"] = {
            "checked": len(measured_interiors),
            "verified": sum(1 for row in measured_interiors if row["verified"]),
            "mismatches": [row for row in measured_interiors if not row["verified"]][:6],
        }

    record["calls"] = budget.calls - calls_before
    record["anchors"] = [None if anchor is None else round(anchor, 12) for anchor in anchors]
    return record


def _summarize(controls: dict[str, Any], *, seconds: float, calls: int, budget: Budget) -> dict:
    states: dict[str, int] = {}
    transitions = 0
    for record in controls.values():
        for entry in record["transitions"]:
            transitions += 1
            states[str(entry.get("state"))] = states.get(str(entry.get("state")), 0) + 1
    return {
        "controls_measured": len(controls),
        "transitions": transitions,
        "transition_states": dict(sorted(states.items())),
        "controls_continuous_display": sum(
            1 for r in controls.values() if r.get("continuous_display")
        ),
        "controls_with_tail": sum(
            1 for r in controls.values() if (r.get("tail") or {}).get("state") == "unlabelled_region"
        ),
        "band_interiors_checked": sum(
            int((r.get("band_interiors") or {}).get("checked") or 0) for r in controls.values()
        ),
        "band_interiors_verified": sum(
            int((r.get("band_interiors") or {}).get("verified") or 0) for r in controls.values()
        ),
        "controls_with_unverified_interior": sum(
            1
            for r in controls.values()
            if (r.get("band_interiors") or {}).get("checked")
            and (r["band_interiors"]["verified"] != r["band_interiors"]["checked"])
        ),
        "positions_checked": sum(len(r["positions_checked"]) for r in controls.values()),
        "positions_wrong": sum(
            1 for r in controls.values() for p in r["positions_checked"] if not p["ok"]
        ),
        "unresolved_members": sum(len(r["unresolved"]) for r in controls.values()),
        "host_calls": calls,
        "calls_budget": budget.max_calls,
        "budget_exhausted": budget.exhausted,
        "seconds": round(seconds, 3),
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=None)
    parser.add_argument("--limit", type=int, default=0, help="measure at most N enum controls (0 = all)")
    parser.add_argument("--tolerance", type=float, default=1e-9)
    parser.add_argument("--epsilon", type=float, default=1e-4)
    parser.add_argument("--max-calls", type=int, default=60000)
    parser.add_argument("--seconds", type=float, default=600.0)
    parser.add_argument("--track", type=int, default=0)
    parser.add_argument("--fx", type=int, default=0)
    args = parser.parse_args(argv[1:])

    provider = VitalProvider()
    source_profile = provider.build_profile(
        {
            "fx_name": PB.VITAL_FX_NAME,
            "class_id": PB.VITAL_CLASS_ID,
            "module": PB.VITAL_MODULE,
            "param_count": 2858,
        }
    )
    identity = _profile_identity(source_profile)
    enums = [c for c in source_profile["controls"] if c.get("type") == "enum"]
    if args.limit:
        enums = enums[: args.limit]

    import reapy  # noqa: PLC0415 - only the live probe imports the host client
    from reapy import reascript_api as R  # noqa: PLC0415

    budget = Budget(args.max_calls, args.seconds)
    out_path = Path(args.out) if args.out else Path(EB.default_enum_boundaries_path())
    artifact: dict[str, Any] = {
        "kind": EB.ENUM_BOUNDARIES_KIND,
        "method_version": EB.ENUM_BOUNDARIES_METHOD_VERSION,
        "generated_utc": _utc(),
        "host": {"name": "reaper", "transport": "reapy"},
        "identity": identity,
        "method": {
            "call": "TrackFX_FormatParamValueNormalized",
            "strategy": (
                "per control: verify each write position; prove a rational candidate in two "
                "calls (exact_at); else verify the profile's own boundary inside epsilon; else "
                "bisect between adjacent member anchors"
            ),
            "tolerance": args.tolerance,
            "epsilon": args.epsilon,
            "max_bisection_steps": _MAX_BISECTION_STEPS,
            "candidate_denominators": list(_RATIONAL_DENOMINATORS),
            "evaluates": "the display text of a normalized value the caller passes",
            "writes": "none: no setter, no state write, no project save",
        },
        "controls": {},
    }

    reapy.connect()
    with reapy.inside_reaper():
        track = R.GetTrack(0, args.track)
        names = list(R.TrackFX_GetFXName(track, args.fx, "", 512))
        artifact["host"]["fx_name_reported"] = next(
            (name for name in names if isinstance(name, str) and name.startswith("VST")), ""
        )
        reported_params = int(R.TrackFX_GetNumParams(track, args.fx))
        artifact["host"]["param_count_reported"] = reported_params
        if reported_params != identity["param_count"]:
            print(
                f"refusing to measure: the host reports {reported_params} parameters, the "
                f"profile was built for {identity['param_count']}",
                file=sys.stderr,
            )
            return 2

        def raw_format(value: float, index: int) -> str | None:
            raw = list(R.TrackFX_FormatParamValueNormalized(track, args.fx, index, float(value), "", 512))
            if len(raw) <= 5:
                return None
            text = raw[5]
            return None if text is None else str(text)

        def step_metadata(index: int) -> dict[str, Any] | None:
            """`TrackFX_GetParameterStepSizes` for one control - metadata, read-only."""
            raw = list(R.TrackFX_GetParameterStepSizes(track, args.fx, index, 0.0, 0.0, 0.0, False))
            if len(raw) < 8:
                return None
            return {
                "reported": bool(raw[0]),
                "step": float(raw[4]),
                "small": float(raw[5]),
                "large": float(raw[6]),
                "is_toggle": bool(raw[7]),
            }

        watch = list(range(0, reported_params, 25))
        before_values = [float(R.TrackFX_GetParamNormalized(track, args.fx, i)) for i in watch]
        before_chunk = hashlib.sha256(
            str(R.GetTrackStateChunk(track, "", 20_000_000, False)).encode("utf-8")
        ).hexdigest()

        started = time.perf_counter()
        for control in enums:
            if budget.exhausted:
                artifact["notes"] = ["the call budget was exhausted; the remaining controls were not measured"]
                break
            index = (control.get("binding") or {}).get("index")
            if not isinstance(index, int):
                continue
            artifact["controls"][str(control["id"])] = discover_control(
                control_id=str(control["id"]),
                index=index,
                control=control,
                text_of=lambda value, index=index: raw_format(value, index),
                budget=budget,
                tolerance=args.tolerance,
                epsilon=args.epsilon,
                step_of=lambda index=index: step_metadata(index),
            )
        seconds = time.perf_counter() - started

        after_values = [float(R.TrackFX_GetParamNormalized(track, args.fx, i)) for i in watch]
        after_chunk = hashlib.sha256(
            str(R.GetTrackStateChunk(track, "", 20_000_000, False)).encode("utf-8")
        ).hexdigest()

    artifact["read_only_proof"] = {
        "values_unchanged": before_values == after_values,
        "chunk_sha256_unchanged": before_chunk == after_chunk,
        "watched_points": len(watch),
        "statement": (
            "TrackFX_FormatParamValueNormalized, TrackFX_GetParamNormalized and "
            "GetTrackStateChunk were called; the watched normalized values and the state "
            "chunk are byte-identical before and after"
        ),
    }
    artifact["counts"] = _summarize(
        artifact["controls"], seconds=seconds, calls=budget.calls, budget=budget
    )
    artifact["counts"]["read_only"] = {
        "values_unchanged": artifact["read_only_proof"]["values_unchanged"],
        "chunk_sha256_unchanged": artifact["read_only_proof"]["chunk_sha256_unchanged"],
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(artifact, indent=2, sort_keys=True)
    # ``newline="\n"`` matters: the plain form translates LF to CRLF on Windows, so
    # the file's bytes would differ from the bytes this digest is taken over - and
    # those bytes are cache-key material, so the recorded digest *is* the thing a
    # reader will compare against. Verified by re-reading what was written.
    out_path.write_text(text, encoding="utf-8", newline="\n")
    digest = hashlib.sha256(out_path.read_bytes()).hexdigest()

    evidence = {
        "probe": "p23_enum_boundaries",
        "utc": _utc(),
        "artifact": {
            "path": str(out_path),
            "bytes": len(out_path.read_bytes()),
            "sha256": digest,
            "sha256_matches_disk": hashlib.sha256(out_path.read_bytes()).hexdigest() == digest,
        },
        "identity": identity,
        "host": artifact["host"],
        "method": artifact["method"],
        "counts": artifact["counts"],
        "transition_states": artifact["counts"]["transition_states"],
        "read_only_proof": artifact["read_only_proof"],
        "per_control": {
            control_id: {
                "transitions": [
                    {
                        "i": entry["i"],
                        "state": entry.get("state"),
                        "current": entry.get("current"),
                        "lo": entry.get("lo"),
                        "hi": entry.get("hi"),
                        "width": entry.get("width"),
                        "value": entry.get("value"),
                        "bracket_contains_current": (
                            entry.get("lo") is not None
                            and entry.get("hi") is not None
                            and entry.get("current") is not None
                            and float(entry["lo"]) <= float(entry["current"]) <= float(entry["hi"])
                        ),
                    }
                    for entry in record["transitions"]
                ],
                "positions_wrong": [
                    {"member": row["member"], "position": row["position"], "text": row["text"]}
                    for row in record["positions_checked"]
                    if not row["ok"]
                ],
                "tail": record.get("tail"),
                "unresolved": record["unresolved"],
                "band_interiors": record.get("band_interiors"),
                "calls": record["calls"],
            }
            for control_id, record in artifact["controls"].items()
        },
    }
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = _run_stamp()
    evidence_path = EVIDENCE_DIR / f"p23_enum_boundaries__{stamp}__{digest[:12]}.json"
    if evidence_path.exists():
        evidence_path = EVIDENCE_DIR / f"p23_enum_boundaries__{stamp}__{digest[:12]}__2.json"
    evidence_path.write_text(json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8")

    print(f"artifact: {out_path}  sha256={digest[:16]}  {evidence['artifact']['bytes']:,} bytes")
    print(f"evidence: {evidence_path}")
    print(f"counts:   {json.dumps(artifact['counts'], sort_keys=True)}")
    print(
        "read-only proof: {} {}".format(
            artifact["read_only_proof"]["values_unchanged"],
            artifact["read_only_proof"]["chunk_sha256_unchanged"],
        )
    )
    print(f"artifact digest verified on disk: {evidence['artifact']['sha256_matches_disk']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
