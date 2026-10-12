"""The providerless path: describe / find / read / write any FX from an artifact (P28).

The four plugin tools were provider-gated: `plugin_describe`, `plugin_find`,
`plugin_read` and `plugin_write` all resolved their subject through a configured
*provider*, so an FX no provider claims answered `InstanceNotFound` (or
`UnsupportedProvider` with none configured). For a synth with no provider the
whole tool surface was therefore inert, and the measured consequence is in the
round's evidence: the classic name-addressed setter
(`TrackFX_GetParamName` per parameter) costs **31.4 ms/param unheld = 89.7 s per
pass over 2858 parameters**, and `get_fx_param_list` projects to ~277 s for the
same instance. A compact, indexed path over the **prepared discovery artifact**
is what removes that (brief §6, §7.4).

What this module is - and is not:

* It is a **profile-shaped view** of an artifact the discovery engine measured,
  so the existing tools (module rows, name resolution, `values` conversion,
  `Transaction`) work unchanged. The view never re-measures and never touches
  the host on its own.
* It is not a second engine: the artifact is still produced by `plugin_prepare`,
  the values still convert through `plugins.values`, and the batch write still
  runs inside `plugins.transaction.Transaction`.
* Its **law declarations are verified, not assumed**: every numeric law proposed
  here is checked against the artifact's own recorded samples through the very
  functions the write path uses (`values.normalized_to_native` /
  `from_native`). A control whose samples no candidate reproduces keeps
  ``law.kind = "none"`` - readable, and refused for writing with a typed
  `NoConversion` instead of a guessed conversion.
* The "native" space of a host-scope control **is its display space**: the host
  exposes exactly two spaces (a 0..1 normalized position and the formatter's
  text), so the only verifiable conversion is normalized <-> printed text. That
  is stated on every materialized control (`evidence.native_space`).
* Nothing here claims the state/document channel: without a codec there is no
  preset, no route edit and no document rollback. A request naming one is
  refused with a typed problem row, never silently ignored.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from . import binding as B
from . import request_value as RV
from . import values as V
from .cache import CacheStatus, ProfileBuild
from .discovery import index as INDEX
from .discovery.index import ControlIndex, IndexedControl, load_index, normalise
from .discovery.store import ENGINE_VERSION, PreparedArtifact, PreparedStore
from .errors import (
    DiscoveryArtifactMissing,
    InvalidValue,
    NoConversion,
    PluginError,
    UnsupportedControl,
)
from .model import (
    CaptureScope,
    PluginInstanceRef,
    ResolvedRequest,
    VerificationPolicy,
)
from .profile import ProfileValidation

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps the import graph acyclic
    from .resolve import ResolvedInstance

__all__ = [
    "SCOPE_ID",
    "REVISION",
    "HostScope",
    "HostScopeProvider",
    "materialize_profile",
    "materialize_control",
    "resolve_discovery_instance",
    "artifact_ready",
    "artifact_stats",
    "load_artifact",
    "readiness",
]

#: The scope/provider id used for every host-scope answer. It is deliberately a
#: *scope*, not a registered provider: nothing can be reached through the profile
#: cache, and `plugin_instances` never lists it as a plugin provider.
SCOPE_ID = "host-scope"

#: Declared revision of this path's semantics. It travels in the cache key and in
#: the answer's meta, so a materialization change is visible in both.
REVISION = "host-scope-r1"

#: Materialization version: part of the in-process profile cache key, so a rule
#: change cannot be served a profile built by the previous rules.
MATERIALIZATION_VERSION = "materialize-v1"

#: The default relative tolerance when an artifact records no printed resolution.
#: A law that cannot be checked is not declared (see `_numeric_law`).
_NO_TOLERANCE = 0.0


# ---------------------------------------------------------------------------
# The scope
# ---------------------------------------------------------------------------


class HostScope:
    """A provider-shaped scope that claims **every** FX slot of the host.

    It supplies enumeration and identity only: `matches`/`identify` and nothing
    else. It builds no profile of its own, declares no capability and is never
    registered as a provider, so the profile path cannot reach it - the
    discovery path decides separately whether a usable artifact exists.
    """

    id = SCOPE_ID

    def matches(self, facts: Any) -> bool:
        return True

    def identify(self, facts: Any) -> dict[str, Any]:
        return {
            "fx_name": facts.fx_name,
            "class_id": facts.class_id,
            "module": facts.module,
            "param_count": facts.param_count,
        }


# ---------------------------------------------------------------------------
# Materialization: one artifact control -> one profile control
# ---------------------------------------------------------------------------


def _samples_of(observation: Mapping[str, Any] | None) -> list[tuple[float, str]]:
    """The recorded (position, printed text) pairs of one control."""
    if not isinstance(observation, Mapping):
        return []
    rows: list[tuple[float, str]] = []
    for sample in observation.get("samples") or []:
        if not isinstance(sample, Mapping):
            continue
        position = sample.get("normalized")
        text = sample.get("text")
        if isinstance(position, bool) or not isinstance(position, (int, float)):
            continue
        if text is None:
            continue
        rows.append((float(position), str(text)))
    rows.sort(key=lambda row: row[0])
    return rows


def _numeric_samples(samples: Sequence[tuple[float, str]]) -> list[tuple[float, float]]:
    from .discovery.text import parse_display

    rows: list[tuple[float, float]] = []
    for position, text in samples:
        parsed = parse_display(text)
        if parsed.numeric and parsed.value is not None:
            rows.append((position, float(parsed.value)))
    return rows


def _printed_tolerance(row: Mapping[str, Any], samples: Sequence[tuple[float, str]]) -> float:
    """The tightest meaningful comparison for a control's printed values.

    The artifact records the tolerance its own fit was accepted against
    (`classification.evidence.printed_tolerance`). When it does not, the gap
    between adjacent *distinct* printed values is the best available bound on
    what the display can resolve - and if neither exists, ``0.0`` is returned,
    which makes every candidate law fail rather than pass by default.
    """
    evidence = dict((row.get("classification") or {}).get("evidence") or {})
    recorded = evidence.get("printed_tolerance")
    if isinstance(recorded, (int, float)) and not isinstance(recorded, bool) and recorded > 0:
        return float(recorded)
    values = sorted({value for _position, value in _numeric_samples(samples)})
    gaps = [b - a for a, b in zip(values, values[1:]) if b > a]
    return min(gaps) / 2.0 if gaps else _NO_TOLERANCE


def _verify_numeric(control: dict[str, Any], points: Sequence[tuple[float, float]], tolerance: float) -> dict[str, Any]:
    """Check a candidate numeric law against the artifact's own samples.

    Both directions are exercised on every sample: the **read** direction
    (position -> value, what `plugin_read` reports) and the **write** direction
    (value -> position -> value, what a write would store and the read-back would
    compare). A law that only agrees in one direction is not a usable law.
    """
    worst_read = 0.0
    worst_write = 0.0
    failures: list[str] = []
    bounds = dict(control.get("range") or {})
    low = bounds.get("min")
    high = bounds.get("max")
    for position, expected in points:
        try:
            native = V.normalized_to_native(control, position)
            display = V.from_native(control, native)
            worst_read = max(worst_read, abs(float(display) - expected))
        except PluginError as exc:
            failures.append(f"read({position}): {exc.code}")
            continue
        try:
            # A law's own endpoint can round a hair outside the declared range; the
            # write direction is checked on the value the range admits.
            target = float(display)
            if isinstance(low, (int, float)) and isinstance(high, (int, float)) and not isinstance(low, bool):
                target = min(max(target, float(low)), float(high))
            native_written = V.to_native(control, target)
            normalized = V.to_normalized(control, native_written)
            back = V.from_native(control, V.normalized_to_native(control, normalized))
            worst_write = max(worst_write, abs(float(back) - expected))
        except PluginError as exc:
            failures.append(f"write({position}): {exc.code}")
    ok = not failures and worst_read <= tolerance and worst_write <= tolerance
    return {
        "ok": ok,
        "worst_read_error": worst_read,
        "worst_write_error": worst_write,
        "tolerance": tolerance,
        "n_points": len(points),
        "failures": failures[:4],
    }


def _padded(bounds: tuple[float, float]) -> tuple[float, float]:
    """A range widened by a relative hair (1e-9 of its span, floored at 1e-12).

    Why: a *power* law's own endpoints are ``offset`` and ``offset + base``, and a
    fitted ``base`` can put the top endpoint an ulp or two above the printed
    maximum the artifact recorded (measured on the first run of these tests: the
    law evaluated to 4.000000000000001 against a range max of 4.0, so `validate`
    refused the law's own endpoint as out of range - a verification artefact, not
    a wrong law). The pad is relative to the span, far below any printable
    resolution, so it cannot admit a value the plugin would consider out of range.
    """
    span = abs(bounds[1] - bounds[0])
    pad = max(span * 1e-9, 1e-12)
    return (bounds[0] - pad, bounds[1] + pad)


def _measured_range(row: Mapping[str, Any]) -> tuple[float, float] | None:
    """The artifact's chosen range, only when it is in **display** space."""
    analysis = row.get("range")
    if not isinstance(analysis, Mapping):
        return None
    if str(analysis.get("space") or "") not in ("display", "sampled"):
        return None
    chosen = analysis.get("chosen")
    if not isinstance(chosen, (list, tuple)) or len(chosen) != 2:
        return None
    low, high = chosen
    if isinstance(low, bool) or isinstance(high, bool):
        return None
    if not isinstance(low, (int, float)) or not isinstance(high, (int, float)):
        return None
    low, high = float(low), float(high)
    if low == high:
        return None
    return (min(low, high), max(low, high))


def _quantization(row: Mapping[str, Any], bounds: tuple[float, float] | None) -> dict[str, Any] | None:
    """The host's own step as a storage grid - only when the range sits on it.

    The step is declared as a grid only when the measured range is an exact
    multiple of it, because `values._snap_to_grid` clamps into ``grid ∩ range``:
    a range that does not sit on the grid would make the clamp a *different*
    claim than the plugin's own rounding (the divergence P25 recorded for a
    misaligned ladder). Otherwise the step is reported as information and no
    grid is claimed.
    """
    steps = dict(row.get("steps") or {})
    step = steps.get("step")
    if steps.get("reported") is not True or steps.get("is_toggle"):
        return None
    if isinstance(step, bool) or not isinstance(step, (int, float)) or float(step) <= 0.0:
        return None
    step = float(step)
    if bounds is not None:
        span = bounds[1] - bounds[0]
        ratio = span / step
        if abs(ratio - round(ratio)) > 1e-6 or round(ratio) <= 0:
            return None
    return {"step": step, "origin": 0.0, "rounding": "half_away_from_zero"}


def _integer_grid(points: Sequence[tuple[float, float]]) -> dict[str, Any] | None:
    """The storage grid an integers-only display proves, or ``None``.

    A display that only ever prints whole numbers is quantised: the value the
    plugin *stores* sits on a grid, and a continuous law through the printouts is
    a model of the rounding, wrong between the steps. The step is the **greatest
    common divisor of the observed gaps** - a 17-point sweep of a 21-state ladder
    shows gaps of 1 and 2, and their gcd (1) is the only step consistent with both
    - with the origin taken from the lowest printed value. The grid is then
    checked by the ordinary verification: a candidate is accepted only if it
    reproduces *every* sampled text, so a grid that is wrong cannot survive.

    Returns ``None`` for a display whose samples are not whole numbers (nothing
    about a grid is provable then) or that never repeats a value.
    """
    import math

    values = [value for _position, value in points]
    if any(abs(value - round(value)) > 1e-9 for value in values):
        return None
    integers = [int(round(value)) for value in values]
    gaps = {abs(later - earlier) for earlier, later in zip(integers, integers[1:]) if later != earlier}
    if not gaps:
        return None
    step = 0
    for gap in sorted(gaps):
        step = math.gcd(step, gap)
    if step <= 0:
        return None
    origin = float(min(integers) % step)
    return {"step": float(step), "origin": origin, "rounding": "half_away_from_zero"}


def _numeric_law(row: Mapping[str, Any], samples: Sequence[tuple[float, str]]) -> dict[str, Any]:
    """The simplest *verified* law for a numeric control, or the reason there is none."""
    points = _numeric_samples(samples)
    tolerance = _printed_tolerance(row, samples)
    mapping = row.get("mapping") or {}
    chosen = dict(mapping.get("chosen") or {}) if isinstance(mapping, Mapping) else {}
    fit_params = dict(chosen.get("params") or {})
    bounds = _measured_range(row)
    evidence = dict((row.get("classification") or {}).get("evidence") or {})
    if isinstance(mapping, Mapping) and mapping.get("reason") == "formatter_constant":
        return {
            "law": {"kind": "none"},
            "reason": "formatter_constant",
            "evidence": {
                "note": (
                    "every sampled position printed the same number, so the display carries no "
                    "information about the value: no law can be verified against it, and a write "
                    "could not be checked in display space"
                )
            },
        }
    if len(points) < 3:
        return {"law": {"kind": "none"}, "reason": "fewer_than_three_numeric_samples", "evidence": {}}
    if tolerance <= 0.0:
        return {
            "law": {"kind": "none"},
            "reason": "no_printed_resolution_measured",
            "evidence": {"note": "without a printed tolerance no candidate law can be checked"},
        }
    low, high = min(value for _p, value in points), max(value for _p, value in points)
    ascending = points[-1][1] >= points[0][1]
    direction = "ascending" if ascending else "descending"
    measured_bounds = (min(low, high), max(low, high)) if bounds is None else bounds

    candidates: list[tuple[str, dict[str, Any]]] = []
    candidates.append(
        (
            "affine",
            {
                "kind": "affine",
                "params": {"scale": 1.0, "offset": 0.0, "direction": direction},
                "range": {"min": measured_bounds[0], "max": measured_bounds[1]},
            },
        )
    )
    exponent = fit_params.get("exponent")
    scale = fit_params.get("scale")
    offset = fit_params.get("offset")
    if chosen.get("kind") == "power" and isinstance(exponent, (int, float)) and not isinstance(exponent, bool):
        base = float(scale) if isinstance(scale, (int, float)) and not isinstance(scale, bool) else None
        origin = float(offset) if isinstance(offset, (int, float)) and not isinstance(offset, bool) else 0.0
        if base is not None and base > 0.0 and float(exponent) > 0.0:
            candidates.append(
                (
                    "power",
                    {
                        "kind": "power",
                        "params": {
                            "base": base,
                            "exponent": float(exponent),
                            "offset": origin,
                            "direction": direction,
                        },
                        "range": {"min": measured_bounds[0], "max": measured_bounds[1]},
                    },
                )
            )
    if chosen.get("kind") == "exponential" and low > 0.0 and high > 0.0:
        candidates.append(
            (
                "exponential",
                {
                    "kind": "exponential",
                    "params": {"direction": direction},
                    "range": {"min": measured_bounds[0], "max": measured_bounds[1]},
                },
            )
        )

    # A measured table: the plugin's display has no closed form this vocabulary can
    # express (REAPER's own log frequency mapping measured 20 Hz..24 kHz over the
    # sweep and the engine fitted it as a 17-point table), so the samples
    # themselves are declared as the law. It is offered LAST - a closed form that
    # verifies is a better model, because it can be evaluated between the samples
    # instead of interpolated - and only for a strictly monotone series, since an
    # invertible mapping is what a write needs.
    if len(points) >= 3 and monotone_series(points):
        candidates.append(
            (
                "table",
                {
                    "kind": "table",
                    "params": {"points": [[float(x), float(y)] for x, y in points]},
                    "range": {"min": measured_bounds[0], "max": measured_bounds[1]},
                },
            )
        )

    # Ordering: when the artifact reports an integers-only display, the *grid*
    # model is tried first at a tight tolerance, because a grid that is right
    # reproduces the printouts exactly while a continuous law can only sit at the
    # ±half-step resolution limit (measured: `A Octave`'s best continuous law
    # missed by 0.500000005 against a tolerance of exactly 0.5 - a refusal decided
    # by 5e-9, which is not a judgement about the data).
    attempts: list[tuple[str, dict[str, Any], float]] = []
    grid = _integer_grid(points) if evidence.get("integers_only") else None
    if grid is not None:
        for name, law in candidates:
            attempts.append(
                (f"{name}+measured_grid", {**law, "params": {**law["params"], "quantization": grid}}, 1e-6)
            )
    attempts.extend((name, law, tolerance) for name, law in candidates)

    reports: list[dict[str, Any]] = []
    for name, candidate, limit in attempts:
        # What is verified must be what gets declared. Two laws read the range as
        # their *value mapping* (affine, exponential): widening it there would
        # change the values the law produces (measured: a 2.5e-5 relative pad moved
        # an exponential cutoff's prediction by 0.0012 Hz at 9.8 kHz and pushed it
        # past the printed tolerance it had just passed). `power` and `table` use
        # the range only for bounds and clamping, where the pad is what keeps a
        # fitted endpoint from falling an ulp outside.
        declared_range = (
            _padded(measured_bounds)
            if candidate.get("kind") in ("power", "table")
            else measured_bounds
        )
        probe = {
            "type": "number",
            "id": "probe",
            "range": {"min": declared_range[0], "max": declared_range[1]},
            "law": {**candidate, "range": {"min": declared_range[0], "max": declared_range[1]}},
        }
        report = _verify_numeric(probe, points, limit)
        report["kind"] = name
        if grid is not None:
            report["tolerance"] = limit
        reports.append(report)
        if report["ok"]:
            declared = {
                **candidate,
                "range": {"min": declared_range[0], "max": declared_range[1]},
            }
            grid_used = "quantization" in dict(candidate.get("params") or {})
            if not grid_used:
                quantization = _quantization(row, measured_bounds)
                if quantization is not None:
                    declared["params"] = {**declared["params"], "quantization": quantization}
            return {
                "law": declared,
                "reason": None,
                "evidence": {
                    **report,
                    "candidate_order": [item[0] for item in attempts],
                    "engine_fit": chosen.get("kind"),
                    "engine_mapping_reason": (mapping.get("reason") if isinstance(mapping, Mapping) else None),
                    "native_space": "display",
                },
            }
    return {
        "law": {"kind": "none"},
        "reason": "no_verified_law",
        "evidence": {
            "candidates": reports,
            "engine_fit": chosen.get("kind"),
            "engine_mapping_reason": (mapping.get("reason") if isinstance(mapping, Mapping) else None),
            "native_space": "display",
        },
    }


def _enum_members(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The discrete members with their measured bands, unique-ified ids."""
    discrete = row.get("discrete") or {}
    members = list(discrete.get("members") or []) if isinstance(discrete, Mapping) else []
    seen: dict[str, int] = {}
    rows: list[dict[str, Any]] = []
    for ordinal, member in enumerate(members):
        label = str(member.get("label") or "")
        band = member.get("band")
        if not isinstance(band, (list, tuple)) or len(band) != 2:
            return []
        low, high = float(band[0]), float(band[1])
        seen[label] = seen.get(label, 0) + 1
        member_id = label if seen[label] == 1 and label else f"{label or 'state'} ({ordinal})"
        rows.append(
            {
                "id": member_id,
                "label": label,
                "ordinal": ordinal,
                "band": [low, high],
                "unlabelled": bool(member.get("unlabelled")) or not label,
            }
        )
    # a label that appears twice is disambiguated for *both* occurrences, so an id
    # never silently means "the first one"
    for row_ in rows:
        if seen.get(row_["label"], 0) > 1:
            row_["id"] = f"{row_['label']} ({row_['ordinal']})"
    return rows


def label_positions(
    samples: Sequence[tuple[float, str]], members: Sequence[Mapping[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Where each printed label was **measured**, per member, from the samples.

    This is the fix for a defect measured live in P29: the write target used to be
    the arithmetic midpoint of a member's recorded *band*, and for a band whose
    edges came from a sampled grid that midpoint can print the **neighbouring**
    member - writing `"7.19 :1"` on a real compressor left the host printing
    `8.73 :1`, a different member than the one that was named. The band-map check
    only ever exercised the *read* direction (position -> label), so it could not
    see this.

    The target here is derived from measurement instead: the midpoint of the
    positions that actually printed this label (a single position is used as
    itself). Every such target is inside the region the artifact observed that
    label in, and both directions are verified by the caller.

    ``basis`` says which of the three cases produced it:

    - ``measured`` - exactly one position printed this label: the target **is**
      that measurement;
    - ``measured_bracketed`` - several did: the target is their midpoint, i.e.
      strictly between two measurements of the same label (the true transition
      cannot be inside the interval they bound);
    - ``band_first`` - nothing printed it (an extrapolated band): the recorded
      band's own start is used as the best available bound.
    """
    measured: dict[str, list[float]] = {}
    for position, text in samples:
        measured.setdefault(str(text), []).append(float(position))
    out: dict[str, dict[str, Any]] = {}
    for member in members:
        label = str(member.get("label") or "")
        found = sorted(measured.get(label, []))
        if len(found) == 1:
            out[str(member.get("id"))] = {"position": round(found[0], 12), "basis": "measured"}
        elif len(found) > 1:
            midpoint = (found[0] + found[-1]) / 2.0
            out[str(member.get("id"))] = {
                "position": round(midpoint, 12),
                "basis": "measured_bracketed",
                "bracketed_by": [round(found[0], 12), round(found[-1], 12)],
            }
        else:
            band = member.get("band") or [0.0, 0.0]
            out[str(member.get("id"))] = {
                "position": round(float(band[0]), 12),
                "basis": "band_first",
            }
    return out


def _discrete_law(row: Mapping[str, Any], samples: Sequence[tuple[float, str]]) -> dict[str, Any]:
    """A band map for a discrete control, verified in **both** directions."""
    members = _enum_members(row)
    discrete = row.get("discrete") or {}
    if len(members) < 2:
        return {"law": {"kind": "none"}, "reason": "fewer_than_two_measured_members", "evidence": {}}
    targets = label_positions(samples, members)
    positions = [targets[member["id"]]["position"] for member in members]
    switch_points = [round(member["band"][0], 12) for member in members]
    law = {
        "kind": "enum_index",
        "params": {
            "positions": positions,
            "switch_points": switch_points,
            "band_source": "sampled_grid",
            "position_basis": [targets[member["id"]]["basis"] for member in members],
            "band_note": (
                "band edges are the sampled grid positions the host printed each member at; "
                "`positions` are the write targets, placed ON the measured positions of each "
                "label (never on the midpoint of an extrapolated band, which can print a "
                "neighbouring member)"
            ),
        },
    }
    control = {
        "type": "enum",
        "id": "probe",
        "enum_values": [{"id": member["id"]} for member in members],
        "law": law,
    }
    mismatches: list[dict[str, Any]] = []
    for position, text in samples:
        try:
            index = int(V.normalized_to_native(control, position))
            observed = V.from_native(control, index)
        except PluginError as exc:
            mismatches.append({"position": position, "text": text, "error": exc.code})
            continue
        if str(observed) != text:
            mismatches.append({"position": position, "expected": text, "observed": observed})
    # the write direction: the position a member resolves to must read back as that
    # very member. Without this check the law could be right in one direction and
    # wrong in the other, which is how the live wrong-member write got through.
    write_targets: list[dict[str, Any]] = []
    for member, position in zip(members, positions):
        try:
            index = int(V.to_native(control, member["id"]))
            written = float(V.to_normalized(control, index))
            observed = V.from_native(control, int(V.normalized_to_native(control, written)))
            agrees = str(observed) == str(member["id"])
        except PluginError as exc:
            write_targets.append({"member": member["id"], "error": exc.code, "agrees": False})
            continue
        write_targets.append(
            {
                "member": member["id"],
                "target": round(written, 12),
                "reads_back_as": observed,
                "basis": targets[member["id"]]["basis"],
                "agrees": agrees,
            }
        )
    bad_targets = [row_ for row_ in write_targets if not row_.get("agrees")]
    ok = not mismatches and not bad_targets
    return {
        "law": law if ok else {"kind": "none"},
        "reason": None if ok else "band_map_disagrees_with_samples",
        "evidence": {
            "ok": ok,
            "n_members": len(members),
            "n_samples": len(samples),
            "mismatches": mismatches[:4],
            "write_targets_wrong": bad_targets[:4],
            "write_targets": write_targets[:32],
            "discrete_state": str(discrete.get("state") or "unknown"),
            "members_are_a_lower_bound": True,
        },
    }


def _numeric_share(samples: Sequence[tuple[float, str]]) -> float:
    """The share of samples whose printed text is a *number*."""
    if not samples:
        return 0.0
    return len(_numeric_samples(samples)) / len(samples)


def monotone_series(points: Sequence[tuple[float, float]]) -> bool:
    """Strictly monotone in either direction (the weaker test a table needs).

    A table law inverts its samples, so it needs *some* strict monotonicity, not
    the sample-to-sample continuity a closed-form law is held to: `-3, -3, -2, ...`
    is a perfectly invertible ladder even though the drawing is a staircase.
    """
    values = [value for _position, value in points]
    if len(set(values)) < 2:
        return False
    return all(later > earlier for earlier, later in zip(values, values[1:])) or all(
        later < earlier for earlier, later in zip(values, values[1:])
    )


def _strictly_monotone(points: Sequence[tuple[float, float]]) -> tuple[bool, str]:
    """Whether the numeric samples are *strictly* monotone, with the failure named.

    Why this matters (measured on the real artifact, 2026-10-12): `Active Scene`
    prints ``0,0,0,0,0,0,0,0,1,1,1,...`` - two integer states over the whole range.
    Its printed tolerance is 0.5, so a linear law fits it *inside* tolerance and
    looks verified, while every position between the two states would report a
    fraction the host never prints. A continuous law has to be strictly monotone
    in the samples; a display that repeats a value has states, not a continuum,
    and a band map is the model that actually matches it.
    """
    values = [value for _position, value in points]
    if len(values) < 3:
        return False, "fewer_than_three_numeric_samples"
    if len(set(values)) < 3:
        return False, f"only_{len(set(values))}_distinct_numeric_values"
    if all(later > earlier for earlier, later in zip(values, values[1:])):
        return True, "strictly_increasing"
    if all(later < earlier for earlier, later in zip(values, values[1:])):
        return True, "strictly_decreasing"
    repeats = sum(1 for earlier, later in zip(values, values[1:]) if later == earlier)
    return False, f"repeated_numeric_values ({repeats} adjacent equal pair(s))"


def _non_numeric_samples(
    samples: Sequence[tuple[float, str]], limit: int = 4
) -> list[dict[str, Any]]:
    """The samples a numeric law cannot reproduce, named (never silently dropped)."""
    from .discovery.text import parse_display

    rows: list[dict[str, Any]] = []
    for position, text in samples:
        if parse_display(text).numeric:
            continue
        rows.append({"position": position, "text": text})
        if len(rows) >= limit:
            break
    return rows


def materialize_control(row: Mapping[str, Any], observation: Mapping[str, Any] | None) -> dict[str, Any]:
    """One artifact control as a profile-shaped control the shared paths can use.

    The shape is the one `plugins.values` and `plugins.binding` already consume
    (`id`/`label`/`aliases`/`type`/`authority`/`binding`/`range`/`law`/...), so a
    materialized control needs no special-casing downstream.

    **Which model wins when both verify** (measured disagreement, 2026-10-12): the
    discovery engine classes a control as an *enum* when a single sampled position
    printed a non-numeric label - so a dB level whose lowest position prints
    ``-inf dB`` arrives labelled `enum` even though a numeric fit was accepted for
    it. Taking the band map there would quantise a continuous level to 1/16 of its
    range (several dB), which is the opposite of what a mixing request needs. The
    rule here is evidence-based and stated: a **verified numeric law wins whenever
    at least half the samples are numeric**, and the samples it cannot reproduce
    are recorded on the control (``law_evidence.non_numeric_samples``) instead of
    being dropped; otherwise a verified band map is used, and if neither verifies
    the control keeps ``law.kind = "none"`` - readable through the host's own
    formatter, refused for writing with a typed `NoConversion`.
    """
    samples = _samples_of(observation)
    classification = dict(row.get("classification") or {})
    kind = str(classification.get("kind") or "unknown")
    name = str(row.get("name") or f"param_{row.get('index')}")
    control_id = "_".join(normalise(name).split(" ")) or f"param_{row.get('index')}"
    index = int(row.get("index") or 0)
    discrete_family = str((row.get("discrete") or {}).get("family") or "")
    numeric = _numeric_law(row, samples)
    discrete = _discrete_law(row, samples)
    numeric_share = _numeric_share(samples)
    monotone, monotone_note = _strictly_monotone(_numeric_samples(samples))
    numeric_verified = numeric["reason"] is None
    discrete_verified = discrete["reason"] is None
    quantised = "quantization" in dict((numeric.get("law") or {}).get("params") or {})
    tabled = str((numeric.get("law") or {}).get("kind")) == "table"
    if numeric_verified and tabled:
        # The measured-table model: the samples ARE the law, so the verification is
        # exact by construction and the *interpolation between them* is what the
        # model adds. Labelled as such - it is a weaker claim than a closed form.
        analysis, ctype, model = numeric, "number", "table_law"
    elif numeric_verified and quantised:
        # The grid model: the artifact's own integers-only display proved a
        # storage step, and snapping onto it reproduces every sampled text
        # exactly - the strongest model available for a quantised control.
        analysis, ctype, model = numeric, "number", "quantised_numeric"
    elif numeric_verified and monotone:
        analysis, ctype, model = numeric, "number", "numeric_law"
    elif discrete_verified:
        analysis, ctype, model = discrete, "enum", "band_map"
    elif numeric_verified:
        # Verified inside the printed resolution, but the display repeats values
        # and no grid was provable: the law is the best available model and is
        # labelled as one that is only good to the printed resolution.
        analysis, ctype, model = numeric, "number", "numeric_law_at_resolution"
    else:
        # Neither model verifies. The numeric analysis carries the diagnosis
        # (which candidates were tried, how far each missed); a discrete control
        # that has no band map is reported with its own reason instead - unless
        # the display never changed at all, which is the strongest and most
        # useful thing to say about it.
        if numeric["reason"] == "formatter_constant":
            analysis, ctype, model = numeric, "number", "unverified"
        else:
            analysis, ctype, model = (
                (discrete, "enum", "unverified")
                if discrete_family in ("enum", "boolean") or kind in ("enum", "boolean")
                else (numeric, "number", "unverified")
            )
    law = analysis["law"]
    evidence = {
        "strength": ("strong" if analysis["reason"] is None else "weak"),
        "source": f"discovery-artifact:{MATERIALIZATION_VERSION}",
        "classification": {"kind": kind, "confidence": classification.get("confidence")},
        "model": model,
        "law_reason": analysis["reason"],
        "law_evidence": dict(analysis.get("evidence") or {}),
        "native_space": "display",
        "numeric_share": round(numeric_share, 4),
        "numeric_series": monotone_note,
    }
    if model == "table_law":
        evidence["law_evidence"]["table_note"] = (
            "the law is the artifact's own samples (the display has no closed form this vocabulary "
            "can express); values BETWEEN samples are linearly interpolated, which is the model's "
            "one assumption and is stated rather than hidden"
        )
    if model == "quantised_numeric":
        step = dict((law.get("params") or {}).get("quantization") or {}).get("step")
        evidence["law_evidence"]["grid_source"] = (
            f"the artifact reports an integers-only display, and step {step!r} reproduces every "
            "sampled text exactly; the grid is declared (not assumed) so both directions snap "
            "onto values the plugin can hold"
        )
    if model == "band_map" and numeric_verified and not monotone:
        # The numeric law would have verified inside the printed tolerance while
        # being the wrong model; say so, so nobody "fixes" the band map later.
        evidence["law_evidence"]["numeric_model_rejected"] = (
            f"a numeric law fits the printed values within tolerance but the display is not "
            f"continuous ({monotone_note}); the band map is the model that matches the display"
        )
    if model == "numeric_law":
        excluded = _non_numeric_samples(samples)
        if excluded:
            evidence["law_evidence"]["non_numeric_samples"] = excluded
            evidence["law_evidence"]["non_numeric_note"] = (
                "the host prints a non-numeric text at these positions (e.g. '-inf dB' at 0); "
                "the law is a verified model of the numeric samples, and the host's own "
                "formatter is what plugin_read reports for the live value"
            )
    control: dict[str, Any] = {
        "id": control_id,
        "label": name,
        "type": ctype,
        "authority": {"read": "parameter", "write": "parameter"},
        "binding": {
            "index": index,
            "ident": row.get("ident"),
            "live_name": name,
        },
        "module": row.get("module"),
        "unit": row.get("unit"),
        "aliases": [f"#{index}"] + ([str(row["ident"])] if row.get("ident") else []),
        "law": law,
        "evidence": evidence,
        "discrete": bool(ctype == "enum"),
        "model": model,
    }
    if law.get("kind") == "enum_index":
        rows = _enum_members(row)
        control["enum_values"] = [
            {"id": member["id"], "label": member["label"], "band": member["band"]} for member in rows
        ]
        control["range"] = {"min": 0.0, "max": 1.0}
    else:
        # A control can print *labels* and still be modelled numerically (or not at
        # all): measured live (P29), Surge's oscillator volumes print "-inf dB",
        # "-72.00 dB", ... - a ladder whose minimum is not a number a numeric law
        # can express. The labels are recorded evidence, so they are carried here as
        # `labelled_states` **with the position each one was measured at**: a string
        # request naming one is then honoured by writing that position instead of
        # being refused as "not a number".
        labelled = _enum_members(row)
        if len(labelled) >= 2:
            targets = label_positions(samples, labelled)
            control["labelled_states"] = [
                {
                    "id": member["id"],
                    "label": member["label"],
                    "position": targets[member["id"]]["position"],
                    "basis": targets[member["id"]]["basis"],
                }
                for member in labelled
            ]
        bounds = dict(law.get("range") or {})
        if bounds:
            control["range"] = bounds
    steps = dict(row.get("steps") or {})
    if steps.get("reported") is True and isinstance(steps.get("step"), (int, float)):
        control["step"] = float(steps["step"])
    return control


# ---------------------------------------------------------------------------
# The materialized profile
# ---------------------------------------------------------------------------


_PROFILE_CACHE: dict[tuple[str, str], dict[str, Any]] = {}
_PROFILE_STATS: dict[str, int] = {"profiles_built": 0, "profile_hits": 0}
_CACHE_CAP = 4

#: Parsed artifacts, keyed by (path, size, mtime_ns). A 28 MB artifact costs ~3 s
#: to read and parse, and every tool call on the providerless path needs it - so
#: it is parsed once per *file revision*, with the stat triple as the guard: a
#: re-measured artifact (same path, new bytes) has a new key and cannot be served
#: a stale parse. Two entries are enough for a two-plugin workflow.
_ARTIFACT_CACHE: dict[tuple[str, int, int], PreparedArtifact] = {}
_ARTIFACT_CAP = 2
_ARTIFACT_STATS: dict[str, int] = {"artifact_parses": 0, "artifact_hits": 0}


def load_artifact(identity: Mapping[str, Any], *, store: PreparedStore | None = None) -> PreparedArtifact:
    """The stored artifact for an identity, parsed once per file revision."""
    store = store or PreparedStore()
    path = store.path_for(identity)
    try:
        stat = path.stat()
        key = (str(path), int(stat.st_size), int(stat.st_mtime_ns))
    except OSError:
        return store.load(identity)
    found = _ARTIFACT_CACHE.get(key)
    if found is not None:
        _ARTIFACT_STATS["artifact_hits"] += 1
        return found
    artifact = store.load(identity)
    if artifact.ok:
        _ARTIFACT_CACHE[key] = artifact
        _ARTIFACT_STATS["artifact_parses"] += 1
        while len(_ARTIFACT_CACHE) > _ARTIFACT_CAP:
            _ARTIFACT_CACHE.pop(next(iter(_ARTIFACT_CACHE)))
    return artifact


def artifact_stats() -> dict[str, int]:
    return {**_ARTIFACT_STATS, "cached": len(_ARTIFACT_CACHE)}


def materialize_profile(
    artifact: PreparedArtifact,
    *,
    index: ControlIndex | None = None,
) -> dict[str, Any]:
    """The whole-artifact materialization, cached per (path, content digest).

    Building it means one pass over every control plus a law verification per
    numeric control against up to 17 samples - real work, done once. The cache
    key is the artifact's content digest, so a re-measured artifact can never be
    served a previous materialization.
    """
    digest = str(artifact.document.get("content_sha256") or "")
    key = (str(artifact.path or ""), digest or f"{MATERIALIZATION_VERSION}:{artifact.size}")
    found = _PROFILE_CACHE.get(key)
    if found is not None:
        _PROFILE_STATS["profile_hits"] += 1
        return found
    payload = artifact.payload()
    observations = {int(row.get("index") or 0): row for row in artifact.observations()}
    controls = [
        materialize_control(row, observations.get(int(row.get("index") or 0)))
        for row in payload.get("controls") or []
    ]
    nodes = list((payload.get("modules") or {}).get("nodes") or [])
    # Two controls can share a name (ReaComp has two `Attack`s). The ids must stay
    # unique or a request naming one of them would silently address the other, and
    # the rule must be the same one the index applies to the search rows.
    duplicates = INDEX.duplicated_bases(str(control["id"]) for control in controls)
    if duplicates:
        controls = [
            {
                **control,
                "id": INDEX.disambiguated(
                    str(control["id"]), int(control["binding"]["index"]), duplicates
                ),
            }
            for control in controls
        ]
    by_id = {str(control["id"]): control for control in controls}
    if len(by_id) != len(controls):  # pragma: no cover - the rule above makes this unreachable
        raise InvalidValue(
            "the artifact's controls cannot be addressed uniquely; a request would be ambiguous",
            detail={
                "reason": "duplicate_control_ids",
                "disambiguated_bases": sorted(duplicates),
                "controls": len(controls),
                "unique_ids": len(by_id),
            },
        )
    module_rows = []
    for node in nodes:
        members = []
        for index_ in node.get("controls") or []:
            control = next(
                (item for item in controls if item["binding"]["index"] == int(index_)), None
            )
            if control is not None:
                members.append(control["id"])
        module_rows.append(
            {
                "id": node.get("id"),
                "kind": node.get("kind") or "inferred",
                "label": node.get("label"),
                "parent": node.get("parent"),
                "controls": members,
                "props": {
                    "status": node.get("status"),
                    "confidence": node.get("confidence"),
                    "signals": list(node.get("signals") or []),
                },
            }
        )
    writable = sum(1 for control in controls if (control.get("law") or {}).get("kind") != "none")
    profile: dict[str, Any] = {
        "profile_schema_version": f"{MATERIALIZATION_VERSION}/host-scope",
        "provider_id": SCOPE_ID,
        "implementation_revision": REVISION,
        "identity": {
            "fx_name": artifact.document.get("identity", {}).get("fx_name"),
            "class_id": artifact.document.get("identity", {}).get("class_id"),
            "module": artifact.document.get("identity", {}).get("module"),
            "param_count": artifact.document.get("identity", {}).get("param_count"),
            # The host exposes no plugin version on this path, so no version gate
            # is claimed - an empty list here would read as "no version is
            # compatible", so the absence is stated instead of implied.
            "compatible_versions": [],
            "version_basis": "not_exposed_by_host",
        },
        "controls": controls,
        "modules": module_rows,
        "state": {
            "codec": None,
            "derived_leaves": [],
            "structural_keys": [],
            "reason": "a state codec is provider territory; without one there is no document channel",
        },
        "modulation": {
            "supported": False,
            "reason": (
                "routing is a provider codec operation (it edits the state document); the "
                "host-scope path exposes no route vocabulary and refuses route requests"
            ),
            "sources": [],
            "destinations": [],
            "slots": None,
        },
        "limits": [
            "no state/document channel without a provider codec (presets, routes, structural edits)",
            "a control's law is a fit over the sampled grid, verified against those samples",
            "roles in search results are synonym inference, labelled as such",
            "no plugin version gate exists on this path (the host exposes no version for it)",
        ],
        "counts": {
            "controls": len(controls),
            "writable": writable,
            "law_none": len(controls) - writable,
            "modules": len(module_rows),
            "disambiguated_ids": sorted(duplicates),
        },
        "evidence": {
            "artifact": {
                "path": artifact.path,
                "content_sha256": digest,
                "mode": artifact.document.get("mode"),
                "engine_version": artifact.document.get("engine_version"),
                "written_utc": artifact.document.get("written_utc"),
                "observations": len(observations),
            },
            "materialization": MATERIALIZATION_VERSION,
            "index": (index or ControlIndex(payload)).census(),
        },
        "index": index,
    }
    _PROFILE_CACHE[key] = profile
    _PROFILE_STATS["profiles_built"] += 1
    while len(_PROFILE_CACHE) > _CACHE_CAP:
        _PROFILE_CACHE.pop(next(iter(_PROFILE_CACHE)))
    return profile


def materialization_stats() -> dict[str, int]:
    return {**_PROFILE_STATS, "cached": len(_PROFILE_CACHE)}


# ---------------------------------------------------------------------------
# The provider-shaped object the four tools use
# ---------------------------------------------------------------------------


@dataclass
class HostScopeProvider:
    """The provider *shaped* object for a host-scope answer.

    It implements the subset of the provider contract the shared paths actually
    call - `matches`/`identify`, `normalize_request`, `write_order`,
    `verification_policy`, `capture_scope`, `dependencies`, `limits` - and
    refuses the rest (state, routes) with typed errors instead of answering
    something it cannot verify.

    ``index_addressed_parameters`` is the one declared *capability flag* the
    transaction reads: on this path parameters are addressed by the measured
    **index** with a live name check inside the write hop, because that is the
    durable address the artifact measured (`binding.index`), while the idents an
    artifact records are informative only - re-binding them costs a full ident
    table read of the whole plugin on every write.
    """

    artifact: PreparedArtifact | None = None
    index: ControlIndex | None = None
    profile: dict[str, Any] | None = None

    id: str = SCOPE_ID
    implementation_revision: str = REVISION
    index_addressed_parameters: bool = True

    # -- identity ---------------------------------------------------------

    def matches(self, facts: Any) -> bool:
        return True

    def identify(self, facts: Any) -> dict[str, Any]:
        return HostScope().identify(facts)

    def artifact_paths(self) -> list[str]:
        return []

    def limits(self) -> list[str]:
        return list((self.profile or {}).get("limits") or [])

    def build_profile(self, identity: Mapping[str, Any]) -> dict[str, Any]:
        """Materialize from the stored artifact (no host access beyond identity).

        A missing or refused artifact is a typed refusal carrying the remedy -
        never an empty profile that would read as "this plugin has no controls".
        """
        artifact = self.artifact
        if artifact is None or not artifact.ok:
            raise DiscoveryArtifactMissing(
                "no prepared discovery artifact is available for this instance, so the "
                "providerless path has nothing verified to work from",
                detail={
                    "remedy": "call plugin_prepare with this instance (mode='prepare') once",
                    "artifact_status": (artifact.status if artifact is not None else "missing"),
                    "artifact_rule": (artifact.rule if artifact is not None else None),
                    "artifact_path": (artifact.path if artifact is not None else None),
                    "identity": dict(identity),
                },
            )
        return materialize_profile(artifact, index=self.index)

    def adopt_profile(self, profile: dict[str, Any]) -> None:
        self.profile = profile

    # -- request normalization -------------------------------------------

    def normalize_request(self, profile: dict[str, Any], request: dict[str, Any]) -> ResolvedRequest:
        """Resolve a controls-only request; refuse every other channel loudly.

        The request vocabulary is the one the provider path already speaks
        (`controls` plus optional label/state/routes keys), so a caller does not
        have to learn a second shape - but a channel this path cannot honour is a
        typed problem row, never a silent no-op.
        """
        resolved = ResolvedRequest()
        requested = request.get("controls")
        if requested is None:
            requested = {}
        if not isinstance(requested, Mapping):
            raise InvalidValue(
                "`request.controls` must be an object of control -> value",
                detail={"type": type(requested).__name__},
            )
        controls = list(profile.get("controls") or [])
        for name, value in requested.items():
            control = _control_by_name(controls, name)
            if control is None:
                resolved.problems.append(
                    {
                        "control": str(name),
                        "code": "UnsupportedControl",
                        "message": f"unknown control {name!r}",
                        "detail": {"suggestions": _suggest(controls, name)},
                    }
                )
                continue
            outcome = RV.resolve_control_value(control, value, values_module=V, coerce=_coerce)
            if "problem" in outcome:
                resolved.problems.append(outcome["problem"])
                continue
            row = outcome["row"]
            row["binding"] = dict(row.get("binding") or {})
            resolved.controls[str(control["id"])] = row
        for key, reason in (
            ("state", "the state/document channel needs a provider codec"),
            ("routes_create", "route creation needs a provider codec"),
            ("routes_update", "route updates need a provider codec"),
            ("routes_remove", "route removal needs a provider codec"),
        ):
            payload = request.get(key)
            if payload:
                resolved.problems.append(
                    {
                        "channel": key,
                        "code": "UnsupportedProvider",
                        "message": f"{key!r} is not available on the providerless path: {reason}",
                        "detail": {
                            "reason": "channel_unavailable_without_provider",
                            "accepted_channels": ["controls"],
                        },
                    }
                )
        for key in sorted(set(request) - _KNOWN_REQUEST_KEYS):
            resolved.problems.append(
                {
                    "channel": key,
                    "code": "InvalidValue",
                    "message": f"unknown request key {key!r}",
                    "detail": {"accepted": sorted(_KNOWN_REQUEST_KEYS)},
                }
            )
        return resolved

    def convert_depth(self, profile: dict[str, Any], destination: str, depth: Any) -> float:
        raise NoConversion(
            "semantic depth conversion is provider knowledge; the providerless path exposes "
            "no route vocabulary to convert a depth against",
            detail={"destination": destination, "reason": "no_route_vocabulary"},
        )

    def dependencies(self) -> Any:
        from .provider import DependencyRuleSet

        return DependencyRuleSet(rules=[])

    def codec(self) -> Any:
        """There is no state codec on this path - stated, not faked.

        Returning `None` (rather than a codec that raises later) is safe
        **because the transaction never reaches a codec without a state stage**:
        `write_order()` declares no state stage, every state read/decode/restore
        site is guarded by it, and a state/routes request is refused in
        `normalize_request` and again in `_build_units`.
        """
        return None

    def write_order(self) -> tuple[str, ...]:
        """Parameters only, then verify: there is no state stage to order."""
        return ("parameters", "verify")

    def verification_policy(self) -> VerificationPolicy:
        return VerificationPolicy(
            tolerances={"parameter.normalized": 1e-3},
            comparators={"parameter.normalized": "abs_le"},
        )

    def capture_scope(self) -> CaptureScope:
        """The planned parameters only - captured complete-or-refuse for that set.

        `full_vector` on a 2858-parameter plugin means two whole-table reads per
        transaction (measured: 2.9 s for names, 0.32 s for values, per hop, and
        the idents table again for lookup). The parameter channel this path can
        write is exactly the set the request names, so that set is what is
        captured - and the restore path restores that set.
        """
        return CaptureScope(
            channels=["parameters"],
            parameters={"scope": "planned", "ident_required": False},
            restore_order=["parameters"],
            verify_order=["parameters"],
        )

    def document_version(self, document: Any) -> str | None:
        return None

    def unevaluated_rules(self) -> list[dict[str, Any]]:
        return []


_KNOWN_REQUEST_KEYS = {
    "controls",
    "state",
    "routes_create",
    "routes_update",
    "routes_remove",
    "label",
    # the batch's unresolved-control policy (`refuse` default, `skip` opt-in);
    # validated in the transaction, listed here so it is not "an unknown key"
    "on_unresolved",
}


def _control_by_name(controls: Sequence[Mapping[str, Any]], name: Any) -> dict[str, Any] | None:
    """Resolve an address: declared name, control id, ``#index``, or an index."""
    text = str(name).strip()
    if text.startswith("#"):
        text = text[1:]
    if text.isdigit():
        wanted_index = int(text)
        for control in controls:
            if int((control.get("binding") or {}).get("index") or -1) == wanted_index:
                return dict(control)
    wanted = B.normalise_name(name)
    for control in controls:
        if wanted in B.declared_names(control):
            return dict(control)
    return None


def _suggest(controls: Sequence[Mapping[str, Any]], name: Any, limit: int = 3) -> list[str]:
    wanted = B.normalise_name(name)
    tokens = [token for token in wanted.replace("-", "_").split("_") if token]
    scored: list[tuple[int, str]] = []
    for control in controls:
        control_id = str(control.get("id", ""))
        score = sum(1 for token in tokens if token and token in control_id.lower())
        if score:
            scored.append((score, control_id))
    scored.sort(key=lambda row: (-row[0], row[1]))
    return [control_id for _score, control_id in scored[:limit]]


def _coerce(control: Mapping[str, Any], value: Any) -> Any:
    """The one coercion this path performs: a boolean word for a 2-member enum.

    A plugin prints "Off"/"On" while a caller may say ``true``. The coercion is
    deliberately narrow: it only fires when the control's own measured members
    are two and its law maps them in ascending order, so it can never reinterpret
    a value on a control with more states.
    """
    members = [str(item.get("id")) for item in control.get("enum_values") or []]
    if len(members) != 2 or isinstance(value, str):
        return value
    if isinstance(value, bool):
        return members[1] if value else members[0]
    return value


# ---------------------------------------------------------------------------
# Project-wide enumeration (P29): the host's own slot list, provider or not
# ---------------------------------------------------------------------------


def enumerate_project(
    bridge: Any,
    *,
    selector: Mapping[str, Any] | None = None,
    providers: Sequence[Any] = (),
    include_readiness: bool = False,
) -> dict[str, Any]:
    """Every FX slot the host reports, with the identity facts and a usable ref.

    Why this belongs to the *host bridge* and not to a provider: REAPER itself
    answers all of it - the track list, each track's FX count, and per slot the FX
    name, the class id and module from the state chunk, and the parameter count.
    A provider adds *semantics* (laws, routes, a codec); requiring one to *list*
    what is loaded made every new plugin a configuration task (the defect P28's
    selector-only fallback only half-closed).

    Freshness and honesty:

    - a slot whose facts cannot be read (a stale index, a slot removed between the
      track read and the slot read) does **not** silently disappear: it is reported
      in ``refused`` with the host's own reason, because "not listed" and "could not
      be read" are different answers;
    - the selector rule is the provider path's own (`resolve.matches_selector`), so
      `{"track_index": 2}`, `{"track_name": "Bass"}` and `{"name": "<fx-name part>"}`
      mean the same thing in both scopes. (No plugin name is written here on
      purpose: this module is on the provider-agnostic side of the L0 boundary,
      and that checker bans a provider identifier anywhere in it.)
    - a row carries the **same object shape** the other tools accept as their
      `instance` argument (host/location/provider/identity/display), so a caller can
      pass back the row it just read instead of re-deriving an address;
    - `artifact` file facts are cheap (path, presence, size); the *verdict* on
      whether a slot is writable needs the artifact parsed, so it is opt-in
      (`include_readiness=True`) and reported per row when asked for.
    """
    from . import resolve as RS
    from .discovery.store import PreparedStore

    tracks = bridge.list_tracks()
    wanted_track: int | None = None
    if isinstance(selector, Mapping) and selector.get("track_index") is not None:
        try:
            wanted_track = int(selector["track_index"])
        except (TypeError, ValueError):
            wanted_track = None

    rows: list[dict[str, Any]] = []
    refused: list[dict[str, Any]] = []
    slots_scanned = 0
    tracks_scanned = 0
    service = HostScope()
    store = PreparedStore() if include_readiness else None
    for track in tracks:
        track_index = int(track.track_index)
        if wanted_track is not None and track_index != wanted_track:
            continue
        tracks_scanned += 1
        for fx_index in range(int(track.n_fx)):
            slots_scanned += 1
            try:
                facts = bridge.fx_facts(track_index, fx_index)
            except PluginError as exc:
                refused.append(
                    {
                        "location": {"track_index": track_index, "fx_index": fx_index},
                        "track_name": track.name,
                        "code": exc.code,
                        "message": exc.message,
                        "detail": dict(exc.detail or {}),
                        "note": (
                            "the host has a slot at this index whose identity could not be read; "
                            "it is reported rather than dropped, and it is not offered as a target"
                        ),
                    }
                )
                continue
            if not RS.matches_selector(facts, track.name, dict(selector or {})):
                continue
            claiming = None
            for provider in providers:
                matches = getattr(provider, "matches", None)
                try:
                    if callable(matches) and matches(facts):
                        claiming = str(getattr(provider, "id", ""))
                        break
                except PluginError:  # a provider that cannot judge does not claim
                    continue
            provider_id = claiming or SCOPE_ID
            identity = service.identify(facts)
            location = RS.facts_location(facts)
            row: dict[str, Any] = {
                "host": "reaper",
                "location": location,
                "provider": provider_id,
                "providerless": claiming is None,
                "display": f"{facts.fx_name} @ track {track_index + 1} fx {fx_index + 1}",
                "fx_name": facts.fx_name,
                "module": facts.module,
                "class_id": facts.class_id,
                "param_count": int(facts.param_count),
                "fx_count_on_track": int(facts.fx_count),
                "track_name": facts.track_name,
                "identity": identity,
                "artifact_file": _artifact_file_facts(identity),
            }
            if store is not None:
                artifact = load_artifact(identity)
                row["artifact"] = {
                    "status": artifact.status,
                    "mode": artifact.document.get("mode"),
                    "written_utc": artifact.document.get("written_utc"),
                    "path": artifact.path,
                    "reason": artifact.reason,
                }
                row["ready"] = artifact.ok
                row["remedy"] = (
                    None
                    if artifact.ok
                    else "call plugin_prepare with this instance (mode='prepare') to measure it once"
                )
            rows.append(row)
    return {
        "instances": rows,
        "refused": refused,
        "slots_scanned": slots_scanned,
        "tracks_scanned": tracks_scanned,
        "readiness_included": bool(include_readiness),
    }


def _artifact_file_facts(identity: Mapping[str, Any]) -> dict[str, Any]:
    """Whether an artifact *file* exists for this identity - without parsing it.

    A listing must stay cheap: parsing the document is what costs (a 28 MB
    artifact takes seconds), and the interrogatives a caller actually has in a
    listing are "is there something on disk" and "how big". The verdict on
    usability comes from `plugin_prepare(reuse)` or a row's `artifact` block when
    readiness is requested.
    """
    from .discovery.store import PreparedStore

    path = PreparedStore().path_for(identity)
    try:
        stat = path.stat()
    except OSError:
        return {"path": str(path), "present": False, "verdict_note": "no artifact file for this identity"}
    return {
        "path": str(path),
        "present": True,
        "bytes": int(stat.st_size),
        "verdict_note": (
            "presence is not usability: call plugin_prepare(instance=...) mode='reuse' (or ask "
            "for readiness) for the verdict"
        ),
    }


# ---------------------------------------------------------------------------
# Resolution: instance + artifact -> a ProfileResolution-shaped answer
# ---------------------------------------------------------------------------


def artifact_ready(bridge: Any, selector: dict[str, Any] | None) -> dict[str, Any]:
    """Read-only readiness check for a slot: does an artifact exist, and for what."""
    from . import resolve as RS

    found = RS.iter_instances(bridge, HostScope(), selector=selector)
    if len(found) != 1:
        return {"status": "no_instance" if not found else "ambiguous", "matches": len(found)}
    facts, _track_name = found[0]
    identity = HostScope().identify(facts)
    artifact = load_artifact(identity)
    return {
        "status": artifact.status,
        "rule": artifact.rule,
        "reason": artifact.reason,
        "path": artifact.path,
        "identity": identity,
        "mode": artifact.document.get("mode"),
        "written_utc": artifact.document.get("written_utc"),
        "engine_version": artifact.document.get("engine_version"),
    }


def readiness(reason: str | None) -> dict[str, Any]:
    """How much of the tool surface an artifact's analysis state supports."""
    return {
        "read": True,
        "search": True,
        "write": reason is None,
        "write_blocker": reason,
    }


def resolve_discovery_instance(
    bridge: Any,
    *,
    selector: dict[str, Any] | None = None,
    registry: Any = None,
) -> tuple[ResolvedInstance, PreparedArtifact, ControlIndex, dict[str, Any]]:
    """Resolve one slot, load its artifact, materialize the profile (no writes)."""
    from . import resolve as RS

    scope = HostScope()
    instance = RS.resolve_one(bridge, scope, selector=selector, registry=registry)
    identity = dict(instance.ref.identity)
    artifact = load_artifact(identity)
    if not artifact.ok:
        raise DiscoveryArtifactMissing(
            "no usable prepared discovery artifact for this instance",
            detail={
                "remedy": (
                    "call plugin_prepare for this instance once (mode='prepare'); the providerless "
                    "tools serve a stored, verified measurement rather than sweeping the plugin"
                ),
                "artifact_status": artifact.status,
                "artifact_rule": artifact.rule,
                "artifact_reason": artifact.reason,
                "artifact_path": artifact.path,
                "identity": identity,
            },
        )
    index = load_index(artifact.payload(), cache_key=(str(artifact.path), str(artifact.document.get("content_sha256"))))
    provider = HostScopeProvider(artifact=artifact, index=index)
    profile = materialize_profile(artifact, index=index)
    provider.adopt_profile(profile)
    return instance, artifact, index, profile


def discovery_resolution(
    bridge: Any,
    *,
    selector: dict[str, Any] | None = None,
    registry: Any = None,
) -> tuple[HostScopeProvider, Any, Any]:
    """The tool-facing resolution: `(provider, ProfileResolution, artifact)`.

    A real `ProfileResolution` is returned (not a look-alike) so every consumer -
    `_meta`, `require_writable`, `plugin_describe` - keeps one code path. Its
    `build`/`cache` are host-scope records: the revision and the artifact's
    content digest are its key material, and the cache status says `discovery`
    (this path never reads or writes the disposable profile cache).
    """
    from . import resolve as RS

    instance, artifact, index, profile = resolve_discovery_instance(
        bridge, selector=selector, registry=registry
    )
    digest = str(artifact.document.get("content_sha256") or "")
    identity = dict(instance.ref.identity)
    build = ProfileBuild(
        profile=profile,
        validation=ProfileValidation(ok=True),
        cacheable=False,
        provider_revision=REVISION,
        provider_artifact_digest=digest,
        source_digest=hashlib.sha256(
            f"{MATERIALIZATION_VERSION}:{digest}".encode("utf-8")
        ).hexdigest(),
        from_cache=False,
    )
    cache = CacheStatus(
        key=instance.ref.location and str(digest)[:24] or "",
        source="discovery",
        path=artifact.path,
        prepared=True,
        built=False,
    )
    resolution = RS.ProfileResolution(
        instance=instance,
        identity=identity,
        profile=profile,
        build=build,
        cache=cache,
        missing_identity=[],
        version_refusal_reason=None,
    )
    return HostScopeProvider(artifact=artifact, index=index, profile=profile), resolution, artifact


def writable_blockers(profile: Mapping[str, Any]) -> list[str]:
    """Controls a caller cannot write, by reason (a readiness census)."""
    blockers: dict[str, int] = {}
    for control in profile.get("controls") or []:
        law = (control.get("law") or {}).get("kind")
        if law == "none":
            blockers["no_verified_law"] = blockers.get("no_verified_law", 0) + 1
    return [f"{reason}:{count}" for reason, count in sorted(blockers.items())]
