"""Plan building and world projection (05 §2.7, 11 §2.2, 10 §3.1).

A plan is data: building it writes nothing. Dependency findings are evaluated
against the **projected** world - the live state overlaid with the planned
changes - so a correct patch is not falsely refused because the snapshot's flags
were stale.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .model import (
    Plan,
    PlanItem,
    PluginInstanceRef,
    ResolvedRequest,
    StateRevision,
    TargetedStateSlice,
)
from . import values as V

__all__ = [
    "LiveState",
    "build_plan",
    "state_changes",
    "parameter_pairs",
    "project_world",
    "active_route_count",
]


@dataclass
class LiveState:
    """The live state a plan is built against (05 §2.7 invariant b)."""

    document: Any
    routes: list[dict[str, Any]]
    slice: TargetedStateSlice
    revision: StateRevision | None = None
    parameters: dict[str, float] = field(default_factory=dict)
    parameter_readback: dict[str, float] = field(default_factory=dict)


def active_route_count(routes: list[dict[str, Any]]) -> int:
    return sum(1 for route in routes if route.get("destination"))


def state_changes(resolved: ResolvedRequest) -> dict[str, Any]:
    """The document change set for the codec (state channel only)."""
    paths: dict[str, Any] = {}
    for row in resolved.controls.values():
        if row.get("channel") == "state":
            path = (row.get("binding") or {}).get("state_key")
            if path:
                paths[str(path)] = row["native"]
    paths.update({str(path): value for path, value in resolved.state.items()})
    return {
        "paths": paths,
        "routes_create": [dict(spec) for spec in resolved.routes_create],
        "routes_update": [dict(spec) for spec in resolved.routes_update],
        "routes_remove": [dict(spec) for spec in resolved.routes_remove],
    }


def parameter_pairs(resolved: ResolvedRequest) -> list[tuple[str, float]]:
    """The one batched parameter write: (ident, normalized) in plan order."""
    pairs: list[tuple[str, float]] = []
    for row in resolved.controls.values():
        if row.get("channel") != "parameter":
            continue
        ident = (row.get("binding") or {}).get("ident")
        if ident is None:
            continue
        pairs.append((str(ident), float(row["normalized"])))
    return pairs


def build_plan(
    *,
    profile: dict[str, Any],
    ref: PluginInstanceRef | None,
    resolved: ResolvedRequest,
    live: LiveState,
    label: str = "",
) -> Plan:
    items: list[PlanItem] = []
    for control_id, row in resolved.controls.items():
        control = row["control"]
        channel = row["channel"]
        if channel == "parameter":
            ident = (row.get("binding") or {}).get("ident")
            current = live.parameter_readback.get(str(ident)) if ident is not None else None
            items.append(
                PlanItem(
                    subject={"control": control_id},
                    channel="parameter",
                    binding=dict(row.get("binding") or {}),
                    from_=V.from_native(control, V.normalized_to_native(control, current))
                    if current is not None
                    else None,
                    to=row["display"],
                    value_space="display",
                )
            )
        else:
            path = (row.get("binding") or {}).get("state_key")
            items.append(
                PlanItem(
                    subject={"control": control_id},
                    channel="state",
                    path=str(path) if path else None,
                    binding=dict(row.get("binding") or {}),
                    to=row["native"],
                    value_space="native",
                )
            )
    for spec in resolved.routes_create:
        items.append(PlanItem(subject={"route_create": dict(spec)}, channel="state", to=dict(spec)))
    for spec in resolved.routes_update:
        items.append(PlanItem(subject={"route_update": dict(spec)}, channel="state", to=dict(spec)))
    for spec in resolved.routes_remove:
        items.append(PlanItem(subject={"route_remove": dict(spec)}, channel="state", to=dict(spec)))
    for path, value in resolved.state.items():
        items.append(PlanItem(subject={"state_path": str(path)}, channel="state", path=str(path), to=value))

    return Plan(
        label=label or "plugin_write",
        instance_ref=ref,
        profile_key=str(profile.get("provider", "unknown")),
        changes=items,
        disable_set={},
        warnings=[],
        findings=[],
        write_order=("state", "parameters", "verify"),
        n_slots_in_use=active_route_count(live.routes),
    )


def project_world(live: LiveState, resolved: ResolvedRequest) -> dict[str, Any]:
    """Live state overlaid with the planned changes, as plain signals."""
    routes = active_route_count(live.routes)
    removed_slots = {int(spec["slot"]) for spec in resolved.routes_remove}
    for route in live.routes:
        if int(route.get("slot", -1)) in removed_slots and route.get("destination"):
            routes -= 1
    routes += len(resolved.routes_create)
    controls_changed: dict[str, Any] = {}
    for control_id, row in resolved.controls.items():
        controls_changed[control_id] = row["native"]
    state_paths_changed = dict(state_changes(resolved)["paths"])
    return {
        "active_routes": max(0, routes),
        "routes_create": len(resolved.routes_create),
        "routes_remove": len(resolved.routes_remove),
        # The **specs** ride alongside the counts: a guard whose subject is the
        # (source, destination) pair a plan creates cannot be built from a count,
        # and Core already has them (owner decision, 2026-10-11).
        "routes_create_specs": [dict(spec) for spec in resolved.routes_create],
        "routes_update_specs": [dict(spec) for spec in resolved.routes_update],
        "controls_changed": controls_changed,
        "state_paths_changed": state_paths_changed,
        "n_slots_total": len(live.routes),
    }
