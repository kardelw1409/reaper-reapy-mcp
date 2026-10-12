"""Discovery reporting (P26 §12, §18).

A run has to be readable by a human before it is trusted by an agent, so this
module answers, in one place and without prose: what was inventoried, what was
established, what was *not* established and why, which fields are disputed, what
the module tree looks like, and what the run cost.

Nothing here produces a single "quality" number. A percentage would hide exactly
the distinction the brief cares about - "measured", "inferred" and "unknown" are
different answers, and they are reported as different counts.
"""

from __future__ import annotations

from typing import Any

from .types import DiscoveryResult

__all__ = ["summarize", "disputed", "unknown_census", "format_text"]


def summarize(result: DiscoveryResult, *, module_limit: int = 40, disputed_limit: int = 25) -> dict[str, Any]:
    """A compact, complete summary of one run."""
    controls = result.controls
    classes: dict[str, int] = {}
    confidences: dict[str, int] = {}
    range_spaces: dict[str, int] = {}
    families: dict[str, int] = {}
    for control in controls:
        classes[control.classification.kind] = classes.get(control.classification.kind, 0) + 1
        confidences[control.classification.confidence] = (
            confidences.get(control.classification.confidence, 0) + 1
        )
        space = (control.range.space if control.range is not None else None) or "none"
        range_spaces[space] = range_spaces.get(space, 0) + 1
        family = control.discrete.family if control.discrete is not None else "not_analyzed"
        families[family] = families.get(family, 0) + 1
    return {
        "engine_version": result.engine_version,
        "mode": result.mode,
        "identity": dict(result.identity),
        "parameters_inventoried": len(result.inventory.params),
        "controls_analyzed": len(controls),
        "controls_with_display": sum(1 for control in controls if control.evidence.get("mapping")),
        "controls_with_law": sum(
            1
            for control in controls
            if control.mapping is not None and control.mapping.chosen is not None
        ),
        "controls_with_range": sum(
            1 for control in controls if control.range is not None and control.range.chosen is not None
        ),
        "classes": classes,
        "class_confidence": confidences,
        "range_spaces": range_spaces,
        "discrete_families": families,
        "modules": [
            {
                "id": node.id,
                "label": node.label,
                "parent": node.parent,
                "kind": node.kind,
                "status": node.status,
                "confidence": node.confidence,
                "controls": len(node.controls),
                "signals": list(node.signals),
            }
            for node in result.grouping.nodes[:module_limit]
        ],
        "module_signals_used": list(result.grouping.signals_used),
        "unknown": unknown_census(result, limit=disputed_limit),
        "formats": list(result.formats),
        "cost": result.cost.to_dict(),
        "warnings": [dict(row) for row in result.warnings],
        "counts": dict(result.counts),
    }


def disputed(result: DiscoveryResult, *, limit: int = 25) -> list[dict[str, Any]]:
    """Controls with a disagreement or an unresolved field."""
    rows: list[dict[str, Any]] = []
    for control in result.controls:
        conflicts: list[dict[str, Any]] = []
        if control.range is not None and control.range.conflict is not None:
            conflicts.append(control.range.conflict.to_dict())
        if control.mapping is not None and control.mapping.chosen is None and control.mapping.reason:
            conflicts.append({"field": "mapping", "reason": control.mapping.reason})
        if conflicts or control.unknown:
            rows.append(
                {
                    "index": control.index,
                    "name": control.name,
                    "module": control.module,
                    "class": control.classification.kind,
                    "unknown": [dict(item) for item in control.unknown],
                    "conflicts": conflicts[:3],
                }
            )
        if len(rows) >= limit:
            break
    return rows


def unknown_census(result: DiscoveryResult, *, limit: int = 25) -> dict[str, Any]:
    """Every unresolved field, grouped by reason, with named examples."""
    by_reason: dict[str, list[str]] = {}
    for control in result.controls:
        for row in control.unknown:
            reason = str(row.get("reason") or "unknown")
            label = control.name or f"#{control.index}"
            by_reason.setdefault(reason, []).append(f"{label}:{row.get('field')}")
    return {
        "total": sum(len(items) for items in by_reason.values()),
        "by_reason": {reason: len(items) for reason, items in sorted(by_reason.items())},
        "examples": {reason: items[:limit] for reason, items in sorted(by_reason.items())},
    }


def format_text(result: DiscoveryResult, *, limit: int = 12) -> str:
    """A short human-readable rendering of the summary."""
    summary = summarize(result, disputed_limit=limit)
    lines: list[str] = []
    lines.append(
        f"discovery {summary['engine_version']} ({summary['mode']}) for "
        f"{summary['identity'].get('fx_name')!r}"
    )
    lines.append(
        f"  parameters inventoried: {summary['parameters_inventoried']}, "
        f"analyzed: {summary['controls_analyzed']}"
    )
    lines.append(
        "  established: "
        f"{summary['controls_with_law']} conversion shape(s), "
        f"{summary['controls_with_range']} range(s)"
    )
    lines.append("  classes: " + ", ".join(f"{key}={value}" for key, value in sorted(summary['classes'].items())))
    lines.append(
        "  module tree: "
        + ", ".join(f"{row['label']}({row['controls']})" for row in summary["modules"][:limit])
    )
    unknown = summary["unknown"]
    if unknown["total"]:
        lines.append("  unresolved fields: " + ", ".join(f"{key}={value}" for key, value in unknown["by_reason"].items()))
    cost = summary["cost"]
    lines.append(
        f"  cost: {cost['total_calls']} host call(s) in {cost['total_seconds']} s "
        f"({', '.join(f'{key}:{value}' for key, value in cost['calls_by_stage'].items())})"
    )
    if summary["warnings"]:
        lines.append(f"  warnings: {len(summary['warnings'])}")
    return "\n".join(lines)
