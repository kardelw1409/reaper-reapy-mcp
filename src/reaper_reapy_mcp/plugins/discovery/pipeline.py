"""Orchestration: one discovery run, its budgets, timings and checkpoints (P26).

The pipeline is deliberately linear and re-entrant: every stage reads the
previous stage's data structures and writes its own, so a stage can be re-run on
a decoded artifact without a live host (that is what `replay` does). Nothing here
touches the host directly - the bridge arrives as an object, which is what lets
the offline suite drive the identical code path with a recorded inventory and a
recorded observation set.

Cost discipline: the run reports calls and seconds **per stage**, the limits it
hit, and a checkpoint per stage. A limit that fires leaves the affected result
marked incomplete (`budget_exhausted`) rather than claiming coverage.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from ..discovery import ENGINE_VERSION
from .classify import classify_control
from .discrete import analyze_discrete
from .formats import probe_adapters
from .inventory import collect_inventory, facts_identity
from .mapping import analyze_range, display_bounds, fit_mapping, numeric_samples
from .modules import infer_modules
from .observe import ObservationStore, SamplingBudget, grid_positions
from .text import printed_resolution, unit_token_of
from .types import ControlObservations, ControlResult, DiscoveryResult, Inventory, MappingAnalysis, RunCost

__all__ = ["EngineConfig", "DiscoveryEngine", "run_discovery"]

#: Modes the engine offers. ``inventory`` reads metadata only; ``prepare`` adds
#: the display sweep; ``deep`` adds adaptive refinement of disputed controls.
MODES: tuple[str, ...] = ("inventory", "prepare", "deep")


@dataclass
class EngineConfig:
    """One run's configuration. Every limit is explicit and reported."""

    mode: str = "prepare"
    grid_points: int = 17
    refine_budget: int = 0
    max_controls: int | None = None
    sample_indices: Sequence[int] | None = None
    batch_size: int = 2048
    #: Parameter indices per host metadata call. The inventory is chunked because a
    #: single call for a whole instance can exceed any sane timeout (measured live).
    chunk_size: int = 256
    refine_unlabelled: bool = False
    formats: Sequence[str] | None = None
    budget: SamplingBudget = field(default_factory=SamplingBudget)
    channels: tuple[str, ...] = ("name", "ident", "value", "steps", "formatted", "named", "section")

    def resolved(self, *, mode: str | None = None) -> "EngineConfig":
        chosen = str(mode or self.mode)
        if chosen not in MODES:
            raise ValueError(f"unknown discovery mode {chosen!r}; expected one of {MODES}")
        config = EngineConfig(
            mode=chosen,
            grid_points=self.grid_points,
            refine_budget=self.refine_budget,
            max_controls=self.max_controls,
            sample_indices=self.sample_indices,
            batch_size=self.batch_size,
            chunk_size=self.chunk_size,
            refine_unlabelled=self.refine_unlabelled,
            formats=self.formats,
            budget=self.budget,
            channels=self.channels,
        )
        if chosen == "inventory":
            config.grid_points = 0
            config.refine_budget = 0
        elif chosen == "prepare":
            config.refine_budget = 0
        elif chosen == "deep" and config.refine_budget <= 0:
            config.refine_budget = 96
        return config

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "grid_points": int(self.grid_points),
            "refine_budget": int(self.refine_budget),
            "max_controls": self.max_controls,
            "sample_indices": list(self.sample_indices) if self.sample_indices is not None else None,
            "batch_size": int(self.batch_size),
            "chunk_size": int(self.chunk_size),
            "formats": list(self.formats) if self.formats is not None else None,
            "budget": self.budget.to_dict(),
        }


class DiscoveryEngine:
    """Runs the pipeline against a bridge (live) or recorded data (replay)."""

    def __init__(self, *, bridge: Any = None) -> None:
        self.bridge = bridge

    # -- plumbing ----------------------------------------------------------

    def _sampler(self, ref: Any) -> tuple[Any, Any, Any]:
        """``(single, per_control_batch, all_controls_batch)`` formatter callables.

        All three go through one bridge verb, so the host is touched from exactly one
        place. The wide batch is what makes a sweep cheap: 775 controls x 17 points is
        13,175 values, and asking for them one value (or one control) per host hop
        turns a 13-second sweep into a much longer one. A bridge that offers only the
        narrower forms still works - the price is reported in the call census.
        """
        if self.bridge is None:
            return None, None, None
        batch = getattr(self.bridge, "format_params_normalized", None)
        single = getattr(self.bridge, "format_param_normalized", None)
        if callable(batch):
            def _texts(pairs: Sequence[tuple[int, float]]) -> list[str | None]:
                payload = batch(ref, [(int(index), float(value)) for index, value in pairs])
                if isinstance(payload, Mapping):
                    return list(payload.get("texts") or [])
                return list(payload or [])

            def _per_control(index: int, positions: Sequence[float]) -> list[str | None]:
                return _texts([(index, position) for position in positions])

            def _single_value(index: int, position: float) -> str | None:
                texts = _texts([(index, position)])
                return texts[0] if texts else None

            return _single_value, _per_control, _texts
        if callable(single):
            return single, None, None
        return None, None, None

    # -- stages ------------------------------------------------------------

    def run(
        self,
        ref: Any,
        facts: Any,
        *,
        config: EngineConfig | None = None,
        inventory: Inventory | None = None,
        recorded: Sequence[ControlObservations] | None = None,
        name: str | None = None,
    ) -> DiscoveryResult:
        config = (config or EngineConfig()).resolved()
        result = DiscoveryResult(
            engine_version=ENGINE_VERSION,
            mode=config.mode,
            identity=facts_identity(facts),
        )
        cost = RunCost(budgets={"engine": config.to_dict()})
        cost.started_utc = _utc()
        result.cost = cost

        # 1. host inventory
        started = time.perf_counter()
        if inventory is None:
            if self.bridge is None:
                raise ValueError("no bridge and no inventory: nothing to inventory")
            inventory = collect_inventory(
                self.bridge,
                ref,
                facts,
                indices=config.sample_indices,
                channels=config.channels,
                batch_size=config.batch_size,
                chunk_size=config.chunk_size,
            )
        result.inventory = inventory
        result.identity = dict(inventory.identity)
        cost.calls_by_stage["inventory"] = _calls_of(inventory)
        cost.seconds_by_stage["inventory"] = time.perf_counter() - started
        _checkpoint(cost, "inventory", inventory.params)

        # 2. format adapters
        started = time.perf_counter()
        api = getattr(self.bridge, "_api", None)
        if self.bridge is not None and callable(api):
            try:
                result.formats = probe_adapters(api(), ref, facts)
            except Exception as exc:  # noqa: BLE001 - an adapter probe must not end a run
                result.warnings.append(
                    {"stage": "formats", "message": f"{type(exc).__name__}: {exc}"}
                )
        else:
            result.formats = []
        for row in result.formats:
            if row.get("status") in ("unavailable", "error", "partial"):
                result.warnings.append(
                    {
                        "stage": "formats",
                        "adapter": row.get("adapter"),
                        "status": row.get("status"),
                        "reason": row.get("reason"),
                    }
                )
        cost.seconds_by_stage["formats"] = time.perf_counter() - started

        # 3. observations
        single, per_control, batch_all = self._sampler(ref)
        store = ObservationStore(sampler=single, budget=config.budget)
        started = time.perf_counter()
        selected = _select_controls(inventory, config)
        if recorded:
            # Replay: seed the store with recorded observations and take no host
            # sample at all, so the analyzers below cannot tell a live run from a
            # replayed one except by the origin the artifact records.
            for entry in recorded:
                for sample in entry.samples:
                    store.record(
                        entry.index,
                        sample.normalized,
                        sample.text,
                        origin="recorded",
                        name=entry.name,
                        ident=entry.ident,
                    )
                seeded = store.entry(entry.index)
                if seeded is not None:
                    seeded.status = entry.status
                    seeded.reason = entry.reason
                    seeded.origin = "recorded"
            result.warnings.append(
                {
                    "stage": "observations",
                    "message": "the run used recorded observations; no host call was made",
                    "origin": "recorded",
                }
            )
        elif config.grid_points >= 2 and (single or per_control or batch_all):
            _sweep(store, ref, selected, config, per_control=per_control, batch_all=batch_all)
        elif config.grid_points >= 2:
            result.warnings.append(
                {
                    "stage": "observations",
                    "message": "the bridge offers no read-only formatter read; no display sample was taken",
                }
            )
            for index in selected:
                entry = store.entry(index)
                if entry is None:
                    store.record(index, 0.0, "", origin="host")
                    entry = store.entry(index)
                if entry is not None:
                    entry.samples = []
                    entry.status = "no_samples"
                    entry.reason = "formatter channel unavailable"
        cost.calls_by_stage["observations"] = store.total_calls
        cost.seconds_by_stage["observations"] = time.perf_counter() - started
        cost.limits_hit.extend(store.limits_hit)
        result.observations = store.observations()
        _checkpoint(cost, "observations", store.observations())

        # 4-6. analyzers
        started = time.perf_counter()
        result.controls = _analyze(result, store, config, ref=ref)
        cost.seconds_by_stage["analyze"] = time.perf_counter() - started
        cost.calls_by_stage["analyze"] = sum(
            control.discrete.calls if control.discrete is not None else 0 for control in result.controls
        ) + int(store.calls_by_stage.get("discrete_refine", 0))
        _checkpoint(cost, "analyze", result.controls)

        # 7. module inference, then the controls are placed in the inferred tree
        started = time.perf_counter()
        result.grouping = infer_modules(_grouping_rows(result, inventory))
        for control in result.controls:
            control.module = _module_of(result, control.index)
        cost.seconds_by_stage["modules"] = time.perf_counter() - started
        _checkpoint(cost, "modules", result.grouping.nodes)

        result.counts = counts_of(result)
        cost.finished_utc = _utc()
        if cost.limits_hit:
            result.warnings.append(
                {
                    "stage": "budget",
                    "message": "the run hit a declared limit; the affected results are marked incomplete",
                    "limits": sorted(set(cost.limits_hit)),
                }
            )
        return result


def _utc() -> str:
    from ..model import utc_now_iso

    return utc_now_iso()


def _calls_of(inventory: Inventory) -> int:
    total = 0
    for attempt in inventory.attempts.values():
        total += int(attempt.calls)
    return total


def _checkpoint(cost: RunCost, stage: str, payload: Any) -> None:
    size = len(payload) if isinstance(payload, (list, tuple)) else None
    cost.checkpoints.append(
        {
            "stage": stage,
            "rows": size,
            "calls": cost.total_calls,
            "seconds": round(cost.total_seconds, 6),
        }
    )


def _select_controls(inventory: Inventory, config: EngineConfig) -> list[int]:
    if config.sample_indices is not None:
        wanted = [int(index) for index in config.sample_indices]
    else:
        wanted = [row.index for row in inventory.params if row.name or row.ident]
    if config.max_controls is not None:
        wanted = wanted[: int(config.max_controls)]
    return wanted


def _sweep(
    store: ObservationStore,
    ref: Any,
    selected: Sequence[int],
    config: EngineConfig,
    *,
    per_control: Any,
    batch_all: Any,
) -> None:
    positions = grid_positions(int(config.grid_points))
    pairs: list[tuple[int, float]] = []
    metadata: dict[int, tuple[str | None, str | None]] = {}
    for index in selected:
        entry = store.entry(index)
        name = entry.name if entry else None
        ident = entry.ident if entry else None
        metadata[index] = (name, ident)
        for position in positions:
            if store.known(index, position) is None:
                pairs.append((int(index), float(position)))
    if not pairs:
        return
    chunk = max(1, int(config.batch_size))
    if batch_all is not None:
        # One host hop per chunk of (control, value) pairs: the sweep is the most
        # expensive part of a prepare run, and the census below prices it as a
        # number of values, not as a number of hops.
        for start in range(0, len(pairs), chunk):
            window = pairs[start : start + chunk]
            texts = batch_all(window)
            for (index, position), text in zip(window, texts):
                name, ident = metadata.get(int(index), (None, None))
                store.record(int(index), float(position), text, origin="host", name=name, ident=ident)
                store.calls_by_stage["grid"] = int(store.calls_by_stage.get("grid", 0)) + 1
                store.calls_by_control[int(index)] = store.control_calls(int(index)) + 1
        return
    if per_control is not None:
        grouped: dict[int, list[float]] = {}
        for index, position in pairs:
            grouped.setdefault(int(index), []).append(float(position))
        for index, values in grouped.items():
            name, ident = metadata.get(int(index), (None, None))
            texts = per_control(index, values)
            for position, text in zip(values, texts):
                store.record(int(index), float(position), text, origin="host", name=name, ident=ident)
                store.calls_by_stage["grid"] = int(store.calls_by_stage.get("grid", 0)) + 1
                store.calls_by_control[int(index)] = store.control_calls(int(index)) + 1
        return
    for index, position in pairs:
        name, ident = metadata.get(int(index), (None, None))
        store.observe(int(index), float(position), stage="grid", name=name, ident=ident)


def _analyze(
    result: DiscoveryResult,
    store: ObservationStore,
    config: EngineConfig,
    *,
    ref: Any,
) -> list[ControlResult]:
    rows: list[ControlResult] = []
    inventory = result.inventory
    for param in inventory.params:
        entry = store.entry(param.index)
        control = ControlResult(
            index=param.index,
            name=param.name,
            ident=param.ident,
            label=param.name or (f"parameter {param.index}" if param.ident is None else param.ident),
            current_normalized=param.current_normalized,
            steps=param.steps.to_dict(),
            section=param.section,
            named_config=dict(param.named_config),
        )
        positions = entry.positions if entry is not None else []
        texts = entry.texts if entry is not None else []
        ordered = sorted(zip(positions, texts), key=lambda item: item[0])
        positions = [item[0] for item in ordered]
        texts = [item[1] for item in ordered]
        if len(texts) >= 3:
            resolution = printed_resolution(
                [point.y for point in numeric_samples(positions, texts)],
                [text for text in texts if _numeric(text)],
            ).to_dict()
            mapping = fit_mapping(positions, texts, resolution=resolution)
        else:
            mapping = MappingAnalysis(reason="insufficient_points")
            resolution = {}
        discrete = analyze_discrete(
            index=param.index,
            positions=positions,
            texts=texts,
            mapping=mapping,
            resolution=resolution,
            is_toggle=param.steps.is_toggle,
            host_step=(param.steps.step if param.steps.reported else None),
            store=store if config.refine_budget > 0 else None,
            name=param.name,
            ident=param.ident,
            refine_budget=int(config.refine_budget),
        )
        classification = classify_control(
            positions=positions,
            texts=texts,
            discrete=discrete,
            mapping=mapping,
            steps=param.steps,
            resolution=resolution,
        )
        control.mapping = mapping
        control.discrete = discrete
        control.classification = classification
        control.unit = _unit_of(texts)
        control.range = analyze_range(
            declared_native=param.native_range,
            display=display_bounds(positions, texts),
            unit=control.unit,
        )
        control.evidence = _evidence_rows(control, resolution)
        control.unknown = _unknown_rows(control, positions, texts)
        rows.append(control)
    return rows


def _numeric(text: str) -> bool:
    from .text import parse_display

    return parse_display(text).numeric


def _unit_of(texts: Sequence[str]) -> str | None:
    return unit_token_of(texts)


def _evidence_rows(control: ControlResult, resolution: dict[str, Any]) -> dict[str, Any]:
    rows: dict[str, Any] = {
        "classification": {
            "source_kind": "measured" if control.classification.confidence in ("strong", "medium") else "heuristic",
            "method": "discovery.classify",
            "strength": control.classification.confidence,
            "inputs": {"reasons": control.classification.reasons[:3]},
            "verified": control.classification.confidence == "strong",
        }
    }
    if control.mapping is not None and control.mapping.chosen is not None:
        chosen = control.mapping.chosen
        rows["mapping"] = {
            "source_kind": "derived",
            "method": "discovery.mapping.fit",
            "strength": "medium" if chosen.n_test else "weak",
            "inputs": {
                "kind": chosen.kind,
                "n_train": chosen.n_train,
                "n_test": chosen.n_test,
                "test_error": chosen.test_error,
                "tolerance": resolution.get("tolerance"),
            },
            "verified": bool(chosen.n_test and chosen.test_error is not None),
            "notes": ["the fit describes the printed display space, not the plugin's stored value space"],
        }
    elif control.mapping is not None:
        rows["mapping"] = {
            "source_kind": "unknown",
            "method": "discovery.mapping.fit",
            "strength": "none",
            "reason": control.mapping.reason or "model_unstable",
            "inputs": {"candidates": [fit.kind for fit in control.mapping.fits]},
            "verified": False,
        }
    if control.range is not None:
        claim = next(
            (
                item
                for item in control.range.claims
                if control.range.chosen is not None
                and list(item.get("value") or []) == list(control.range.chosen)
            ),
            None,
        )
        rows["range"] = {
            "source_kind": (claim or {}).get("source_kind", "unknown"),
            "method": "discovery.mapping.range",
            "strength": (claim or {}).get("strength", "none"),
            "inputs": {"space": control.range.space, "claims": len(control.range.claims)},
            "verified": control.range.space in ("native", "accepted"),
            "reason": None if control.range.chosen is not None else (control.range.reason or "api_does_not_expose"),
        }
        if control.range.conflict is not None:
            rows["range"]["conflicts"] = [control.range.conflict.to_dict()]
    return rows


def _unknown_rows(control: ControlResult, positions: Sequence[float], texts: Sequence[str]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    if not texts:
        rows.append({"field": "display", "reason": "no_observations"})
    if control.mapping is not None and control.mapping.chosen is None:
        rows.append({"field": "mapping", "reason": control.mapping.reason or "model_unstable"})
    if control.range is not None and control.range.chosen is None:
        rows.append({"field": "range", "reason": control.range.reason or "api_does_not_expose"})
    if control.classification.kind == "unknown":
        rows.append({"field": "class", "reason": "ambiguous_model"})
    if control.discrete is not None and control.discrete.reason == "requires_setter_experiment":
        rows.append({"field": "storage", "reason": "requires_setter_experiment"})
    if control.discrete is not None and control.discrete.budget_exhausted:
        rows.append({"field": "state_boundaries", "reason": "budget_exhausted"})
    return rows


def _module_of(result: DiscoveryResult, index: int) -> str | None:
    for node in result.grouping.nodes:
        if node.parent is None and index in node.controls:
            return node.id
    return None


def _grouping_rows(result: DiscoveryResult, inventory: Inventory) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    by_index = inventory.by_index()
    controls = {control.index: control for control in result.controls}
    for index, param in by_index.items():
        control = controls.get(index)
        rows.append(
            {
                "index": index,
                "name": param.name,
                "ident": param.ident,
                "section": param.section,
                "unit": control.unit if control else None,
                "class": control.classification.kind if control else None,
                "fallback_name": None,
            }
        )
    return rows


def counts_of(result: DiscoveryResult) -> dict[str, int]:
    """The count column of an artifact: a lost or half-applied run must fail a
    second, independent check besides its content hash."""
    classes: dict[str, int] = {}
    no_range = 0
    no_mapping = 0
    unknown_fields = 0
    measured_transitions = 0
    sampled_transitions = 0
    unresolved_transitions = 0
    for control in result.controls:
        classes[control.classification.kind] = classes.get(control.classification.kind, 0) + 1
        if control.range is None or control.range.chosen is None:
            no_range += 1
        if control.mapping is None or control.mapping.chosen is None:
            no_mapping += 1
        unknown_fields += len(control.unknown)
        if control.discrete is not None:
            # A grid-derived edge is NOT an unresolved transition: it is a weaker
            # but positive statement (the switch point lies inside this bracket),
            # so it is counted separately from one the run could not locate.
            for item in control.discrete.transitions:
                if item.state in ("exact", "bracketed"):
                    measured_transitions += 1
                elif item.state == "sampled_upper_bound":
                    sampled_transitions += 1
                else:
                    unresolved_transitions += 1
    counts = {
        "parameters": len(result.inventory.params),
        "controls_analyzed": len(result.controls),
        "controls_observed": sum(1 for item in result.observations if item.samples),
        "modules": sum(1 for node in result.grouping.nodes if node.parent is None),
        "submodules": sum(1 for node in result.grouping.nodes if node.parent),
        "no_range": no_range,
        "no_mapping": no_mapping,
        "unknown_fields": unknown_fields,
        "transitions_measured": measured_transitions,
        "transitions_sampled_upper_bound": sampled_transitions,
        "transitions_unresolved": unresolved_transitions,
        "calls": result.cost.total_calls,
    }
    for kind in sorted(classes):
        counts[f"class_{kind}"] = classes[kind]
    return counts


def run_discovery(
    bridge: Any,
    ref: Any,
    facts: Any,
    *,
    config: EngineConfig | None = None,
    inventory: Inventory | None = None,
) -> DiscoveryResult:
    """Convenience wrapper: one engine, one run."""
    return DiscoveryEngine(bridge=bridge).run(ref, facts, config=config, inventory=inventory)
