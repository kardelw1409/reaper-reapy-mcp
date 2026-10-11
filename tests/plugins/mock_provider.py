"""MockProvider + its state codec (codec A) - the primary L1/L2 test double.

Deliberately tiny and provider-agnostic in naming: a parameter/state control
table, a two-route model, one `%`-unit trap and one declared hard dependency
(12 §6). The codec's own block format is defined in `fixtures.py` and is
distinct from codec B's shape/batching (`mock_codec_b.py`), so no engine test
can depend on one provider's document shape (AUD-004).
"""

from __future__ import annotations

import copy
from typing import Any

import fixtures as F

from reaper_reapy_mcp.plugins.errors import InvalidRoute, InvalidValue, NoConversion
from reaper_reapy_mcp.plugins.model import (
    CaptureScope,
    Finding,
    FxHostFacts,
    ResolvedRequest,
    StateWritePlan,
    StateWriteUnit,
    TargetedStateSlice,
    VerificationPolicy,
    VerificationReport,
)
from reaper_reapy_mcp.plugins.provider import DependencyRuleSet

PSEUDO_SLOTS = F.ROUTE_SLOTS


class DemoCodec:
    """Codec A: one JSON document, base64-encoded into the block payload.

    Batching: all non-route path changes ride ONE state unit; every route
    operation gets its own sequential unit (the declared structural limit).
    """

    name = "b64-json-block-v1"

    # -- document <-> block -------------------------------------------------

    def decode(self, slice: TargetedStateSlice) -> dict[str, Any]:
        return F.decode_block_document(slice.block_text)

    def encode_slice(
        self, doc: dict[str, Any], unit: StateWriteUnit, current: TargetedStateSlice
    ) -> TargetedStateSlice:
        payload = unit.payload or {}
        new_doc = copy.deepcopy(doc)
        for path, value in (payload.get("paths") or {}).items():
            try:
                F.set_path(new_doc, path, value)
            except (KeyError, IndexError, TypeError) as exc:
                raise InvalidValue(
                    f"state path {path!r} is not rooted at a declared document key",
                    detail={"path": path, "reason": str(exc)},
                ) from exc
        route_op = payload.get("route_op")
        if route_op:
            self._apply_route_op(new_doc, route_op)
        return TargetedStateSlice(
            chunk_digest=current.chunk_digest,
            line_start=current.line_start,
            line_end=current.line_end,
            header_line=current.header_line,
            class_hex=current.class_hex,
            ordinal=current.ordinal,
            block_text=F.make_block_text(new_doc, header=current.header_line),
            framing=None,
        )

    def plan_state_writes(self, doc: dict[str, Any], changes: dict[str, Any]) -> StateWritePlan:
        paths = dict(changes.get("paths") or {})
        route_ops = [
            {"op": "create", **spec} for spec in (changes.get("routes_create") or [])
        ] + [
            {"op": "update", **spec} for spec in (changes.get("routes_update") or [])
        ] + [
            {"op": "remove", **spec} for spec in (changes.get("routes_remove") or [])
        ]
        units: list[StateWriteUnit] = []
        if paths:
            units.append(
                StateWriteUnit(order_index=len(units), kind="state", payload={"paths": paths})
            )
        for op in route_ops:
            units.append(
                StateWriteUnit(order_index=len(units), kind="state", payload={"route_op": op})
            )
        return StateWritePlan(
            units=units,
            rollback_plan=[{"kind": "state", "why": "restore captured document then parameters"}],
            verification_plan={"space": "native", "tolerances": {"state.float": 1e-05}},
        )

    def verify(
        self, readback: dict[str, Any], unit: StateWriteUnit, policy: VerificationPolicy
    ) -> VerificationReport:
        tolerance = float(policy.tolerances.get("state.float", 1e-05))
        rows: list[dict[str, Any]] = []
        payload = unit.payload or {}
        for path, value in (payload.get("paths") or {}).items():
            actual = F.get_path(readback, path, default=None)
            rows.append(
                {
                    "path": path,
                    "written": value,
                    "read_back": actual,
                    "agrees": _agrees(value, actual, tolerance),
                    "channel": "state",
                }
            )
        route_op = payload.get("route_op")
        if route_op:
            slot = route_op.get("slot")
            landed = _find_route(readback, slot) if slot is not None else _find_route_by_pair(
                readback, route_op.get("source"), route_op.get("destination")
            )
            if route_op["op"] == "remove":
                agrees = _route_is_empty(landed)
                rows.append(
                    {"subject": {"route": slot}, "written": "removed", "read_back": landed,
                     "agrees": agrees, "channel": "state"}
                )
            else:
                # compare ONLY the fields the operation actually wrote: an update
                # of the amount says nothing about the source/destination it kept,
                # so verifying them would report a mismatch that never happened
                fields = {
                    key: value
                    for key, value in route_op.items()
                    if key not in ("op", "slot") and value is not None
                }
                for key, value in fields.items():
                    actual = landed.get(key) if landed else None
                    rows.append(
                        {
                            "subject": {"route": landed.get("slot") if landed else slot},
                            "field": key,
                            "written": value,
                            "read_back": actual,
                            "agrees": _agrees(value, actual, tolerance),
                            "channel": "state",
                        }
                    )
                if not fields:
                    rows.append(
                        {
                            "subject": {"route": slot},
                            "written": "addresses an existing route",
                            "read_back": bool(landed),
                            "agrees": bool(landed),
                            "channel": "state",
                        }
                    )
        mismatched = [row for row in rows if not row["agrees"]]
        return VerificationReport(
            ok=bool(rows) and not mismatched,
            rows=rows,
            mismatched=mismatched,
            comparators=dict(policy.comparators),
            tolerances=dict(policy.tolerances),
            space="native",
        )

    def snapshot_document(self, doc: dict[str, Any]) -> dict[str, Any]:
        return {"document": copy.deepcopy(doc), "routes": copy.deepcopy(doc.get("routes", []))}

    def restore_changes(self, doc: dict[str, Any], snap: dict[str, Any]) -> dict[str, Any]:
        """Restoration as document changes: every leaf returns to its captured
        value (executed through ``plan_state_writes``, never a blob write)."""
        return {"paths": F.flat_paths(snap["document"])}

    # -- internals ----------------------------------------------------------

    def _apply_route_op(self, doc: dict[str, Any], op: dict[str, Any]) -> None:
        routes = doc.setdefault("routes", [])
        kind = op.get("op")
        if kind == "create":
            used = {route.get("slot") for route in routes if route.get("destination")}
            free = next(
                (slot for slot in range(1, PSEUDO_SLOTS + 1) if slot not in used), None
            )
            if free is None:
                raise InvalidRoute("no free route slot", detail={"slots": PSEUDO_SLOTS})
            routes.append(
                {
                    "slot": free,
                    "source": op.get("source", ""),
                    "destination": op.get("destination", ""),
                    "amount": float(op.get("amount", 0.0)),
                    "bipolar": bool(op.get("bipolar", False)),
                }
            )
            return
        slot = op.get("slot")
        route = _find_route(doc, slot)
        if route is None:
            raise InvalidRoute(
                f"route slot {slot!r} does not exist", detail={"slot": slot}
            )
        if kind == "remove":
            if not route.get("destination"):
                raise InvalidRoute(
                    f"route slot {slot!r} is already empty", detail={"slot": slot}
                )
            route.update({"source": "", "destination": "", "amount": 0.0, "bipolar": False})
            return
        if kind == "update":
            for key in ("source", "destination"):
                if op.get(key) is not None:
                    route[key] = op[key]
            if op.get("amount") is not None:
                route["amount"] = float(op["amount"])
            if op.get("bipolar") is not None:
                route["bipolar"] = bool(op["bipolar"])
            return
        raise InvalidRoute(f"unknown route operation {kind!r}", detail={"op": kind})


def _find_route(doc: dict[str, Any], slot: Any) -> dict[str, Any] | None:
    for route in doc.get("routes", []):
        if route.get("slot") == slot:
            return route
    return None


def _find_route_by_pair(doc: dict[str, Any], source: Any, destination: Any) -> dict[str, Any] | None:
    for route in doc.get("routes", []):
        if route.get("source") == source and route.get("destination") == destination:
            return route
    return None


def _route_is_empty(route: dict[str, Any] | None) -> bool:
    return bool(route) and not route.get("destination")


def _agrees(written: Any, read_back: Any, tolerance: float) -> bool:
    if isinstance(written, bool) or isinstance(read_back, bool):
        return written == read_back
    if isinstance(written, (int, float)) and isinstance(read_back, (int, float)):
        return abs(float(written) - float(read_back)) <= tolerance
    return written == read_back


class MockProvider:
    """A tiny parameter/state provider used by every offline engine test."""

    id = F.DEMO_PROVIDER
    implementation_revision = "demo-provider-r1"

    def __init__(self, *, profile: dict[str, Any] | None = None, codec: Any | None = None,
                 document_version: Any = None) -> None:
        self._profile = profile or F.demo_profile()
        self._codec = codec or DemoCodec()
        #: Optional live-version source (07 §3.2). ``None`` means "this provider
        #: declares no document-side version" - which is the honest default and
        #: is what makes the hook's optionality testable.
        self._document_version = document_version

    # -- contract -----------------------------------------------------------

    def artifact_paths(self) -> list[str]:
        return [__file__]

    def matches(self, facts: FxHostFacts) -> bool:
        return facts.class_id == F.DEMO_CLASS and (facts.module or "") == F.DEMO_MODULE

    def identify(self, facts: FxHostFacts) -> dict[str, Any]:
        identity: dict[str, Any] = {
            "fx_name": facts.fx_name,
            "class_id": facts.class_id,
            "module": facts.module,
            "param_count": facts.param_count,
        }
        if facts.version is not None:
            identity["version"] = facts.version
        return identity

    def build_profile(self, identity: dict[str, Any]) -> dict[str, Any]:
        profile = copy.deepcopy(self._profile)
        profile["identity"].update({k: v for k, v in identity.items() if k != "version"})
        return profile

    def controls(self, profile: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return list((profile or self._profile).get("controls", []))

    def document_version(self, document: Any) -> str | None:
        """Optional live-version hook (07 §3.2), exactly as the Vital provider has.

        Returns ``None`` unless a source was configured, which is the honest
        default: the engine must treat "no document-side version" as "no
        version gate", not as a version of ``None``.
        """
        if self._document_version is None:
            return None
        return self._document_version(document)

    def control_by_name(self, name: str, profile: dict[str, Any] | None = None) -> dict[str, Any] | None:
        lowered = str(name).strip().lower()
        for control in self.controls(profile):
            candidates = {
                str(control.get("id", "")).lower(),
                str(control.get("label", "")).lower(),
                str((control.get("binding") or {}).get("state_key", "")).lower(),
            }
            candidates.update(str(alias).lower() for alias in control.get("aliases") or [])
            if lowered in candidates:
                return control
        return None

    def normalize_request(self, profile: dict[str, Any], request: dict[str, Any]) -> ResolvedRequest:
        resolved = ResolvedRequest()
        controls_in = dict(request.get("controls") or {})
        for name, value in controls_in.items():
            control = self.control_by_name(name, profile)
            if control is None:
                resolved.problems.append(
                    {
                        "control": name,
                        "code": "UnsupportedControl",
                        "message": f"unknown control {name!r}",
                        "detail": {"suggestions": _suggest(name, self.controls(profile))},
                    }
                )
                continue
            try:
                native, normalized = _control_value(control, value)
            except (InvalidValue, NoConversion) as exc:
                resolved.problems.append(
                    {"control": name, "code": exc.code, "message": exc.message, "detail": exc.detail}
                )
                continue
            binding = dict(control.get("binding") or {})
            if control["authority"]["write"] == "parameter" and not binding.get("ident"):
                # mirrors the Vital provider: an unaddressable control refuses
                # instead of being silently dropped from the write batch
                unverified = binding.get("unverified") or {}
                resolved.problems.append(
                    {
                        "control": name,
                        "code": "UnsupportedControl",
                        "message": "the control has no verified live ident binding",
                        "detail": {
                            "control": control["id"],
                            "index": binding.get("index"),
                            "reason": str(unverified.get("reason") or "ident_not_bound"),
                            "observed_name": unverified.get("observed_name"),
                        },
                    }
                )
                continue
            resolved.controls[control["id"]] = {
                "control": control,
                "display": value,
                "native": native,
                "normalized": normalized,
                "channel": control["authority"]["write"],
                "binding": binding,
            }

        modulation = profile.get("modulation", {})
        sources = set(modulation.get("sources") or [])
        destinations = set(modulation.get("destinations") or [])
        for spec in request.get("routes_create") or []:
            source, destination = spec.get("source"), spec.get("destination")
            if source not in sources or destination not in destinations:
                resolved.problems.append(
                    {
                        "route": spec,
                        "code": "InvalidRoute",
                        "message": f"route vocabulary violation: {source!r} -> {destination!r}",
                        "detail": {"sources": sorted(sources), "destinations": sorted(destinations)},
                    }
                )
                continue
            if spec.get("amount") is None and spec.get("depth") is not None:
                try:
                    amount = self.convert_depth(profile, destination, spec["depth"])
                except NoConversion as exc:
                    resolved.problems.append(
                        {"route": spec, "code": exc.code, "message": exc.message, "detail": exc.detail}
                    )
                    continue
            else:
                amount = float(spec.get("amount") or 0.0)
            resolved.routes_create.append(
                {"source": source, "destination": destination, "amount": amount}
            )

        for spec in request.get("routes_update") or []:
            if "slot" not in spec:
                resolved.problems.append(
                    {
                        "route": spec,
                        "code": "InvalidRoute",
                        "message": "an update must name the route's slot identity",
                        "detail": {"spec": spec},
                    }
                )
                continue
            resolved.routes_update.append(
                {
                    "slot": int(spec["slot"]),
                    **{key: spec[key] for key in ("amount", "bipolar") if spec.get(key) is not None},
                }
            )

        for spec in request.get("routes_remove") or []:
            if "slot" not in spec:
                resolved.problems.append(
                    {
                        "route": spec,
                        "code": "InvalidRoute",
                        "message": "a remove must name the route's slot identity",
                        "detail": {"spec": spec},
                    }
                )
                continue
            resolved.routes_remove.append({"slot": int(spec["slot"])})

        for path, value in dict(request.get("state") or {}).items():
            if not str(path).startswith("settings."):
                resolved.problems.append(
                    {
                        "path": path,
                        "code": "InvalidValue",
                        "message": f"state path {path!r} is not rooted at a declared key",
                        "detail": {"roots": ["settings"]},
                    }
                )
                continue
            resolved.state[str(path)] = value

        resolved.structural = bool(request.get("structural"))
        return resolved

    def convert_depth(self, profile: dict[str, Any], destination: str, depth: Any) -> float:
        destinations = set((profile.get("modulation") or {}).get("destinations") or [])
        if destination not in destinations:
            raise NoConversion(
                f"no depth conversion for destination {destination!r}",
                detail={"destination": destination, "reason": "unknown destination"},
            )
        if destination == "tone_cutoff":
            raise NoConversion(
                f"depth for {destination!r} is not affine and is refused, never approximated",
                detail={"destination": destination, "reason": "non_affine"},
            )
        return float(depth) / 10.0

    def dependencies(self) -> DependencyRuleSet:
        rules = list(self._profile.get("dependencies") or [])

        def evaluate(world: dict[str, Any], rule_rows: Any) -> list[Finding]:
            findings: list[Finding] = []
            for rule in rule_rows:
                when = rule.get("when") or {}
                path = when.get("state_path")
                minimum = int(when.get("min_active_routes", 1))
                changed = (world.get("state_paths_changed") or {})
                if path and path in changed and int(world.get("active_routes", 0)) >= minimum:
                    findings.append(
                        Finding(
                            kind=rule.get("kind", "hard"),
                            rule=rule.get("id", "rule"),
                            subject={"state_path": path},
                            message=rule.get("message", ""),
                            detail={"active_routes": world.get("active_routes")},
                            informational=bool(rule.get("informational", False)),
                        )
                    )
            return findings

        return DependencyRuleSet(rules=rules, evaluate=evaluate)

    def codec(self) -> DemoCodec:
        return self._codec

    def write_order(self) -> tuple[str, ...]:
        return ("state", "parameters")

    def verification_policy(self) -> VerificationPolicy:
        return VerificationPolicy(
            tolerances={"state.float": 1e-05, "parameter.normalized": 0.001},
            comparators={"enum": "exact", "number": "abs_le"},
        )

    def capture_scope(self) -> CaptureScope:
        return CaptureScope(
            channels=["document", "routes", "parameters"],
            parameters={"scope": "full_vector", "ident_required": True},
            restore_order=["document", "parameters"],
            verify_order=["document", "parameters"],
        )

    def limits(self) -> list[str]:
        return list(self._profile.get("limits") or [])


def _control_value(control: dict[str, Any], value: Any) -> tuple[Any, float]:
    """Minimal provider-side conversion: display -> (native, normalized)."""
    ctype = control.get("type")
    unit = control.get("unit")
    if unit == "%" and isinstance(value, (int, float)) and float(value) == 1.0:
        raise InvalidValue(
            "a 1.0 on a percent-unit control means 1 percent - use the value in percent",
            detail={"control": control.get("id"), "unit": "%", "value": value, "trap": True},
        )
    if ctype == "enum":
        members = [str(member.get("id")) for member in control.get("enum_values") or []]
        if str(value) not in members:
            raise InvalidValue(
                f"unknown enum member {value!r}",
                detail={"control": control.get("id"), "legal": members},
            )
        index = members.index(str(value))
        native = float(index)
        normalized = index / max(1, len(members) - 1)
        return native, normalized
    if ctype == "bool":
        if isinstance(value, bool):
            return float(value), float(value)
        raise InvalidValue(
            f"expected a boolean, got {value!r}", detail={"control": control.get("id")}
        )
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidValue(
            f"expected a number, got {value!r}", detail={"control": control.get("id")}
        )
    bounds = control.get("range") or {}
    low, high = bounds.get("min"), bounds.get("max")
    if low is not None and high is not None and not (float(low) <= float(value) <= float(high)):
        raise InvalidValue(
            f"value {value!r} outside the declared range",
            detail={"control": control.get("id"), "range": {"min": low, "max": high}},
        )
    native = float(value)
    law = control.get("law") or {}
    if law.get("kind") in ("none", "unmeasured"):
        raise NoConversion(
            f"control {control.get('id')!r} declares no conversion (law kind {law.get('kind')!r})",
            detail={"control": control.get("id"), "law": law.get("kind")},
        )
    if law.get("kind") == "affine":
        scale = float(law.get("params", {}).get("scale", 1.0))
        offset = float(law.get("params", {}).get("offset", 0.0))
        native = scale * native + offset
    if str(control.get("authority", {}).get("write")) == "state":
        normalized = native
    elif low is not None and high is not None and float(high) != float(low):
        normalized = (native - float(low)) / (float(high) - float(low))
    else:
        normalized = native
    return native, normalized


def _suggest(name: str, controls: list[dict[str, Any]], limit: int = 3) -> list[str]:
    lowered = str(name).lower()
    scored = []
    for control in controls:
        control_id = str(control.get("id", ""))
        score = sum(1 for token in lowered.split("_") if token and token in control_id.lower())
        if score:
            scored.append((score, control_id))
    scored.sort(key=lambda row: (-row[0], row[1]))
    return [control_id for _, control_id in scored[:limit]]
