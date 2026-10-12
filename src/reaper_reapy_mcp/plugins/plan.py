"""Plan building and world projection (05 §2.7, 11 §2.2, 10 §3.1).

A plan is data: building it writes nothing. Dependency findings are evaluated
against the **projected** world - the live state overlaid with the planned
changes - so a correct patch is not falsely refused because the snapshot's flags
were stale.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

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
    parameter_key: Callable[[dict[str, Any]], str | None] | None = None,
    write_order: tuple[str, ...] = ("state", "parameters", "verify"),
) -> Plan:
    items: list[PlanItem] = []
    for control_id, row in resolved.controls.items():
        control = row["control"]
        channel = row["channel"]
        if channel == "parameter":
            binding = dict(row.get("binding") or {})
            # The address a parameter is tracked under is the caller's business: the
            # ident path keeps its historical `ident` key, the index path (P28) uses
            # `#<index>`, and `from_` must be read from the key the *live readback*
            # was actually recorded under - a miss here silently turns the baseline
            # into `None`, i.e. a preview that cannot show what it changes from.
            if parameter_key is not None:
                key = parameter_key(binding)
            else:
                ident = binding.get("ident")
                key = None if ident is None else str(ident)
            current = live.parameter_readback.get(key) if key is not None else None
            # Which space this item is stated in: a display request converts (so
            # both ends are display values), an explicit normalized request - or a
            # control with no verified law, whose only write target is the host
            # space - is stated in normalized values at both ends.
            if V.has_display_law(control):
                from_value = (
                    V.from_native(control, V.normalized_to_native(control, current))
                    if current is not None
                    else None
                )
                to_value = row["display"]
                value_space = "display"
            else:
                from_value = float(current) if current is not None else None
                to_value = float(row["normalized"])
                value_space = "normalized"
            items.append(
                PlanItem(
                    subject={"control": control_id},
                    channel="parameter",
                    binding=binding,
                    from_=from_value,
                    to=to_value,
                    value_space=value_space,
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
        write_order=tuple(write_order),
        n_slots_in_use=active_route_count(live.routes),
    )


def project_world(live: LiveState, resolved: ResolvedRequest) -> dict[str, Any]:
    """Live state overlaid with the planned changes, as plain signals.

    ``live_document`` is the provider-shaped decoded document, passed through
    **untouched**. It is the smallest honest way to let a provider rule whose
    deciding input is a *live flag* be evaluated: Core must not interpret the
    document to hand over a pre-digested flag (that would put provider
    semantics in Core), and a rule that cannot see the state its plan starts
    from can only be refused, never decided (L-10). The value is opaque data for
    the provider's own evaluator; nothing serializes the world - findings carry
    their own subjects.
    """
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
        #: provider-shaped opaque document (see the docstring)
        "live_document": live.document,
    }
