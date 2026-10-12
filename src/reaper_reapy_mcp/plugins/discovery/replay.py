"""Offline replay and comparison (P26 §14).

The replay path exists to answer one question before spending a host session:
*do the new generic algorithms reproduce what was already measured on this
plugin, and where do they disagree?*

It is built so the answer cannot be faked:

- the replay feeds the **same analyzers** through the same `DiscoveryEngine`; only
  the observation source changes (`origin="recorded"`), and the engine reports
  that origin in the artifact;
- the reference is a **served profile** and a **recorded boundary artifact** that
  the replay never reads: the generic decision path is given host observations
  only, so a provider fact cannot leak into a generic verdict;
- agreement is *not* treated as proof of correctness. The comparison publishes
  both directions - what the reference has and the engine withheld, and what the
  engine asserts and the reference contradicts - because the reference itself was
  corrected repeatedly (P19-P25), and a difference can mean either side is wrong.

Nothing here calls a host. A recorded artifact is readable on any machine, which
is what makes a replay result checkable by someone else.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .pipeline import DiscoveryEngine, EngineConfig
from .text import parse_display
from .types import ControlObservations, DiscoveryResult, Inventory, ParamRow, Sample, StepSizes

__all__ = [
    "RecordedSource",
    "read_recorded",
    "replay_from_recorded",
    "compare_with_profile",
    "compare_with_boundaries",
    "partition_agreement",
    "summarize_comparison",
]

#: The recorded shapes this reader understands. Dispatch is **structural**, not
#: on the document's own kind string: a provider's recorded file names itself, and
#: Core must not carry a list of plugin names to know how to read one. The
#: declared kind is kept verbatim as data.
SUPPORTED_SHAPES: tuple[str, ...] = ("engine-artifact", "display-samples", "boundary-record")


@dataclass
class RecordedSource:
    """A recorded host observation set, normalized for replay."""

    path: str
    kind: str
    shape: str
    identity: dict[str, Any] = field(default_factory=dict)
    records: list[dict[str, Any]] = field(default_factory=list)
    observations: list[ControlObservations] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def _shape_of(document: Mapping[str, Any]) -> str:
    """Which recorded shape a document has, decided from its structure alone."""
    if isinstance(document.get("payload"), Mapping) and isinstance(document.get("observations"), list):
        return "engine-artifact"
    controls = document.get("controls")
    if isinstance(controls, Mapping) and controls:
        sample = next((row for row in controls.values() if isinstance(row, Mapping)), None)
        if isinstance(sample, Mapping):
            if "samples" in sample:
                return "display-samples"
            if "members" in sample or "transitions" in sample:
                return "boundary-record"
    return "unknown"


def read_recorded(path: str | Path) -> RecordedSource:
    """Read a recorded artifact and normalize it.

    Three *shapes* are understood, all of which exist on this machine in some
    plugin's recorded data:

    ``display-samples``
        a control map keyed by parameter name, each row carrying a list of display
        strings sampled over a grid, plus step sizes and a unit claim;
    ``boundary-record``
        a control map whose rows carry measured member labels, transitions and
        band interiors - read by the **comparison** only, never by the engine;
    ``engine-artifact``
        this engine's own prepared artifact: inventory rows plus observations.
    """
    origin = Path(path)
    raw = json.loads(origin.read_text(encoding="utf-8"))
    declared_kind = str(raw.get("kind") or "")
    shape = _shape_of(raw)
    source = RecordedSource(path=str(origin), kind=declared_kind, shape=shape)
    if shape == "engine-artifact":
        payload = raw.get("payload") or {}
        inventory = payload.get("inventory") or {}
        source.identity = dict(inventory.get("identity") or raw.get("identity") or {})
        source.records = [dict(row) for row in (inventory.get("params") or [])]
        source.observations = _observations_from_rows(raw.get("observations") or [])
        source.meta = {"mode": raw.get("mode"), "engine_version": raw.get("engine_version")}
        return source
    if shape == "display-samples":
        identity = dict(raw.get("identity") or {})
        host = raw.get("host") or {}
        identity.setdefault("param_count", host.get("param_count_reported"))
        source.identity = identity
        controls = raw.get("controls") or {}
        grid = [float(value) for value in (raw.get("grid") or [])]
        for name, row in controls.items():
            if not isinstance(row, Mapping):
                continue
            index = row.get("index")
            try:
                index = int(index)
            except (TypeError, ValueError):
                continue
            samples = [str(item) for item in (row.get("samples") or [])]
            if not samples:
                continue
            positions = grid[: len(samples)] if len(grid) >= len(samples) else _uniform(len(samples))
            steps = row.get("steps") or {}
            record = {
                "index": index,
                "name": str(name),
                "steps": {
                    "reported": bool(steps.get("reported")),
                    "step": float(steps.get("step") or 0.0),
                    "small": float(steps.get("small") or 0.0),
                    "large": float(steps.get("large") or 0.0),
                    "is_toggle": bool(steps.get("is_toggle")),
                    "status": "ok" if steps.get("reported") or steps.get("is_toggle") else "absent",
                },
                "extra": {
                    "recorded_unit_token": row.get("unit_token"),
                    "recorded_numeric": row.get("numeric"),
                    "recorded_descending": row.get("descending"),
                    "recorded_labels": row.get("labels"),
                },
            }
            source.records.append(record)
            source.observations.append(
                ControlObservations(
                    index=index,
                    name=str(name),
                    samples=[
                        Sample(normalized=float(position), text=text, origin="recorded")
                        for position, text in zip(positions, samples)
                    ],
                    status="ok",
                    origin="recorded",
                )
            )
        source.meta = {
            "grid": grid,
            "generated_utc": raw.get("generated_utc"),
            "method_version": raw.get("method_version"),
            "host": host,
            "read_only_proof": raw.get("read_only_proof"),
            "step_sizes_reported": (raw.get("host_step_sizes") or {}).get("reported"),
        }
        if not source.records:
            source.warnings.append("the artifact carries no control rows")
        return source
    if shape == "boundary-record":
        source.identity = dict(raw.get("identity") or {})
        source.records = [
            {"name": str(name), **dict(row)}
            for name, row in (raw.get("controls") or {}).items()
            if isinstance(row, Mapping)
        ]
        source.meta = {
            "counts": raw.get("counts"),
            "method": raw.get("method"),
            "generated_utc": raw.get("generated_utc"),
        }
        return source
    source.warnings.append(
        f"the document declares kind {declared_kind!r} and has none of the supported shapes "
        f"{list(SUPPORTED_SHAPES)}; nothing was replayed"
    )
    return source


def _uniform(count: int) -> list[float]:
    if count <= 1:
        return [0.0] * count
    step = 1.0 / (count - 1)
    return [round(index * step, 12) for index in range(count)]


def _observations_from_rows(rows: Iterable[Mapping[str, Any]]) -> list[ControlObservations]:
    out: list[ControlObservations] = []
    for raw in rows:
        try:
            index = int(raw.get("index"))
        except (TypeError, ValueError):
            continue
        entry = ControlObservations(
            index=index,
            name=raw.get("name"),
            ident=raw.get("ident"),
            status=str(raw.get("status") or "ok"),
            origin="recorded",
            reason=(str(raw["reason"]) if raw.get("reason") else None),
        )
        for sample in raw.get("samples") or []:
            if not isinstance(sample, Mapping):
                continue
            entry.samples.append(
                Sample(
                    normalized=float(sample.get("normalized") or 0.0),
                    text="" if sample.get("text") is None else str(sample.get("text")),
                    origin="recorded",
                )
            )
        out.append(entry)
    return out


def inventory_from_source(source: RecordedSource) -> Inventory:
    """The inventory a replay uses: recorded rows, marked as recorded."""
    rows: list[ParamRow] = []
    for raw in source.records:
        row = ParamRow(index=int(raw.get("index", len(rows))))
        if raw.get("name"):
            row.name = str(raw["name"])
        if raw.get("ident"):
            row.ident = str(raw["ident"])
        steps = raw.get("steps")
        if isinstance(steps, Mapping):
            row.steps = StepSizes(
                reported=bool(steps.get("reported")),
                step=float(steps.get("step") or 0.0),
                small=float(steps.get("small") or 0.0),
                large=float(steps.get("large") or 0.0),
                is_toggle=bool(steps.get("is_toggle")),
                status=str(steps.get("status") or "absent"),
            )
        if isinstance(raw.get("extra"), Mapping):
            row.extra.update(dict(raw["extra"]))
        rows.append(row)
    identity = dict(source.identity)
    count = identity.get("param_count")
    inventory = Inventory(
        identity=identity,
        param_count=int(count) if isinstance(count, (int, float)) else len(rows),
        reported_param_count=int(count) if isinstance(count, (int, float)) else None,
        params=rows,
        origin="recorded",
    )
    inventory.attempt(
        "param_metadata",
        "ok",
        calls=0,
        message="replayed from a recorded artifact (no host contact)",
        artifact=source.path,
        kind=source.kind,
    )
    inventory.notes.append(
        "the replay reads recorded host observations only: no plugin registry, no source facts and no "
        "provider profile take part in the generic decision"
    )
    return inventory


def replay_from_recorded(
    source: RecordedSource,
    *,
    config: EngineConfig | None = None,
) -> DiscoveryResult:
    """Run the discovery pipeline over a recorded source, with no host."""
    engine = DiscoveryEngine(bridge=None)
    resolved = (config or EngineConfig(mode="prepare")).resolved()
    return engine.run(
        None,
        source.identity,
        config=resolved,
        inventory=inventory_from_source(source),
        recorded=source.observations,
    )


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

#: Discovery classes that a profile `enum` type is compatible with, and vice versa.
_ENUM_COMPATIBLE = ("enum", "boolean", "integer_like")
_NUMBER_COMPATIBLE = ("continuous", "ramp_like", "stepped")


def compare_with_profile(
    result: DiscoveryResult,
    profile: Mapping[str, Any],
    *,
    reference: str = "profile",
) -> dict[str, Any]:
    """Per-control comparison of a generic run against a served profile."""
    controls = {control.index: control for control in result.controls}
    profile_controls = [row for row in (profile.get("controls") or []) if isinstance(row, Mapping)]
    rows: list[dict[str, Any]] = []
    for row in profile_controls:
        binding = row.get("binding") or {}
        index = binding.get("index") if isinstance(binding, Mapping) else None
        try:
            index = int(index)
        except (TypeError, ValueError):
            index = None
        discovery = controls.get(index) if index is not None else None
        rows.append(_compare_control(row, discovery, index=index))
    aggregates = summarize_comparison(rows)
    module_rows = [row for row in rows if row.get("reference_module")]
    partition = partition_agreement(
        {
            int(row["index"]): str(row["discovery"]["module"])
            for row in rows
            if row.get("index") is not None
            and row.get("discovery", {}).get("module")
            and row.get("reference_module")
        },
        {
            int(row["index"]): str(row["reference_module"])
            for row in rows
            if row.get("index") is not None and row.get("reference_module")
        },
    )
    return {
        "reference": reference,
        "controls": rows,
        "aggregates": aggregates,
        "module_partition": partition,
        "reference_modules": len({row.get("reference_module") for row in module_rows}),
        "notes": [
            "agreement with the reference is evidence about the comparison, not proof that either side is "
            "correct: the reference profile was itself corrected in earlier rounds",
            "a discovery verdict of 'unknown' is counted as withheld, never as a false positive",
        ],
    }


def _compare_control(
    profile_row: Mapping[str, Any],
    discovery: Any | None,
    *,
    index: int | None,
) -> dict[str, Any]:
    profile_type = str(profile_row.get("type") or "unknown")
    law = profile_row.get("law") or {}
    law_kind = str(law.get("kind") or "unknown") if isinstance(law, Mapping) else "unknown"
    enum_values = profile_row.get("enum_values") or []
    bounds = profile_row.get("range") or {}
    row: dict[str, Any] = {
        "index": index,
        "name": profile_row.get("id"),
        "reference_type": profile_type,
        "reference_law": law_kind,
        "reference_module": (profile_row.get("module") or None),
        "reference_unit": profile_row.get("unit"),
        "reference_members": len(enum_values) if isinstance(enum_values, list) else None,
        "reference_range": [bounds.get("min"), bounds.get("max")] if isinstance(bounds, Mapping) else None,
        "reference_enum_shape": _reference_enum_shape(enum_values),
    }
    if discovery is None:
        row["discovery"] = {"status": "not_analyzed"}
        row["verdict"] = "not_analyzed"
        row["detail"] = "the replay produced no control for this index"
        return row
    kind = discovery.classification.kind
    row["discovery"] = {
        "status": "analyzed",
        "class": kind,
        "confidence": discovery.classification.confidence,
        "module": discovery.module,
        "unit": discovery.unit,
        "law": (
            discovery.mapping.chosen.kind
            if discovery.mapping is not None and discovery.mapping.chosen is not None
            else None
        ),
        "law_kind_comparable": (
            discovery.mapping.chosen.profile_law_kind
            if discovery.mapping is not None and discovery.mapping.chosen is not None
            else None
        ),
        "distinct_texts": discovery.discrete.distinct_count if discovery.discrete is not None else 0,
        "range": discovery.range.chosen if discovery.range is not None else None,
        "range_space": discovery.range.space if discovery.range is not None else None,
    }
    verdict, detail = _class_verdict(profile_type, kind)
    row["verdict"] = verdict
    row["detail"] = detail
    row["law_agreement"] = _law_agreement(law_kind, row["discovery"]["law_kind_comparable"])
    row["range_agreement"] = _range_agreement(row["reference_range"], row["discovery"]["range"])
    row["unit_agreement"] = _unit_agreement(row["reference_unit"], row["discovery"]["unit"])
    # Two disagreements need no new measurement to explain, and both are recorded as
    # *explained* rather than mixed into the unexplained count: a reference whose
    # enumeration members are a monotone numeric series is describing a ladder (P23/P24
    # measured exactly this), and a disagreement where both sides found the same number
    # of banded states is a naming difference, not a class error.
    shape = row["reference_enum_shape"]
    if verdict == "disagreement" and profile_type == "enum" and shape.get("numeric_monotone"):
        members = row["reference_members"] or 0
        if members > 16:
            verdict = "disagreement_explained"
            detail = (
                f"the reference enumerates {members} members whose ids are a monotone numeric series "
                f"({shape.get('first')!r}..{shape.get('last')!r}), which is a numeric ladder expressed as "
                "an enumeration; the run classified the printed numbers as a numeric control"
            )
    if verdict == "disagreement":
        members = row["reference_members"]
        distinct = int(row["discovery"]["distinct_texts"] or 0)
        if isinstance(members, int) and 2 <= members <= 16 and distinct == members:
            # The two documents describe the SAME number of banded states and differ
            # only in what they call them. That is a finding, but it is not agreement
            # and not a class error either, so it gets its own verdict instead of being
            # folded into `agree_with_caveat` (the P26 independent review showed that
            # folding can absorb a real disagreement: two counts are not two states).
            verdict = "naming_difference"
            detail = (
                f"both documents describe {members} banded states; the reference calls them an "
                "enumeration and the run a quantised numeric display - the label counts agree, which is "
                "not evidence that the member sets are equal"
            )
    row["verdict"] = verdict
    row["detail"] = detail
    members = row["reference_members"]
    if verdict in ("agree", "agree_with_caveat") and isinstance(members, int) and members > 0:
        distinct = int(row["discovery"]["distinct_texts"] or 0)
        if distinct == 0:
            row["member_relation"] = "no_display_sample"
        elif distinct > members:
            row["member_relation"] = "discovery_sees_more_states_than_reference"
        elif distinct == members:
            row["member_relation"] = "equal"
        else:
            row["member_relation"] = "reference_has_more_members"
    return row


def _class_verdict(profile_type: str, discovery_kind: str) -> tuple[str, str]:
    if discovery_kind == "unknown":
        return "withheld", "the generic run could not classify this control from display evidence alone"
    if profile_type == "enum":
        if discovery_kind in _ENUM_COMPATIBLE:
            return (
                "agree" if discovery_kind == "enum" else "agree_with_caveat",
                f"both discrete ({discovery_kind})",
            )
        if discovery_kind in _NUMBER_COMPATIBLE:
            return "disagreement", f"the reference says enum, the run found a numeric control ({discovery_kind})"
        return "not_comparable", f"unknown class {discovery_kind!r}"
    if profile_type == "number":
        if discovery_kind in _NUMBER_COMPATIBLE:
            return (
                "agree" if discovery_kind == "continuous" else "agree_with_caveat",
                f"both numeric ({discovery_kind})",
            )
        if discovery_kind in _ENUM_COMPATIBLE:
            return "disagreement", f"the reference says number, the run found a discrete control ({discovery_kind})"
        return "not_comparable", f"unknown class {discovery_kind!r}"
    return "not_comparable", f"the reference declares an unknown type {profile_type!r}"


def _reference_enum_shape(enum_values: Any) -> dict[str, Any]:
    """Describe a reference enumeration's member ids.

    This is a reading of the *reference document*, not a claim about the plugin: an
    enumeration whose ids are labels (``12dB``) and one whose ids are a monotone
    numeric series (``0, 1417, 2835, ...``) are different objects, and the
    comparison has to say which one it faced.
    """
    if not isinstance(enum_values, list) or not enum_values:
        return {}
    ids: list[str] = []
    for item in enum_values:
        if isinstance(item, Mapping):
            ids.append(str(item.get("id")))
        else:
            ids.append(str(item))
    numbers: list[float] = []
    for item in ids:
        # The ids are parsed with the same display reader the engine uses, so a
        # member labelled "4 semitones" is recognised as the number it is.
        parsed = parse_display(item)
        if not parsed.numeric or parsed.value is None:
            return {"labels_numeric": False, "count": len(ids)}
        numbers.append(float(parsed.value))
    monotone = all(numbers[i + 1] >= numbers[i] for i in range(len(numbers) - 1))
    steps = [round(numbers[i + 1] - numbers[i], 9) for i in range(len(numbers) - 1)]
    return {
        "labels_numeric": True,
        "count": len(ids),
        "monotone": monotone,
        "numeric_monotone": bool(monotone),
        "uniform_step": bool(steps) and len(set(steps)) == 1,
        "first": numbers[0],
        "last": numbers[-1],
    }


def _law_agreement(reference_law: str, discovery_law: str | None) -> str:
    if discovery_law is None:
        return "not_comparable"
    if reference_law == discovery_law:
        return "same_kind"
    if reference_law in ("none", "unmeasured") or discovery_law in ("none", "unmeasured"):
        return "one_side_unmeasured"
    return "different_kind"


def _range_agreement(reference: Any, discovery: Any) -> dict[str, Any]:
    if not isinstance(reference, (list, tuple)) or len(reference) != 2 or not isinstance(discovery, (list, tuple)):
        return {"relation": "not_comparable"}
    try:
        ref_low, ref_high = float(reference[0]), float(reference[1])
        dis_low, dis_high = float(discovery[0]), float(discovery[1])
    except (TypeError, ValueError):
        return {"relation": "not_comparable"}
    span = max(abs(ref_high - ref_low), abs(dis_high - dis_low), 1e-12)
    tolerance = max(1e-6, span * 1e-9)
    if abs(ref_low - dis_low) <= tolerance and abs(ref_high - dis_high) <= tolerance:
        return {"relation": "equal"}
    offset_low = dis_low - ref_low
    offset_high = dis_high - ref_high
    if abs(offset_low - offset_high) <= max(abs(offset_low) * 1e-6, tolerance):
        return {
            "relation": "constant_offset_candidate",
            "offset": offset_low,
            "note": "one constant offset explains both ends: a different display space, not a different range",
        }
    if ref_low >= dis_low - tolerance and ref_high <= dis_high + tolerance:
        return {"relation": "reference_inside_discovery"}
    if dis_low >= ref_low - tolerance and dis_high <= ref_high + tolerance:
        return {"relation": "discovery_inside_reference"}
    return {"relation": "disjoint_or_overlapping"}


def _unit_agreement(reference: Any, discovery: Any) -> str:
    if reference is None and discovery is None:
        return "both_absent"
    if reference is None or discovery is None:
        return "one_side_absent"
    ref = str(reference).strip().lower()
    dis = str(discovery).strip().lower()
    if ref == dis:
        return "equal"
    if ref and dis and (ref in dis or dis in ref):
        return "compatible"
    return "different"


def compare_with_boundaries(
    result: DiscoveryResult,
    boundaries: Mapping[str, Any],
    *,
    grid_points: int | None = None,
) -> dict[str, Any]:
    """Compare the grid-derived band structure with measured boundaries.

    The recorded boundary artifact holds *measured* switch points; the replay only
    ever sees a sampled grid, so its band edges are upper bounds. The comparison
    therefore asks the only two questions a grid can answer: does the band count
    agree, and does every measured switch point lie inside the grid bracket the
    replay reported?
    """
    measured = boundaries.get("controls") if isinstance(boundaries, Mapping) else None
    if isinstance(measured, Mapping):
        entries = [(str(name), row) for name, row in measured.items() if isinstance(row, Mapping)]
    elif isinstance(measured, list):
        entries = [(str(row.get("name") or row.get("index")), row) for row in measured if isinstance(row, Mapping)]
    else:
        entries = []
    grid = float(result.cost.budgets.get("engine", {}).get("grid_points") or 17)
    resolution = 1.0 / max(1.0, grid - 1.0)
    rows: list[dict[str, Any]] = []
    counts = {
        "band_count_agree": 0,
        "band_count_differ": 0,
        "no_band_structure": 0,
        "not_measured": 0,
        "containment_strict": 0,
        "containment_within_slack": 0,
        "containment_failed": 0,
        "containment_unchecked": 0,
        "containment_label_mismatch": 0,
    }
    for name, raw in entries:
        if not isinstance(raw, Mapping):
            continue
        index = raw.get("index")
        try:
            index = int(index)
        except (TypeError, ValueError):
            index = None
        discovery = result.control(index) if index is not None else None
        members = raw.get("members") or []
        transitions = raw.get("transitions") or []
        n_measured = len(members)
        if discovery is None or discovery.discrete is None:
            counts["not_measured"] += 1
            rows.append({"name": name, "index": index, "verdict": "not_analyzed", "measured_members": n_measured})
            continue
        discrete = discovery.discrete
        if discrete.family not in ("enum", "boolean", "integer_ladder"):
            counts["no_band_structure"] += 1
            rows.append(
                {
                    "name": name,
                    "index": index,
                    "verdict": "no_band_structure",
                    "measured_members": n_measured,
                    "discovery_family": discrete.family,
                    "detail": (
                        "the recorded measurement found labelled members, the replay classified the "
                        "control as a ramp/continuous from display evidence"
                    ),
                }
            )
            continue
        n_discovery = len(discrete.members)
        bracket_checks: list[dict[str, Any]] = []
        for transition in transitions:
            if not isinstance(transition, Mapping):
                continue
            position = transition.get("current")
            if position is None:
                continue
            ordinal = transition.get("i")
            bracket = None
            bracket_source = None
            if isinstance(ordinal, int) and 1 <= ordinal < len(discrete.members):
                # Transition `ordinal` leaves band `ordinal - 1`, which is the band
                # that carries the bracket of its own UPPER edge. Using band `ordinal`
                # (or the two neighbouring band edges) would compare against a bracket
                # a whole band wide and accept almost any position.
                member = discrete.members[ordinal - 1]
                if member.bracket:
                    bracket = [float(member.bracket[0]), float(member.bracket[1])]
                    bracket_source = "band_upper_edge"
                else:
                    bracket = [member.upper, discrete.members[ordinal].lower]
                    bracket_source = "band_span_fallback"
            inside = False
            strict_inside = False
            if bracket is not None:
                inside = bracket[0] - resolution <= float(position) <= bracket[1] + resolution
                strict_inside = bracket[0] <= float(position) <= bracket[1]
            # The reference records which member each transition leaves; the replay has
            # its own band label there. If the two labels disagree, the ordinals do not
            # refer to the same band and a containment "pass" would be meaningless -
            # which is exactly the loophole the P26 independent review found (adjacent
            # brackets touch at a shared sample, so the wrong ordinal can still land
            # inside a neighbouring bracket).
            labels_agree: bool | None = None
            lower_member = transition.get("lower_member")
            if bracket_source is not None and lower_member:
                labels_agree = (
                    str(lower_member).strip().lower()
                    == str(discrete.members[int(ordinal) - 1].label).strip().lower()
                )
            bracket_checks.append(
                {
                    "ordinal": ordinal,
                    "measured_position": position,
                    "grid_bracket": bracket,
                    "bracket_source": bracket_source,
                    "reference_lower_member": lower_member,
                    "labels_agree": labels_agree,
                    "inside_grid_bracket": inside,
                    "inside_bracket_strict": strict_inside,
                    "measured_state": transition.get("state"),
                }
            )
        resolved = [item for item in bracket_checks if item["measured_state"] in ("exact", "exact_at")]
        aligned = [item for item in resolved if item.get("labels_agree") is not False]
        mapped = [item for item in aligned if item.get("labels_agree") is True]
        mismatched = [item for item in resolved if item.get("labels_agree") is False]
        contained = [item for item in aligned if item["inside_grid_bracket"]]
        strict = [item for item in aligned if item.get("inside_bracket_strict")]
        # Containment is a *verdict input*, not decoration: a measured switch point
        # outside the bracket the replay reported is a contradiction the row has to
        # carry, and a row whose ordinals do not refer to the same bands is neither a
        # pass nor a failure (the P26 independent review showed both loopholes).
        if resolved and not aligned:
            containment = "label_mismatch"
            counts["containment_label_mismatch"] += 1
        elif not resolved:
            containment = "unchecked"
            counts["containment_unchecked"] += 1
        elif len(contained) == len(aligned):
            containment = "strict" if len(strict) == len(aligned) else "within_slack"
            counts["containment_strict" if containment == "strict" else "containment_within_slack"] += 1
        else:
            containment = "failed"
            counts["containment_failed"] += 1
        verdict = "band_count_agree" if n_discovery == n_measured else "band_count_differ"
        counts[verdict] += 1
        unlabelled = [member.label for member in discrete.members if member.unlabelled]
        row = {
            "name": name,
            "index": index,
            "verdict": verdict,
            "containment": containment,
            "measured_members": n_measured,
            "discovery_members": n_discovery,
            "measured_transitions": len(transitions),
            "measured_transitions_resolved": len(resolved),
            "labels_checked": len(mapped),
            "labels_mismatched": len(mismatched),
            "strict_inside": len(strict),
            "grid_brackets_containing_measurement": len(contained),
            "checks": bracket_checks[:6],
        }
        if mismatched:
            row["label_mismatch_examples"] = [
                {
                    "ordinal": item["ordinal"],
                    "reference": item["reference_lower_member"],
                    "replay": discrete.members[int(item["ordinal"]) - 1].label,
                }
                for item in mismatched[:3]
                if isinstance(item.get("ordinal"), int) and 1 <= int(item["ordinal"]) - 1 < len(discrete.members)
            ]
        if unlabelled:
            # A replay band the host printed nothing for is the visible form of the
            # measured span question (P25: the style list has unnamed tail bands).
            row["unlabelled_bands"] = len(unlabelled)
            row["detail"] = (
                f"{n_discovery - n_measured} additional band(s) print no text at all, which is why the count differs"
            )
        rows.append(row)
    return {
        "rows": rows,
        "counts": counts,
        "grid_resolution": resolution,
        "notes": [
            "a sampled grid brackets a switch point; it can never measure it, which is why the checks ask "
            "for containment instead of equality",
        ],
    }


def partition_agreement(discovery: Mapping[int, str], reference: Mapping[int, str]) -> dict[str, Any]:
    """Pairwise agreement of two partitions of the same controls.

    Module names differ between an inference and a registry ("Filter 1" vs
    "filter 1"), so the *partition* is compared instead of the labels:
    precision/recall over the "these two controls belong together" relation.
    """
    shared = sorted(set(discovery) & set(reference))
    pairs = 0
    both = 0
    only_reference = 0
    only_discovery = 0
    for position, left in enumerate(shared):
        for right in shared[position + 1 :]:
            pairs += 1
            same_discovery = discovery[left] == discovery[right]
            same_reference = reference[left] == reference[right]
            if same_discovery and same_reference:
                both += 1
            elif same_reference:
                only_reference += 1
            elif same_discovery:
                only_discovery += 1
    precision = (both / (both + only_discovery)) if (both + only_discovery) else None
    recall = (both / (both + only_reference)) if (both + only_reference) else None
    if precision is not None and recall is not None and precision == 0.0 and recall == 0.0:
        f1: float | None = 0.0
    elif precision is not None and recall is not None and (precision + recall) > 0:
        f1 = 2 * precision * recall / (precision + recall)
    else:
        f1 = None
    return {
        "controls_compared": len(shared),
        "pairs": pairs,
        "pairs_grouped_together_in_both": both,
        "pairs_only_in_reference": only_reference,
        "pairs_only_in_discovery": only_discovery,
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }


def summarize_comparison(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate the per-control verdicts without collapsing them into one score."""
    verdicts: dict[str, int] = {}
    laws: dict[str, int] = {}
    ranges: dict[str, int] = {}
    units: dict[str, int] = {}
    members: dict[str, int] = {}
    for row in rows:
        verdict = str(row.get("verdict") or "unknown")
        verdicts[verdict] = verdicts.get(verdict, 0) + 1
        law_agreement = row.get("law_agreement")
        if law_agreement:
            laws[str(law_agreement)] = laws.get(str(law_agreement), 0) + 1
        range_agreement = row.get("range_agreement") or {}
        relation = range_agreement.get("relation") if isinstance(range_agreement, Mapping) else None
        if relation:
            ranges[str(relation)] = ranges.get(str(relation), 0) + 1
        if row.get("unit_agreement"):
            units[str(row["unit_agreement"])] = units.get(str(row["unit_agreement"]), 0) + 1
        if row.get("member_relation"):
            members[str(row["member_relation"])] = members.get(str(row["member_relation"]), 0) + 1
    return {
        "controls": len(rows),
        "verdicts": verdicts,
        "law_agreement": laws,
        "range_agreement": ranges,
        "unit_agreement": units,
        "member_relation": members,
        "true_positive_classes": verdicts.get("agree", 0),
        "compatible_classes": verdicts.get("agree_with_caveat", 0),
        "naming_differences": verdicts.get("naming_difference", 0),
        "disagreements_explained": verdicts.get("disagreement_explained", 0),
        "disagreements_unexplained": verdicts.get("disagreement", 0),
        "withheld": verdicts.get("withheld", 0),
        "not_comparable": verdicts.get("not_comparable", 0),
        "not_analyzed": verdicts.get("not_analyzed", 0),
    }
