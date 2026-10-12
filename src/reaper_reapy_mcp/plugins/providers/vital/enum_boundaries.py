"""Measured enum band boundaries: the adaptive read-only channel's artifact.

What this adds to the P22 picture. The profile already carried band boundaries,
but from the **registry's own sweep** - a change-point list sampled on a grid
(17 or 257 points). A recorded change point is therefore only an *upper bound* on
the true transition, with the sampling step as its resolution: measured
2026-10-11, 5,320 of 5,671 internal boundaries sit up to 3.35e-3 away from the
uniform arithmetic, and every one of those distances is smaller than the sweep's
own step, so the sweep cannot say which of the two is right.

The host formatter can, because it accepts an arbitrary double and *formats* it:

- ``TrackFX_FormatParamValueNormalized(track, fx, index, value, "", 512)`` prints
  what the host would display for ``value`` **without moving the parameter**, so
  the transition between two printed states can be bracketed to any tolerance and,
  where a rational candidate is confirmed against its predecessor double, *proved
  exact*. A preparation probe (``tests/plugins/live/probe_p23_enum_boundaries.py``)
  does that walk once and writes one artifact; this module reads it, verifies it,
  and merges only what it can attribute.

The evidence levels, and what each one licenses:

``exact_at``
    ``format(r)`` is the upper member **and** ``format(prev_double(r))`` is the
    lower one: ``r`` is the smallest IEEE double the host formats as the upper
    member. The claim involves no tolerance.
``exact``
    bisection stopped with ``hi == nextafter(lo)`` - again adjacent doubles.
``interval``
    the transition is inside ``(lo, hi]`` with ``hi - lo <= tolerance``.
``coarse_interval``
    bracketed wider than the tolerance because the call budget ran out.
``confirmed_within``
    the profile's own boundary reproduced both sides within ``epsilon``; weaker
    than the three above (it does not locate the transition, only that the
    existing boundary is consistent with the host's display around it).

A control whose transitions are not **all** on a measured level is refused as a
whole: a band map with one guessed boundary among measured ones is exactly the
kind of half-measurement the layer must not carry. A refused control keeps the
geometry it had - never a partial replacement.

Nothing here writes to the plugin; nothing here runs at import time; the artifact
is a *source* (its hash is cache-key material), and a malformed one is refused by
its own loader, with the reason travelling into the profile.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from ...errors import IncompatibleSettings

__all__ = [
    "ENUM_BOUNDARIES_KIND",
    "ENUM_BOUNDARIES_METHOD_VERSION",
    "ENUM_BOUNDARIES_ENV",
    "DEFAULT_ENUM_BOUNDARIES_FILENAME",
    "MEASURED_TRANSITION_STATES",
    "TRANSITION_STATES",
    "EnumBoundaries",
    "default_enum_boundaries_path",
    "resolve_enum_boundaries_path",
    "load_enum_boundaries",
    "enum_boundaries_source_row",
    "apply_enum_boundaries",
]

#: The artifact's self-description: an unknown kind/version is refused, never guessed.
ENUM_BOUNDARIES_KIND = "vital-enum-boundaries"
ENUM_BOUNDARIES_METHOD_VERSION = 1

#: Explicit path override; a set-but-empty value is a configuration error.
ENUM_BOUNDARIES_ENV = "REAPER_MCP_VITAL_ENUM_BOUNDARIES"

DEFAULT_ENUM_BOUNDARIES_FILENAME = "vital_enum_boundaries.json"

#: Every state a transition record may carry (the probe's vocabulary) ...
TRANSITION_STATES = (
    "exact_at",
    "exact",
    "interval",
    "coarse_interval",
    "confirmed_within",
    # "the host prints a value strictly between these two members": the control's
    # display is a ramp sampled on the sweep's grid, so it has no band structure
    # (measured on 20 Vital controls, 2026-10-11 - see the probe's
    # `_classify_third_label`).
    "continuous_display",
    "foreign_label",
    "unresolved",
)

#: ... and the ones a profile boundary may be *built* from. ``continuous_display``,
#: ``foreign_label``, ``unresolved`` and a missing record keep the control's
#: existing geometry: a measurement that saw a ramping display, a foreign label, or
#: none, has not established a boundary.
MEASURED_TRANSITION_STATES = ("exact_at", "exact", "interval", "coarse_interval", "confirmed_within")

#: Bounds, so a malformed or hostile artifact cannot become unbounded profile
#: content (the same discipline the calibration loader follows).
_MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
_MAX_CONTROLS = 8192
_MAX_MEMBERS = 4096
_MAX_TEXT_CHARS = 256

#: The tolerance a boundary may carry and still be *attached*: a measurement that
#: agrees with the profile's own arithmetic to within this changes no decision, so
#: the profile keeps its byte-identical geometry and the agreement is recorded
#: instead. It is the artifact's own `method.tolerance` that bounds the evidence.
_DEFAULT_AGREEMENT = 1e-9


def _fail(reason: str, path: str, *, control: str | None = None, extra: Mapping[str, Any] | None = None) -> IncompatibleSettings:
    detail: dict[str, Any] = {"path": path, "artifact": ENUM_BOUNDARIES_KIND}
    if control is not None:
        detail["control"] = control
    if extra:
        detail.update(dict(extra))
    return IncompatibleSettings(f"the enum-boundary artifact is unusable: {reason}", detail=detail)


def default_enum_boundaries_path(env: Mapping[str, str] | None = None) -> Path:
    """Where a preparation run writes the artifact: beside the calibration artifact."""
    environ = os.environ if env is None else env
    root = environ.get("LOCALAPPDATA") or environ.get("XDG_CACHE_HOME")
    base = Path(root) if root else Path.home() / ".cache"
    return base / "reaper_reapy_mcp" / "calibration" / DEFAULT_ENUM_BOUNDARIES_FILENAME


def resolve_enum_boundaries_path(
    explicit: str | Path | None = None, env: Mapping[str, str] | None = None
) -> str | None:
    """Explicit path, else the environment override, else the default location.

    Same resolution rules as the registry and the calibration artifact (08 §6.1):
    a **set** value is used verbatim after normalisation (no ``~`` expansion, so a
    typo cannot silently resolve elsewhere) and an empty value is a configuration
    error, not "unset". ``None`` means "not configured at all"; the default
    location is returned even when the file does not exist, because a preparation
    run has to know where to write.
    """
    if explicit is not None:
        return os.path.normpath(os.path.abspath(str(explicit)))
    environ = os.environ if env is None else env
    override = environ.get(ENUM_BOUNDARIES_ENV)
    if override is not None:
        if not str(override).strip():
            raise IncompatibleSettings(
                f"{ENUM_BOUNDARIES_ENV} is set to an empty value; a literal path is required",
                detail={"variable": ENUM_BOUNDARIES_ENV, "value": str(override), "resolved": None},
            )
        return os.path.normpath(os.path.abspath(str(override)))
    return str(default_enum_boundaries_path(env))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class EnumBoundaries:
    """A loaded, shape-checked enum-boundary artifact."""

    path: str
    sha256: str
    method_version: int
    tolerance: float
    identity: dict[str, Any]
    controls: dict[str, dict[str, Any]]
    counts: dict[str, Any]
    proof: dict[str, Any]
    host: dict[str, Any]

    def matches_identity(self, identity: Mapping[str, Any]) -> bool:
        """Whether this artifact was measured on the instance being profiled."""
        for name in ("fx_name", "class_id", "module", "param_count"):
            if self.identity.get(name) != identity.get(name):
                return False
        return True

    def source_row(self) -> dict[str, Any]:
        return {"file": Path(self.path).name, "sha256": self.sha256}


def enum_boundaries_source_row(path: str | Path) -> dict[str, Any]:
    """The `{file, sha256}` row for the artifact (hash-only, no parse)."""
    candidate = Path(path)
    return {"file": candidate.name, "sha256": _sha256_file(candidate)}


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def load_enum_boundaries(path: str | Path) -> EnumBoundaries:
    """Read and shape-check the artifact; every failure is a typed refusal."""
    resolved = os.path.normpath(os.path.abspath(str(path)))
    candidate = Path(resolved)
    try:
        size = candidate.stat().st_size
    except OSError as exc:
        raise _fail(f"the file cannot be read: {type(exc).__name__}: {exc}", resolved) from exc
    if size > _MAX_ARTIFACT_BYTES:
        raise _fail(f"the artifact is {size} bytes, beyond the {_MAX_ARTIFACT_BYTES}-byte bound", resolved)
    try:
        payload = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise _fail(f"the file does not parse as UTF-8 JSON: {exc}", resolved) from exc
    if not isinstance(payload, dict):
        raise _fail("the artifact is not a JSON object", resolved)
    if payload.get("kind") != ENUM_BOUNDARIES_KIND:
        raise _fail(f"kind {payload.get('kind')!r} is not {ENUM_BOUNDARIES_KIND!r}", resolved)
    version = payload.get("method_version")
    if version != ENUM_BOUNDARIES_METHOD_VERSION:
        raise _fail(
            f"method_version {version!r} is not {ENUM_BOUNDARIES_METHOD_VERSION!r}; a different "
            "producer must be re-run rather than mis-read",
            resolved,
        )
    identity = payload.get("identity")
    if not isinstance(identity, dict) or not identity:
        raise _fail("the identity block is missing", resolved)
    method = payload.get("method")
    tolerance = _finite_number((method or {}).get("tolerance")) if isinstance(method, dict) else None
    if tolerance is None or not (0.0 < tolerance < 1.0):
        raise _fail(
            f"the method block's tolerance is missing or unusable: "
            f"{None if not isinstance(method, dict) else method.get('tolerance')!r}",
            resolved,
        )
    controls = payload.get("controls")
    if not isinstance(controls, dict) or not controls:
        raise _fail("the controls block is missing or empty", resolved)
    if len(controls) > _MAX_CONTROLS:
        raise _fail(f"the artifact carries {len(controls)} controls, beyond the bound", resolved)

    checked: dict[str, dict[str, Any]] = {}
    for control_id, entry in controls.items():
        name = str(control_id)
        if not isinstance(entry, dict):
            raise _fail(f"control {name!r} is not an object", resolved, control=name)
        members = entry.get("members")
        if (
            not isinstance(members, list)
            or len(members) < 2
            or len(members) > _MAX_MEMBERS
            or any(not isinstance(member, str) or not member for member in members)
        ):
            raise _fail(
                f"control {name!r} does not carry a usable member list ({len(members) if isinstance(members, list) else None} entries)",
                resolved,
                control=name,
            )
        if any(len(member) > _MAX_TEXT_CHARS for member in members):
            raise _fail(f"control {name!r} has a member id longer than {_MAX_TEXT_CHARS} chars", resolved, control=name)
        transitions = entry.get("transitions")
        if not isinstance(transitions, list) or len(transitions) != len(members) - 1:
            raise _fail(
                f"control {name!r} carries {len(transitions) if isinstance(transitions, list) else None} "
                f"transitions for {len(members)} members",
                resolved,
                control=name,
            )
        rows: list[dict[str, Any]] = []
        previous_hi = -1.0
        for position, transition in enumerate(transitions, start=1):
            if not isinstance(transition, dict):
                raise _fail(f"control {name!r} transition {position} is not an object", resolved, control=name)
            state = str(transition.get("state"))
            if state not in TRANSITION_STATES:
                raise _fail(
                    f"control {name!r} transition {position} has state {state!r}, not one of {TRANSITION_STATES}",
                    resolved,
                    control=name,
                )
            index = transition.get("i")
            if isinstance(index, bool) or not isinstance(index, int) or index != position:
                raise _fail(
                    f"control {name!r} transition {position} declares i={index!r}",
                    resolved,
                    control=name,
                )
            if transition.get("lower_member") != members[position - 1] or transition.get(
                "upper_member"
            ) != members[position]:
                raise _fail(
                    f"control {name!r} transition {position} does not name the members it separates",
                    resolved,
                    control=name,
                )
            hi = _finite_number(transition.get("hi"))
            lo = _finite_number(transition.get("lo"))
            measured_state = state in MEASURED_TRANSITION_STATES
            if measured_state:
                if hi is None or lo is None or not (0.0 <= lo <= hi <= 1.0):
                    raise _fail(
                        f"control {name!r} transition {position} carries an unusable bracket "
                        f"[{transition.get('lo')!r}, {transition.get('hi')!r}]",
                        resolved,
                        control=name,
                    )
                # a *measured* bracket must advance: two boundaries at the same value
                # would give a member an empty band, which no display can have
                if hi <= previous_hi:
                    raise _fail(
                        f"control {name!r} transition {position} does not advance past {previous_hi!r}",
                        resolved,
                        control=name,
                    )
                previous_hi = max(previous_hi, hi)
            rows.append(
                {
                    "i": position,
                    "state": state,
                    "lo": lo,
                    "hi": hi,
                    "lower_member": str(transition.get("lower_member")),
                    "upper_member": str(transition.get("upper_member")),
                    "value": _finite_number(transition.get("value")),
                }
            )
        tail = entry.get("tail")
        tail_row = None
        if isinstance(tail, dict) and str(tail.get("state")) == "unlabelled_region":
            hi = _finite_number(tail.get("hi"))
            lo = _finite_number(tail.get("lo"))
            if hi is not None and lo is not None and 0.0 <= lo <= hi <= 1.0:
                tail_row = {"lo": lo, "hi": hi, "text": str(tail.get("text"))[:_MAX_TEXT_CHARS]}
        interiors = entry.get("band_interiors")
        checked[name] = {
            "index": entry.get("index"),
            "members": [str(member) for member in members],
            "transitions": rows,
            "tail": tail_row,
            #: The probe's interior verification travels with the record: the merge
            #: refuses a band map whose interiors the measurement did not confirm.
            "band_interiors": {
                "checked": int(interiors.get("checked") or 0),
                "verified": int(interiors.get("verified") or 0),
                "mismatches": [
                    {"member": str(row.get("member")), "text": str(row.get("text"))[:64]}
                    for row in (interiors.get("mismatches") or [])
                    if isinstance(row, dict)
                ][:6],
            }
            if isinstance(interiors, dict)
            else None,
            "positions_checked": [
                {
                    "member": str(row.get("member")),
                    "position": _finite_number(row.get("position")),
                    "ok": bool(row.get("ok")),
                }
                for row in (entry.get("positions_checked") or [])
                if isinstance(row, dict)
            ],
        }

    counts = payload.get("counts") if isinstance(payload.get("counts"), dict) else {}
    proof = payload.get("read_only_proof") if isinstance(payload.get("read_only_proof"), dict) else {}
    host = payload.get("host") if isinstance(payload.get("host"), dict) else {}
    return EnumBoundaries(
        path=resolved,
        sha256=_sha256_file(candidate),
        method_version=int(version),
        tolerance=float(tolerance),
        identity=dict(identity),
        controls=checked,
        counts=dict(counts),
        proof=dict(proof),
        host=dict(host),
    )


def _band_ends(record: dict[str, Any]) -> list[float] | None:
    """``[start of member 0, ..., end of the last member]`` - or ``None``.

    The measured transitions give the internal band starts; the ends are the
    domain's 0.0 and either 1.0 or the measured start of an unlabelled region
    (measured: ``Filter 1 Style``'s sixth state prints an empty string from
    0.5625 up, so the last *named* member's band ends there).
    """
    members = record["members"]
    ends = [0.0]
    for row in record["transitions"]:
        if row["state"] not in MEASURED_TRANSITION_STATES:
            return None
        if row["hi"] <= ends[-1]:
            return None
        ends.append(float(row["hi"]))
    top = 1.0
    tail = record.get("tail")
    if isinstance(tail, dict) and tail.get("hi", 0.0) > ends[-1]:
        top = float(tail["hi"])
    if top < ends[-1] or len(ends) != len(members):
        return None
    ends.append(top)
    return ends


def _midpoint(low: float, high: float) -> float:
    """A value strictly inside ``[low, high)`` - exactly, never after rounding.

    Rounding a midpoint to a fixed number of decimals is how a *write target* escapes
    the band it was derived from: for a band narrower than the rounding step the
    rounded value can land in the neighbouring band, and the read path would then
    report a member the host never prints for that value (found by the independent
    review: two transitions two ULP apart produce a band 2 ULP wide). The value is
    therefore exact, and clamped into the band when the band's own width is smaller
    than the spacing of representable values at that magnitude.
    """
    if high > low:
        middle = low + (high - low) / 2.0
        if low <= middle < high:
            return middle
        clamped = min(max(middle, low), math.nextafter(high, -math.inf))
        if low <= clamped < high:
            return clamped
    return low


def apply_enum_boundaries(
    controls: Sequence[dict[str, Any]],
    boundaries: EnumBoundaries,
    *,
    exclude_from_geometry: Sequence[str] = (),
) -> dict[str, Any]:
    """Merge the measured geometry into enum controls; report every decision.

    Fail-closed, per control: a control is changed only when *every* transition
    carries a measured state, the member list matches the profile's own, and the
    result is a strictly increasing band map. A control that cannot be measured
    keeps the geometry it had, and the reason is recorded.

    Minimal-diff: a measured boundary that agrees with the profile's existing
    arithmetic to within the artifact's tolerance changes no decision, so the
    control keeps its byte-identical geometry (`confirmed`) - the measurement's
    evidence lives in the artifact, which is a declared source, not in a rewritten
    profile. Only a boundary that would decide differently is attached, together
    with the band midpoints the write path needs.
    """
    from . import source_facts as source_facts_module  # noqa: PLC0415 - one-way, lazy

    report: dict[str, Any] = {
        "controls_seen": 0,
        "controls_measured": 0,
        "controls_refined": 0,
        "controls_confirmed": 0,
        "controls_refused": 0,
        "controls_without_band_structure": 0,
        "refusals": [],
        "unresolved_band_structures": [],
        "not_enums_any_more": [],
        "confirmations_withheld": [],
        "position_defects": [],
        "positions_moved": [],
        "unlabelled_regions": [],
        "unresolved_transitions": 0,
        "widest_window": 0.0,
        "widest_offset": 0.0,
        "attached": [],
        "counts": {},
    }
    excluded = {str(item) for item in exclude_from_geometry}
    by_id = {str(control.get("id")): control for control in controls}
    for control_id, record in boundaries.controls.items():
        control = by_id.get(control_id)
        if control is None:
            continue
        report["controls_seen"] += 1
        members = [str(member.get("id")) for member in (control.get("enum_values") or [])]
        if not members:
            # The control is **not an enum** in this profile (P24, 2026-10-12): its
            # sweep proved the declared member list was a sampling grid and a
            # measured numeric law replaced it. The artifact is still a
            # measurement of the same control, so it is cross-checked rather than
            # dropped: agreeing (every measured transition showed a display
            # *between* members) confirms the new model and is carried on it;
            # claiming measured bands for a control the profile models numerically
            # is a contradiction and refuses the whole control loudly - a silent
            # skip would hide which of the two measurements is wrong.
            measured_transitions = [
                row for row in record["transitions"]
                if row["state"] in MEASURED_TRANSITION_STATES
            ]
            if measured_transitions:
                report["controls_refused"] += 1
                report["refusals"].append(
                    {
                        "control": control_id,
                        "reason": (
                            "the_artifact_measured_band_structure_for_a_control_the_profile_"
                            "models_numerically"
                        ),
                        "states": sorted({row["state"] for row in measured_transitions}),
                        "transitions": [row["i"] for row in measured_transitions],
                        "profile_model": {
                            "type": str(control.get("type")),
                            "law": str((control.get("law") or {}).get("kind")),
                        },
                    }
                )
                continue
            # The confirmation may only rest on transitions the artifact actually
            # *measured as a display between two members* (`continuous_display`). A
            # transition the measurement could not resolve is not agreement, and
            # counting it would let a wholly unmeasured artifact "confirm" a model -
            # while the same state makes an *enum* control a loud refusal. When the
            # artifact is incomplete the model stands (the sweep produced it) but the
            # confirmation is **withheld** and the reason is recorded.
            continuous = [
                row for row in record["transitions"] if row["state"] == "continuous_display"
            ]
            if len(continuous) != len(record["transitions"]):
                report["confirmations_withheld"].append(
                    {
                        "control": control_id,
                        "reason": "not_every_transition_measured_a_display_between_members",
                        "states": sorted({row["state"] for row in record["transitions"]}),
                        "transitions": len(record["transitions"]),
                        "measured_continuous": len(continuous),
                    }
                )
                report["not_enums_any_more"].append(
                    {
                        "control": control_id,
                        "declared_members": len(record["members"]),
                        "states": sorted({row["state"] for row in record["transitions"]}),
                        "profile_model": {
                            "type": str(control.get("type")),
                            "law": str((control.get("law") or {}).get("kind")),
                        },
                        "confirmation": "withheld",
                        "why": (
                            "the artifact did not measure every transition as a display between "
                            "members, so it cannot confirm the model the sweep produced"
                        ),
                    }
                )
                continue
            confirmation = {
                "kind": ENUM_BOUNDARIES_KIND,
                "method_version": int(boundaries.method_version),
                "artifact": boundaries.source_row(),
                "tolerance": float(boundaries.tolerance),
                "state": "continuous_display",
                "declared_members": len(record["members"]),
                "transitions_measured": len(continuous),
                "profile_model": {
                    "type": str(control.get("type")),
                    "law": str((control.get("law") or {}).get("kind")),
                },
                "note": (
                    "the host printed a value between adjacent members at every measured "
                    "transition: the declared member list is a sampling grid, and the profile "
                    "models the control with the measured numeric law"
                ),
            }
            control["law"] = {
                "kind": str((control.get("law") or {}).get("kind")),
                "params": {
                    **dict((control.get("law") or {}).get("params") or {}),
                    "artifact_confirmation": confirmation,
                },
            }
            report["not_enums_any_more"].append(
                {
                    "control": control_id,
                    "declared_members": len(record["members"]),
                    "states": sorted({row["state"] for row in record["transitions"]}),
                    "profile_model": dict(confirmation["profile_model"]),
                }
            )
            continue
        if members != record["members"]:
            report["controls_refused"] += 1
            report["refusals"].append(
                {
                    "control": control_id,
                    "reason": "member_list_differs_from_the_profile",
                    "measured": record["members"][:6],
                    "profile": members[:6],
                }
            )
            continue
        weak = [
            row for row in record["transitions"] if row["state"] not in MEASURED_TRANSITION_STATES
        ]
        if weak:
            report["unresolved_transitions"] += len(weak)
            states = sorted({row["state"] for row in weak})
            continuous = all(state == "continuous_display" for state in states)
            reason = (
                "the host prints a value between adjacent members: the display is a ramp "
                "sampled on the sweep's grid, so this control has no band structure to find"
                if continuous
                else "not every transition is measurable: " + ", ".join(states)
            )
            label_facts = source_facts_module.NO_LABEL_TABLE.get(control_id)
            if label_facts is not None and not continuous:
                # P25 (2026-10-12): for a control whose *plugin-side* display has no
                # label table at all, `unresolved` is not a gap in the measurement - it
                # is the shape of the thing. The host prints the value itself (Vital's
                # bypass multiplies by 60 and carries no string table), so no
                # measurement could ever read a state name here, and the member ids the
                # profile carries are the registry's words, not the plugin's.
                reason = (
                    label_facts["consequence"]
                    + "; the plugin has no state label to measure, so this transition "
                    "stays unresolved by construction rather than by a measurement gap"
                )
            # the *reason* travels into the profile even where nothing is attached:
            # a consumer must be able to see that this control's bands are not
            # measured, and why
            control["law"] = {
                "kind": str((control.get("law") or {}).get("kind")),
                "params": {
                    **dict((control.get("law") or {}).get("params") or {}),
                    "boundary_evidence": {
                        "kind": ENUM_BOUNDARIES_KIND,
                        "method_version": boundaries.method_version,
                        "artifact": boundaries.source_row(),
                        "tolerance": boundaries.tolerance,
                        "state": "no_band_structure" if continuous else "undetermined",
                        "states": states,
                        "transitions_unmeasured": [row["i"] for row in weak],
                        "note": reason,
                        **(
                            {"state_labels": dict(label_facts)}
                            if label_facts is not None and not continuous
                            else {}
                        ),
                    },
                },
            }
            if continuous:
                report["controls_without_band_structure"] += 1
                report["unresolved_band_structures"].append(
                    {"control": control_id, "members": len(members), "reason": reason}
                )
            else:
                report["controls_refused"] += 1
                report["refusals"].append(
                    {
                        "control": control_id,
                        "reason": "not_every_transition_is_measured",
                        "states": states,
                        "transitions": [row["i"] for row in weak],
                    }
                )
            continue
        ends = _band_ends(record)
        if ends is None:
            report["controls_refused"] += 1
            report["refusals"].append({"control": control_id, "reason": "band_map_not_monotone"})
            continue
        params = (control.get("law") or {}).get("params") or {}
        n = len(members)
        raw_current = params.get("switch_points")
        if isinstance(raw_current, list) and len(raw_current) == n:
            current = [float(value) for value in raw_current]
            current_source = "switch_points"
        else:
            current = [0.0] + [(index - 0.5) / (n - 1) for index in range(1, n)]
            current_source = "uniform"
        # ``confirmed_within`` certifies exactly one thing - that the *profile's own*
        # boundary reproduces both sides of the display within epsilon - so a bracket
        # that does not contain that boundary is the state being misused, and the
        # whole control is refused rather than trusted (found by the independent
        # review: an artifact claiming ``confirmed_within`` with a bracket of
        # [0.1, 0.9] around a boundary of 0.4 would otherwise *refine* to 0.9).
        abused = [
            row["i"]
            for row, boundary in zip(record["transitions"], current[1:])
            if row["state"] == "confirmed_within"
            and not (float(row["lo"]) <= boundary <= float(row["hi"]))
        ]
        if abused:
            report["controls_refused"] += 1
            report["refusals"].append(
                {
                    "control": control_id,
                    "reason": "bracket_does_not_contain_the_profile_boundary",
                    "transitions": abused,
                }
            )
            continue
        # The band *interiors* are what a read between two boundaries resolves to; a
        # control whose interiors the probe could not confirm is not mergeable.
        interiors = record.get("band_interiors") or {}
        if interiors.get("checked") and interiors.get("verified") != interiors.get("checked"):
            report["controls_refused"] += 1
            report["refusals"].append(
                {
                    "control": control_id,
                    "reason": "band_interior_not_verified",
                    "checked": interiors.get("checked"),
                    "verified": interiors.get("verified"),
                    "examples": interiors.get("mismatches") or [],
                }
            )
            continue
        report["controls_measured"] += 1

        # the switch points are the *measured* ends of each band, kept at full
        # precision: rounding them to 9 decimals would discard the very resolution
        # the measurement established (0.4999999850988388 is the boundary; 0.5 is
        # not, and 0.499999985 is a third, wrong number)
        measured_switch = [float(value) for value in ends[:-1]]
        measured_positions = [_midpoint(ends[index], ends[index + 1]) for index in range(n)]

        windows = []
        offsets = []
        for row, current_boundary in zip(record["transitions"], current[1:]):
            low, high = float(row["lo"]), float(row["hi"])
            # the true transition lies in (low, high]; an offset of 0 means the
            # profile's boundary is inside the bracket, i.e. indistinguishable from
            # the measurement. A non-zero offset is a *lower bound* on the error.
            offset = 0.0 if low <= current_boundary <= high else min(
                abs(current_boundary - low), abs(current_boundary - high)
            )
            offsets.append(offset)
            windows.append(max(abs(current_boundary - low), abs(current_boundary - high)))
        widest = max(windows, default=0.0)
        widest_offset = max(offsets, default=0.0)
        # self-scaling rule: a boundary "differs" only when it lies outside the
        # measured bracket by more than that bracket's own width. With an
        # ULP-resolution bracket (measured 2026-10-11: every one of the 831
        # measurable transitions was bracketed by adjacent doubles) this is a very
        # sharp test - the host flips at 0.5 - 1.49e-8, not at 0.5, and the rule
        # sees the difference.
        bracket_widths = [float(row["hi"]) - float(row["lo"]) for row in record["transitions"]]
        differs = any(
            offset > max(boundaries.tolerance, width)
            for offset, width in zip(offsets, bracket_widths)
        )
        report["widest_window"] = max(report["widest_window"], widest)
        report["widest_offset"] = max(report.get("widest_offset", 0.0), widest_offset)

        # the write targets: keep the profile's own where it is still inside its
        # measured band (a byte-identical write), move it only when the
        # measurement says it is outside or the check proved it displays another
        # member
        raw_positions = params.get("positions")
        current_positions = (
            [float(value) for value in raw_positions]
            if isinstance(raw_positions, list) and len(raw_positions) == n
            else [index / max(1, n - 1) for index in range(n)]
        )
        checked = {row["member"]: row for row in record["positions_checked"]}
        #: The last band reaches the top of the domain unless an unlabelled region
        #: caps it, so its upper end is *inclusive* - writing 1.0 for the last member
        #: is what the host prints that member for (measured: every one of the 6,293
        #: write targets agrees). With a tail, 1.0 prints nothing and the band is
        #: half-open like the others.
        closed_top = record.get("tail") is None and ends[-1] == 1.0

        def inside(index: int, value: float) -> bool:
            low, high = ends[index], ends[index + 1]
            if closed_top and index == n - 1:
                return low <= value <= high
            return low <= value < high

        positions = list(current_positions)
        moved: list[dict[str, Any]] = []
        defective: list[dict[str, Any]] = []
        for index, member in enumerate(members):
            low, high = ends[index], ends[index + 1]
            check = checked.get(member)
            displays = check["ok"] if check is not None else None
            if displays is False:
                defective.append(
                    {"member": member, "position": round(current_positions[index], 9)}
                )
            if not inside(index, current_positions[index]) or displays is False:
                positions[index] = measured_positions[index]
                moved.append(
                    {
                        "member": member,
                        "from": round(current_positions[index], 9),
                        "to": measured_positions[index],
                        "reason": (
                            "the position displays another member"
                            if displays is False
                            else "the position is outside the measured band"
                        ),
                        "band": [round(low, 9), round(high, 9)],
                    }
                )
        if record.get("tail"):
            style_facts = source_facts_module.FILTER_STYLE_FACTS
            report["unlabelled_regions"].append(
                {
                    "control": control_id,
                    "lo": round(record["tail"]["lo"], 9),
                    "hi": round(record["tail"]["hi"], 9),
                    "text": record["tail"]["text"][:32],
                    # The state is left **unnamed on purpose** (P24, 2026-10-12). The
                    # plugin's own string table is the authority, and it has no name to
                    # give - and P25 read the source to say *why*: the parameter is
                    # declared over 0.0..9.0 (`ValueDetails::kIndexed`) while the name
                    # table `kFilterStyleNames` has five entries, and
                    # `value_bridge.h getText` indexes it with the rounded value clamped
                    # only at the top: indices 5..9 read past the table. There is no
                    # name to print, rather than an unknown one, and the registry's own
                    # sweep recorded exactly that (an empty string at 0.5625).
                    #
                    # P25 also settled the part P24 could only defer: the *parameter's*
                    # text does **not** depend on `filter_N_model` - `getText` uses
                    # `kFilterStyleNames` unconditionally - so the profile needs no
                    # contextual dependency, only the note below. The model-dependent
                    # naming lives in the editor (`filter_section.cpp getStyleName`),
                    # a different channel.
                    "why": (
                        "the plugin's own style-name table has five entries while the "
                        "parameter is declared over 0.0..9.0: an index past the table has "
                        "no name to print"
                    ),
                    "source": (
                        "mtytel/vital: src/common/synth_parameters.cpp (the `style` parameter), "
                        "src/plugin/value_bridge.h (getText), "
                        + str(style_facts["labels_source"])
                        + "; "
                        + str(style_facts["ui_labels_source"])
                    ),
                    "labels": list(style_facts["labels"]),
                    "named_indices": list(style_facts["named_indices"]),
                    "unnamed_indices": list(style_facts["unnamed_indices"]),
                    "parameter_bounds": dict(style_facts["parameter_bounds"]),
                    "parameter_bounds_source": str(style_facts["parameter_bounds_source"]),
                    "public_source_bounds": dict(style_facts["public_source_bounds"]),
                    "display_depends_on_the_filter_model": bool(
                        style_facts["display_depends_on_the_filter_model"]
                    ),
                    "display_depends_on_the_filter_model_why": str(
                        style_facts["display_depends_on_the_filter_model_why"]
                    ),
                    "ui_labels_by_model": dict(style_facts["ui_labels_by_model"]),
                    "what_the_labels_mean": (
                        "the five names are the *parameter's* display strings, fixed by the "
                        "plugin's own table; which filter shape an index selects depends on "
                        "`filter_N_model` (the editor shows a per-model name), and that "
                        "mapping is not modelled here - the enum's positions and members do "
                        "not change with the model"
                    ),
                }
            )

        # The invariant this round must never break: a write target has to be inside
        # the band whose member it selects, or the read path would report a member the
        # host never prints for the value written. Checked for *every* member after the
        # keep-or-move rules, on the exact numbers that will be attached - the
        # independent review produced a two-ULP band where a rounded midpoint escaped.
        escaping = [
            {"member": members[index], "position": positions[index], "band": [ends[index], ends[index + 1]]}
            for index in range(n)
            if not inside(index, positions[index])
        ]
        if escaping:
            report["controls_refused"] += 1
            report["refusals"].append(
                {
                    "control": control_id,
                    "reason": "no_write_target_inside_its_measured_band",
                    "examples": escaping[:4],
                }
            )
            continue

        changes_decision = control_id not in excluded and (differs or bool(moved))
        if not changes_decision:
            report["controls_confirmed"] += 1
            report["attached"].append(
                {
                    "control": control_id,
                    "outcome": "confirmed",
                    "current_source": current_source,
                    "widest_offset": round(widest_offset, 12),
                    "tolerance": boundaries.tolerance,
                }
            )
            continue

        # attach the measured geometry. The *write* targets move only where the
        # measurement says they must: a position already inside its measured band is
        # kept exactly as it was (so every write this profile performed yesterday it
        # still performs identically), and one that would land in the wrong band or
        # display another member is replaced by its band's midpoint.
        attached_params = {
            **dict(params),
            "switch_points": measured_switch,
            # The evidence block is deliberately *compact*: the artifact (a declared
            # source, whose hash is in the cache key) holds the full per-transition
            # record, so the profile carries the decision and its provenance, not a
            # second copy of the measurement. The long explanation lives once, in the
            # profile's own `enum_boundaries` block.
            "boundary_evidence": {
                "kind": ENUM_BOUNDARIES_KIND,
                "method_version": boundaries.method_version,
                "artifact": boundaries.source_row(),
                "tolerance": boundaries.tolerance,
                "states": sorted({row["state"] for row in record["transitions"]}),
                "transitions": len(record["transitions"]),
                "widest_bracket": max(bracket_widths, default=0.0),
                "widest_offset_before": widest_offset,
                "previous_source": current_source,
                "positions_moved": [row["member"] for row in moved],
                # The bands themselves, so that the *containment* of every write
                # target can be re-checked by a reader (and by `validate_profile`)
                # instead of being taken on trust, and the probe's own interior
                # verification is carried with the numbers it justifies.
                "band_ends": [float(value) for value in ends],
                "band_interiors_checked": int(interiors.get("checked") or 0),
                "band_interiors_verified": int(interiors.get("verified") or 0),
            },
        }
        if moved or isinstance(params.get("positions"), list):
            attached_params["positions"] = positions
        control["law"] = {"kind": str((control.get("law") or {}).get("kind")), "params": attached_params}
        report["controls_refined"] += 1
        report["attached"].append(
            {
                "control": control_id,
                "outcome": "refined",
                "current_source": current_source,
                "widest_offset_before": widest_offset,
                "tolerance": boundaries.tolerance,
                "switch_points": measured_switch,
                "positions_moved": moved,
            }
        )
        report["positions_moved"].extend(
            [{"control": control_id, **row} for row in moved]
        )
        report["position_defects"].extend(
            [{"control": control_id, **row} for row in defective]
        )

    report["counts"] = {
        "controls_in_artifact": len(boundaries.controls),
        "controls_seen": report["controls_seen"],
        "controls_measured": report["controls_measured"],
        "controls_refined": report["controls_refined"],
        "controls_confirmed": report["controls_confirmed"],
        "controls_refused": report["controls_refused"],
        # Artifact-derived: the artifact records whose transitions show a display
        # *between* members. Since P24 such a control is usually no longer an enum
        # in the profile (the sweep proved the member list was a sampling grid) and
        # appears below as `controls_not_enums_any_more`; the two numbers are equal
        # when every ramp the artifact measured was also converted.
        "controls_without_band_structure": sum(
            1
            for record in boundaries.controls.values()
            if any(row["state"] == "continuous_display" for row in record["transitions"])
        ),
        # Profile-derived: artifact rows whose control the profile now models with a
        # measured numeric law (see the docstring of `apply_enum_boundaries`).
        "controls_not_enums_any_more": len(report["not_enums_any_more"]),
        # Profile-derived: converted controls whose artifact did **not** measure every
        # transition as a display between members, so the artifact cannot confirm the
        # model (`confirmations_withheld`). The model itself comes from the sweep.
        "confirmations_withheld": len(report["confirmations_withheld"]),
        "transitions_measured": sum(len(row["transitions"]) for row in boundaries.controls.values()),
        "unresolved_transitions": report["unresolved_transitions"],
        "positions_defective": len(report["position_defects"]),
        "positions_moved": len(report["positions_moved"]),
        "unlabelled_regions": len(report["unlabelled_regions"]),
        "tolerance": boundaries.tolerance,
        "widest_window_before": round(report["widest_window"], 12),
        "widest_offset_before": round(report["widest_offset"], 12),
    }
    return report
