"""Modulation operations and the dependency/depth hooks (10 §2-§3, 06 §9).

This module is Core: it owns route *selection* semantics, the mutation/read
split, the depth-conversion call and the assembled result. The provider owns
route *meaning* (its document's route table, its vocabulary, its depth fits, its
dependency rules) and Core never invents one.

Rules enforced here, not by convention:

- **reads may be ambiguous, mutations may not.** Listing a selector that matches
  several routes acts on the deterministic first match *and reports the
  remaining matches as data*; a mutation with the same selector is a typed
  refusal listing every candidate (B-48, AUD-009).
- **a mutation target is never picked silently**, and route ops travel through
  the P5 codec + P2 engine: ordered units, a live slice re-read between them,
  per-unit verification.
- **a depth refusal is a first-class answer** (`NoConversion` with the declared
  reason), never an approximation.
- **a hard dependency stops the operation before the first write**, evaluated
  against the projected world (the engine already does this; the tests here
  re-prove it at this layer).
- `dry_run` defaults to true: without an explicit ``dry_run: false`` no write
  verb is ever called.
- `force` is **refused**, not echoed: V1 implements no override for a policy
  refusal, and an argument that looks like a permission must not be accepted
  while nothing honours it (Decision 4).

The route-table convention (`document["routes"]`, entries identified by their
provider `slot`) is the one the P2 engine already relies on for projection; the
provider codec is responsible for exposing it in its ``DocumentView``. That is
recorded in the P6 design note.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import values as V
from .errors import InvalidRoute, NoConversion, PluginError, UnsupportedOverride
from .model import (
    FinalState,
    HostAccounting,
    Outcome,
    Plan,
    PluginInstanceRef,
    Stage,
    Verdict,
)
from .transaction import Transaction, TransactionResult

__all__ = [
    "ModulationResult",
    "route_table",
    "slot_counts",
    "match_routes",
    "list_routes",
    "require_single_route",
    "depth_conversion",
    "read_live_routes",
    "modulation",
]

#: The universal route table lives under this key of the codec's DocumentView.
ROUTE_TABLE_KEY = "routes"


def route_table(document: Any) -> list[dict[str, Any]]:
    """The provider document's route table, as the P2 engine reads it.

    A provider whose document has no route channel yields an empty table; a
    malformed table is an empty table (the codec decodes its own document, so a
    non-list here means "no routes are addressable", not "guess").
    """
    if not isinstance(document, dict):
        return []
    rows = document.get(ROUTE_TABLE_KEY)
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _is_active(route: dict[str, Any]) -> bool:
    return bool(route.get("destination"))


def slot_counts(
    routes: list[dict[str, Any]], profile: dict[str, Any] | None = None
) -> dict[str, int]:
    """Total and free slots: declared by the profile when it does, else the table."""
    modulation = (profile or {}).get("modulation") if isinstance(profile, dict) else {}
    declared = modulation.get("slots") if isinstance(modulation, dict) else None
    used = {int(route["slot"]) for route in routes if _is_active(route) and "slot" in route}
    if isinstance(declared, int) and declared > 0:
        total = declared
    else:
        total = max([0, *[int(route.get("slot", 0)) for route in routes]])
    return {"n_slots_total": int(total), "n_free": max(0, int(total) - len(used))}


def match_routes(
    routes: list[dict[str, Any]], selector: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """Routes a selector matches, in provider (document) order.

    Supported selectors: ``{"slot": n}`` and ``{"source": s, "destination": d}``
    (either field may be omitted). An empty selector matches every active route.
    """
    selector = dict(selector or {})
    slot = selector.get("slot")
    source = selector.get("source")
    destination = selector.get("destination")
    matched: list[dict[str, Any]] = []
    for route in routes:
        if not _is_active(route):
            continue
        if slot is not None and int(route.get("slot", -1)) != int(slot):
            continue
        if source is not None and route.get("source") != source:
            continue
        if destination is not None and route.get("destination") != destination:
            continue
        matched.append(route)
    return matched


def list_routes(
    routes: list[dict[str, Any]], selector: dict[str, Any] | None, profile: dict[str, Any] | None
) -> dict[str, Any]:
    """Read-side semantics: deterministic first match **plus** the rest as data."""
    matched = match_routes(routes, selector)
    counts = slot_counts(routes, profile)
    return {
        "routes": matched,
        "selected": matched[0] if matched else None,
        "first_match": matched[0] if matched else None,
        "remaining": matched[1:],
        "n_matches": len(matched),
        "n_slots_total": counts["n_slots_total"],
        "n_free": counts["n_free"],
        "ambiguity": (
            "first match returned for a read; a mutation with this selector would be refused"
            if len(matched) > 1
            else None
        ),
    }


def require_single_route(
    routes: list[dict[str, Any]], selector: dict[str, Any] | None, *, op: str
) -> dict[str, Any]:
    """Mutation-side semantics: exactly one route, or a typed refusal (B-48)."""
    selector = dict(selector or {})
    if not selector:
        raise InvalidRoute(
            f"a {op} must name the route (slot or a selector resolving to exactly one route)",
            detail={"op": op, "reason": "missing_selector"},
        )
    if "slot" in selector:
        slot = int(selector["slot"])
        for route in routes:
            if int(route.get("slot", -1)) == slot:
                if not _is_active(route):
                    raise InvalidRoute(
                        f"route slot {slot} is empty; a {op} must name an active route",
                        detail={"op": op, "slot": slot, "reason": "empty_slot"},
                    )
                return route
        raise InvalidRoute(
            f"route slot {slot} does not exist on this instance",
            detail={
                "op": op,
                "slot": slot,
                "n_slots_total": slot_counts(routes)["n_slots_total"],
                "reason": "unknown_slot",
            },
        )
    matched = match_routes(routes, selector)
    if not matched:
        raise InvalidRoute(
            f"no active route matches the {op} selector",
            detail={"op": op, "selector": selector, "reason": "no_match"},
        )
    if len(matched) > 1:
        raise InvalidRoute(
            f"the selector matches {len(matched)} routes; a {op} is refused rather than "
            "choosing one (list first, then name the slot)",
            detail={
                "op": op,
                "selector": selector,
                "reason": "ambiguous_selector",
                "candidates": [
                    {"slot": route.get("slot"), "source": route.get("source"),
                     "destination": route.get("destination")}
                    for route in matched
                ],
            },
        )
    return matched[0]


def depth_conversion(
    provider: Any,
    profile: dict[str, Any],
    destination: str,
    depth: Any,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """The provider's depth hook, with the refusal preserved as an answer.

    ``force`` is declared for contract compatibility and is **always False**:
    V1 implements no override, and the tool boundary refuses ``force=True``
    before this function is reached (Decision 4). It never turns a refusal into
    a guessed factor (10 §2.2/§2.3).
    """
    if depth is None:
        raise NoConversion(
            "a depth conversion needs a semantic depth value",
            detail={"destination": destination, "reason": "missing_depth"},
        )
    raw = provider.convert_depth(profile, destination, depth)
    native = float(raw)
    result: dict[str, Any] = {
        "destination": destination,
        "depth": depth,
        "native_amount": native,
        # never an echo of an override: V1 implements none, and a `true` here would
        # read as "a policy was overridden" (Decision 4)
        "force": False,
        "reason": None,
    }
    control = _control_for(profile, destination)
    if control is not None:
        result["law"] = dict(control.get("law") or {})
        result["evidence"] = dict(control.get("evidence") or {})
    return result


def _control_for(profile: dict[str, Any], name: str) -> dict[str, Any] | None:
    lowered = str(name).strip().lower()
    for control in profile.get("controls") or []:
        if not isinstance(control, dict):
            continue
        candidates = {str(control.get("id", "")).lower(), str(control.get("label", "")).lower()}
        candidates.update(str(alias).lower() for alias in control.get("aliases") or [])
        if lowered in candidates:
            return control
    return None


def read_live_routes(
    bridge: Any, codec: Any, ref: PluginInstanceRef
) -> tuple[list[dict[str, Any]], Any]:
    """Read-only: the live document and its route table for the addressed slot."""
    track_index = int(ref.location.get("track_index", 0))
    fx_index = int(ref.location.get("fx_index", 0))
    facts = bridge.fx_facts(track_index, fx_index)
    chunk = bridge.read_state_chunk(ref)
    slice_ = bridge.locate_state_slice(ref, facts, chunk)
    document = codec.decode(slice_)
    return route_table(document), document


@dataclass
class ModulationResult:
    """The modulation call's answer: data, the typed error, and the transaction
    (present for every mutation attempt, so its outcome travels with it)."""

    ok: bool
    op: str
    data: dict[str, Any] = field(default_factory=dict)
    error: PluginError | None = None
    transaction: TransactionResult | None = None
    preflight_outcome: Outcome | None = None

    @property
    def outcome(self) -> Any:
        if self.transaction is not None:
            return self.transaction.outcome
        return self.preflight_outcome

    def to_envelope(self, tool: str = "plugin_modulation") -> dict[str, Any]:
        from .tools import error_envelope, ok_envelope

        outcome = None
        if self.outcome is not None:
            outcome = self.outcome.to_dict()
        if self.ok:
            return ok_envelope(tool, self.data, outcome=outcome)
        return error_envelope(tool, self.error or InvalidRoute("modulation failed"), outcome=outcome)


_MUTATIONS = ("create", "update", "remove")


def modulation(
    *,
    provider: Any,
    bridge: Any,
    profile: dict[str, Any],
    ref: PluginInstanceRef,
    token: Any,
    registry: Any,
    op: str,
    routes: list[dict[str, Any]] | None = None,
    selector: dict[str, Any] | None = None,
    destination: str | None = None,
    depth: Any = None,
    force: bool = False,
    dry_run: bool = True,
    cancel: Any = None,
    timing: Any = None,
) -> ModulationResult:
    """One modulation call. Reads touch no write verb; mutations go through the
    P2 engine with ``dry_run`` defaulting to true."""
    op = str(op)
    dry_run = V.strict_bool(dry_run, "dry_run")
    forced = V.strict_bool(force, "force")
    if forced:
        # defence in depth: the tool boundary refuses first, and a direct Core
        # caller must not be able to reach a write with an override that
        # nothing implements (Decision 4)
        raise UnsupportedOverride(
            "`force` is not supported in V1: no override path exists for a policy "
            "refusal, so the request is refused rather than silently ignoring it",
            detail={"argument": "force", "supported": False, "op": op, "reason": "force_not_supported"},
        )
    force = False
    codec = provider.codec()
    try:
        if op == "list":
            routes_live, _document = read_live_routes(bridge, codec, ref)
            return ModulationResult(
                ok=True, op=op, data=list_routes(routes_live, selector, profile)
            )
        if op == "depth":
            if not destination:
                raise NoConversion(
                    "a depth conversion needs a destination", detail={"reason": "missing_destination"}
                )
            return ModulationResult(
                ok=True,
                op=op,
                data=depth_conversion(provider, profile, destination, depth, force=force),
            )
        if op not in _MUTATIONS:
            raise InvalidRoute(
                f"unknown modulation op {op!r}",
                detail={"op": op, "accepted": ["list", "create", "update", "remove", "depth"]},
            )
        request = _mutation_request(op, routes=routes, selector=selector, force=force)
        before_slots: set[int] = set()
        if op in ("update", "remove"):
            routes_live, _document = read_live_routes(bridge, codec, ref)
            target = require_single_route(routes_live, selector, op=op)
            request = _mutation_request(
                op, routes=routes, selector=selector, force=force, target=target
            )
        elif op == "create":
            routes_live, _document = read_live_routes(bridge, codec, ref)
            before_slots = {int(route.get("slot", -1)) for route in routes_live}
        transaction = Transaction(
            provider=provider,
            codec=codec,
            bridge=bridge,
            profile=profile,
            ref=ref,
            request=request,
            token=token,
            registry=registry,
            dry_run=bool(dry_run),
            cancel=cancel,
            timing=timing,
            label=f"plugin_modulation:{op}",
        )
        result = transaction.run()
        data = _mutation_data(op, result, request, dry_run=bool(dry_run), force=force)
        if op == "create" and result.ok and result.committed:
            data.update(_landed_slots(bridge, codec, ref, request, before_slots=before_slots))
        if result.ok:
            return ModulationResult(ok=True, op=op, data=data, transaction=result)
        return ModulationResult(
            ok=False, op=op, data=data, error=result.error, transaction=result
        )
    except PluginError as exc:
        return ModulationResult(
            ok=False,
            op=op,
            data={},
            error=exc,
            preflight_outcome=_preflight_outcome(exc),
        )


def _preflight_outcome(exc: PluginError) -> Outcome:
    """The honest outcome of a refusal decided **before** a transaction existed.

    Mutating calls carry an outcome (06 §2). This one states the measured fact
    the body can prove about itself: it issued no host write verb, because every
    host access on this path is a read helper; the refusal reason is recorded as
    a named gap so the outcome is not pure ceremony.
    """
    return Outcome(
        stage=Stage.PREFLIGHT,
        verdict=Verdict.REFUSED_BEFORE_ANY_WRITE,
        final_state=FinalState.UNCHANGED_VERIFIED,
        host=HostAccounting(),
        named_gaps=[{"channel": "modulation", "reason": f"{exc.code}: {exc.message}"}],
    )


def _mutation_request(
    op: str,
    *,
    routes: list[dict[str, Any]] | None,
    selector: dict[str, Any] | None,
    force: bool,
    target: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The request the provider normalizes.

    ``update``/``remove`` carry the **resolved identity** (the slot that the
    selector matched) plus only the fields the caller explicitly asked for: a
    resolved selector never smuggles the route's current values into the write.
    """
    request: dict[str, Any] = {"force": bool(force)}
    if op == "create":
        specs: list[dict[str, Any]] = []
        for entry in routes or []:
            spec = {
                key: entry[key]
                for key in ("source", "destination", "depth", "amount", "bipolar", "fields")
                if key in entry
            }
            specs.append(spec)
        request["routes_create"] = specs
        return request

    explicit: dict[str, Any] = {}
    for entry in routes or []:
        explicit.update(
            {key: entry[key] for key in ("amount", "depth", "bipolar", "fields") if key in entry}
        )
    slot = target.get("slot") if isinstance(target, dict) else None
    if slot is None and isinstance(selector, dict) and "slot" in selector:
        slot = selector["slot"]
    spec = {"slot": int(slot)} if slot is not None else {}
    spec.update(explicit)
    if op == "update":
        request["routes_update"] = [spec]
    else:
        request["routes_remove"] = [{"slot": int(slot)}] if slot is not None else [spec]
    return request


def _landed_slots(
    bridge: Any,
    codec: Any,
    ref: PluginInstanceRef,
    request: dict[str, Any],
    *,
    before_slots: set[int],
) -> dict[str, Any]:
    """The slots the created routes actually landed in, read from the live table.

    Never predicted: only slots that were **free before the write** are
    candidates, so a create of a pair that already exists cannot be reported as
    having landed in the pre-existing route's slot. If no new slot matches a
    create, the row says so instead of guessing.
    """
    routes_after, _document = read_live_routes(bridge, codec, ref)
    fresh = [
        route
        for route in routes_after
        if _is_active(route) and int(route.get("slot", -1)) not in before_slots
    ]
    landed: list[dict[str, Any]] = []
    for spec in request.get("routes_create") or []:
        match = next(
            (
                route
                for route in fresh
                if route.get("source") == spec.get("source")
                and route.get("destination") == spec.get("destination")
            ),
            None,
        )
        if match is None:
            landed.append(
                {
                    "resolved_slot": None,
                    "source": spec.get("source"),
                    "destination": spec.get("destination"),
                    "note": "no newly used slot in the read-back matches this create",
                }
            )
            continue
        fresh.remove(match)
        landed.append(
            {
                "resolved_slot": match.get("slot"),
                "source": match.get("source"),
                "destination": match.get("destination"),
                "amount": match.get("amount"),
            }
        )
    return {"route_plan": {"routes_create": landed}}


def _mutation_data(
    op: str, result: TransactionResult, request: dict[str, Any], *, dry_run: bool, force: bool
) -> dict[str, Any]:
    plan: Plan | None = result.plan
    data: dict[str, Any] = {
        "op": op,
        "dry_run": bool(dry_run),
        "committed": bool(result.committed),
        "force": False,
        "request": request,
        "dependency_findings": [
            {
                "rule": finding.rule,
                "kind": finding.kind,
                "message": finding.message,
                "subject": finding.subject,
                "informational": finding.informational,
                # Decision 1: a finding row carries whether the rule was **decided**.
                # Without these, a rule the provider declared but cannot evaluate
                # reads as checked.
                "evaluated": dict(getattr(finding, "detail", {}) or {}).get("evaluated", True),
                "reason": dict(getattr(finding, "detail", {}) or {}).get("reason"),
                "detail": dict(getattr(finding, "detail", {}) or {}),
            }
            for finding in (plan.findings if plan is not None else [])
        ],
        # the provider's honest list of rules it does not decide, on the mutation
        # surface as well as on plugin_write (the transaction carries it on every
        # result; `_mutation_data` used to drop it)
        "dependency_rules_not_evaluated": list(
            result.data.get("dependency_rules_not_evaluated") or []
        ),
        "would_write": bool(plan and plan.changes) if dry_run else bool(result.committed),
    }
    if result.outcome is not None:
        data["verdict"] = result.outcome.to_dict()["verdict"]
    return data
