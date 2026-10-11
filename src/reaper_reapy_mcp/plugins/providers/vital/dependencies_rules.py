"""Vital's architectural dependency rules (10 §3; port of the measured rule table).

Ported from the POC's `adapter/dependencies.py`: the rule table with its
recorded evidence, the DISABLE candidates for a structural transition, and the
percentage-control derivation. Two things about the port are important enough to
state in the module rather than in a commit message:

1. **Rules are data.** The table below is the same measured table (id, kind,
   meaning, evidence). Core decides what a `hard` finding does; this module only
   produces findings.
2. **What the V3 evaluator can decide from the projected world.** The POC
   predicates read the *live document's flags* (a filter's on/off state, an
   oscillator's level). Core's projected world carries the plan's changes, the
   route specs **and the decoded live document** (`plan.project_world`), so
   `route_into_off_filter` is now **evaluated**: the destination->module join
   comes from the registry (316/316 destinations, measured 2026-10-11), the
   module's on/off flag comes from the live document overlaid with the plan's own
   writes, and only a pair the rule actually forbids refuses. What remains
   undecidable is refused **per subject** with the missing input named
   (`_evaluate_route_pairs`). The rules that stay unevaluated
   (`fm_needs_modulator`, `wave_frame_needs_multiframe_wavetable`) are named by
   `rules_not_evaluated()` with the signal each would need; the evaluated/not
   split has one source of truth in :data:`EVALUATION`.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from ...model import Finding
from ...provider import DependencyRuleSet
from . import codec as codec_module

__all__ = [
    "DISABLE_CANDIDATES",
    "DIRECT_OUT",
    "EVALUATION",
    "FM_TYPES",
    "MEASURED_FILTER_SCOPE",
    "PERCENTAGE_CONTROLS",
    "RULES",
    "addresses_route_table",
    "disable_set",
    "disable_targets",
    "distortion_type_subjects",
    "evaluation_for",
    "percentage_controls_for",
    "rule_evaluated",
    "rules_not_evaluated",
    "dependencies",
]

#: The state-path root the codec writes through (05 §3).
_STATE_ROOT = "settings"

#: Ported from `adapter/dependencies.DISABLE_CANDIDATES`: what a *structural*
#: change must switch off unless the new architecture keeps it on. Derived from
#: the take-3 incidents where exactly these leaked into a "clean" reference.
DISABLE_CANDIDATES: dict[str, dict[str, Any]] = {
    "Oscillator 1 Distortion Type": {"off": "None", "why": "FM/RM leaks across a rebuild"},
    "Oscillator 2 Distortion Type": {"off": "None", "why": "FM/RM leaks across a rebuild"},
    "Oscillator 3 Distortion Type": {"off": "None", "why": "FM/RM leaks across a rebuild"},
    "Oscillator 1 Distortion Amount": {"off": 0.0, "why": "a leftover drive amount becomes audible the moment the type is set"},
    "Oscillator 2 Distortion Amount": {"off": 0.0, "why": "leftover drive amount"},
    "Oscillator 3 Distortion Amount": {"off": 0.0, "why": "leftover drive amount"},
    "Oscillator 1 Frequency Morph Amount": {"off": 0.0, "why": "the spectral morph leaked into a 'clean' reference and measured +0.46 dB/oct against -3.42"},
    "Oscillator 2 Frequency Morph Amount": {"off": 0.0, "why": "spectral morph leak"},
    "Oscillator 3 Frequency Morph Amount": {"off": 0.0, "why": "spectral morph leak"},
    "Compressor Switch": {"off": 0.0, "why": "modulation-effect leak"},
    "Phaser Switch": {"off": 0.0, "why": "modulation-effect leak"},
    "Delay Switch": {"off": 0.0, "why": "a delay tail spans the measurement window and makes a short render unrepresentative"},
    "Reverb Switch": {"off": 0.0, "why": "a reverb tail spans the measurement window"},
}

#: Ported: the destination value that means "not through the filters".
DIRECT_OUT = "DIRECT OUT"

#: Ported: distortion-type display names that need another oscillator running.
FM_TYPES = ("FM", "RM", "FM <- Osc", "RM <- Sample")

#: Ported fallback list; the live set is derived from the measurement instead
#: (`percentage_controls_for`), because a hand-typed list is how the trap shipped.
PERCENTAGE_CONTROLS = {
    "Filter 1 Mix", "Filter 2 Mix", "Filter fx Mix", "Compressor Mix",
    "Flanger Mix", "Phaser Mix", "Chorus Mix", "Reverb Dry Wet", "Delay Dry Wet",
    "Distortion Mix", "EQ Mix", "Stereo Routing",
}

#: The measured rule table, with the POC's recorded evidence. `signals` names the
#: projected-world inputs the evaluator uses; `needs_live_flags` marks the rules
#: whose POC predicate read untouched live state.
RULES: list[dict[str, Any]] = [
    {
        "id": "direct_out_bypasses_filters",
        "kind": "hard",
        "informational": True,
        "message": "DIRECT OUT bypasses both filters and the whole effects chain",
        "evidence": "filter closed -26.52 dB / 5372 Hz via DIRECT OUT vs -63.18 dB / 135 Hz via FILTER 1",
        "signals": ["state_paths_changed"],
        "needs_live_flags": False,
    },
    {
        "id": "fm_needs_modulator",
        "kind": "hard",
        "informational": False,
        "message": "FM/RM distortion needs another oscillator running; without one the setting is bit-identical to None",
        "evidence": "bit-identical to None with Osc 2 off; with Osc 2 on at level 0: h2/h1 +31.59 dB",
        "signals": ["state_paths_changed"],
        "needs_live_flags": True,
    },
    {
        "id": "route_into_off_filter",
        "kind": "hard",
        "informational": False,
        "message": "a source routed to a filter that is OFF is silent",
        "evidence": "measured",
        "signals": ["routes_create_specs", "state_paths_changed"],
        "needs_live_flags": True,
    },
    {
        "id": "wave_frame_needs_multiframe_wavetable",
        "kind": "hard",
        "informational": True,
        "message": "a route to osc_N_wave_frame is inert when the wavetable's frame is fixed",
        "evidence": "writes return the wavetable's own frame; renders bit-identical",
        "signals": ["routes_create_specs"],
        "needs_live_flags": True,
    },
    {
        "id": "percentage_units_are_not_one_space",
        "kind": "hard",
        "informational": False,
        "message": "percentage-displaying controls are not one value space",
        "evidence": "Filter 1 Mix = 1.0 means 1 %; Distortion Mix is 0..1",
        "signals": ["controls_changed_display"],
        "needs_live_flags": False,
    },
]


#: The rule's **measured scope**: the POC's predicate is
#: ``destination.startswith(f"filter_{f}_") for f in (1, 2)``
#: (``adapter/dependencies.py``, `route_to_an_off_filter`), i.e. the rule spoken
#: about Filter 1 and Filter 2 only. Expressed here as the *destination-name
#: prefixes the measurement names*, resolved to module labels through the
#: registry's own destination->module join at evaluation time - never typed as
#: module labels, so a destination whose module is Filter 1 stays covered even
#: if the registry ever relabels it. `Filter FX` is deliberately **not** in the
#: measured scope, and that is disclosed by :func:`evaluation_for` rather than
#: silently included or silently dropped.
MEASURED_FILTER_SCOPE = ("filter_1", "filter_2")

#: Which of the measured rules this evaluator **decides**, from which projected
#: inputs. One source of truth for the evaluator, `rules_not_evaluated()` and the
#: profile's `dependencies[].evaluated` rows (the three drifting apart is how a
#: rule becomes "declared but nobody notices").
EVALUATION: dict[str, dict[str, Any]] = {
    "direct_out_bypasses_filters": {
        "evaluated": True,
        "inputs": ["state_paths_changed", "profile controls"],
    },
    "fm_needs_modulator": {
        "evaluated": False,
        "inputs": ["state_paths_changed", "profile controls"],
        "inputs_missing": "the other oscillator's level from the live document (untouched state)",
    },
    "route_into_off_filter": {
        "evaluated": True,
        "inputs": [
            "routes_create_specs / routes_update_specs / state_paths_changed (the created pairs)",
            "profile.modulation.destination_modules (registry join: 316/316 destinations)",
            "profile.modulation.module_switches (registry rows whose control is a Switch)",
            "live_document.settings flags, projected through state_paths_changed",
        ],
        "scope": list(MEASURED_FILTER_SCOPE),
    },
    "wave_frame_needs_multiframe_wavetable": {
        "evaluated": False,
        "inputs": ["routes_create_specs"],
        "inputs_missing": "the wavetable's frame count from the live document (settings.wavetables)",
    },
    "percentage_units_are_not_one_space": {
        "evaluated": True,
        "inputs": ["controls_changed", "profile controls"],
    },
}


def rule_evaluated(rule_id: str) -> bool:
    """Whether the evaluator decides ``rule_id`` (default: no)."""
    return bool((EVALUATION.get(str(rule_id)) or {}).get("evaluated"))


def evaluation_for(rule_id: str) -> dict[str, Any] | None:
    """How a rule is decided, in the same shape for every rule.

    ``evaluated`` is the decision itself; because only provider data may say so,
    the disclosure is derived from :data:`EVALUATION` and the rule row rather
    than re-typed per rule. For the rules that are *not* decided, the missing
    input is named here and in ``rules_not_evaluated()``.
    """
    rule = next((row for row in RULES if str(row["id"]) == str(rule_id)), None)
    declared = dict(EVALUATION.get(str(rule_id)) or {})
    if rule is None or not declared:
        return None
    out: dict[str, Any] = {
        "rule": str(rule_id),
        "kind": rule.get("kind"),
        "declared_blocking": bool(rule.get("kind") == "hard" and not rule.get("informational")),
        "evaluated": bool(declared.get("evaluated")),
        "inputs": list(declared.get("inputs") or []),
        "inputs_missing": declared.get("inputs_missing"),
        "scope": list(declared.get("scope") or []),
        "evidence": rule.get("evidence"),
    }
    if str(rule_id) == "route_into_off_filter":
        out.update(
            {
                "decision": "decide_then_refuse_only_the_decided_violation",
                "subjects": [
                    "routes_create[*]",
                    "routes_update[*] naming a non-empty destination",
                    "routes_update[*] naming only a source (the slot's live destination decides)",
                    f"a state write of a non-empty `<root>.{codec_module.ROUTE_TABLE_KEY}.<slot>.destination|.source`",
                ],
                "fail_closed_when": [
                    "the destination is not in the registry's destination->module join",
                    "the slot of a source-only update has no live destination in the document",
                    "the projected on/off flag of the destination's module is not in the document",
                    "a write addresses the route table as a table/whole slot (no pair shape to decide)",
                    "a count arrives without its specs",
                    "a control's declared binding addresses the route table (the parameter batch is invisible to the state scan)",
                ],
                "not_covered": [
                    "Filter FX: the measured predicate names Filter 1 and Filter 2 only",
                    "routes_remove[*] and amount-only updates: no pair is created",
                    "a state write that clears a pair field (an empty value removes a pair)",
                    "state writes outside the route table (e.g. the per-slot amount field)",
                ],
            }
        )
    return out


def percentage_controls_for(scales: dict[str, Any]) -> set[str]:
    """The controls whose measured display ends in ``%`` (derived, not typed)."""
    from .depth import percent_controls

    return percent_controls(scales)


def disable_set(planned_names: Iterable[str]) -> dict[str, Any]:
    """Ported transition computation: every DISABLE candidate the plan leaves on.

    ``planned_names`` are the names/ids the change keeps on; everything else in
    the table is switched off, with the recorded reason attached. The names are
    compared **normalised** (case, spaces and underscores): the previous version
    compared them to ``str(control_dict)``, so the comparison never matched and
    every candidate was disabled even when the plan kept it on.
    """
    kept = {_normalise(name) for name in planned_names}
    out: dict[str, Any] = {}
    for name, spec in DISABLE_CANDIDATES.items():
        if _normalise(name) in kept:
            continue
        out[name] = {"to": spec["off"], "why": spec["why"]}
    return out


#: Why a not-yet-evaluated rule has no enforceable subject guard. Kept next to
#: the rule data rather than inside the loop so it cannot drift per call.
UNGUARDED_REASONS: dict[str, str] = {
    "wave_frame_needs_multiframe_wavetable": (
        "the measured table declares this rule informational, so a violation can only ever be an "
        "advisory: refusing on it would harden the provider's own declaration"
    ),
}


def rules_not_evaluated() -> list[dict[str, Any]]:
    """The rules this evaluator cannot decide, with an honest status each.

    Three things are reported per rule and they are deliberately separate:

    - ``evaluated: false`` - the rule is **not** decided. It is never presented
      as checked. The split comes from :data:`EVALUATION`, the same table the
      evaluator itself reads, so a rule cannot be "evaluated" in one place and
      "not evaluated" in another.
    - ``blocking`` - whether the measured table itself declares the rule a
      blocker (`kind: hard` and not `informational`). The layer does not soften
      or harden the provider's own declaration.
    - ``guard`` - the subject test that *is* enforceable, or ``None`` with the
      measured reason why it is not (a guard is only built from data the
      registry actually carries; a hand-typed list would be the guess this
      module refuses to make).
    """
    rows: list[dict[str, Any]] = []
    for rule in RULES:
        rid = str(rule["id"])
        detail = evaluation_for(rid)
        if detail is None or detail["evaluated"]:
            continue
        guard = _guard_for(rid)
        rows.append(
            {
                "rule": rid,
                "evaluated": False,
                "blocking": bool(rule.get("kind") == "hard" and not rule.get("informational")),
                "needs": str(detail.get("inputs_missing") or "an input the projected world does not carry"),
                "guard": guard,
                "guard_enforceable": bool(guard and guard.get("enforceable")),
                "why_not_enforced": (
                    None if guard and guard.get("enforceable") else UNGUARDED_REASONS.get(rid, guard_reason(rid))
                ),
                "why_recorded": (
                    "Core's plan.project_world now carries the decoded live document "
                    "(2026-10-11), so a rule whose deciding input is a live flag *can* be "
                    "evaluated; this rule's remaining input is the one named above"
                    if rule.get("id") == "fm_needs_modulator"
                    else "the missing input is named above; nothing about this rule is decided"
                ),
            }
        )
    return rows


def distortion_type_subjects() -> tuple[str, ...]:
    """The DISABLE candidates the measured table calls distortion *types*.

    Derived from the table's own keys (not re-typed): these are the settings
    `fm_needs_modulator` speaks about.
    """
    return tuple(name for name in DISABLE_CANDIDATES if str(name).endswith("Distortion Type"))


def guard_reason(rule_id: str) -> str:
    """Why no guard exists for a rule (kept next to the rule data)."""
    if rule_id == "fm_needs_modulator":
        return (
            "the subject is decidable (see the guard); only the rule's second input (the other "
            "oscillator's level) is missing"
        )
    return "no enforceable subject test exists for this rule"


def _guard_for(rule_id: str) -> dict[str, Any] | None:
    """The enforceable subject test for a rule, or ``None``.

    ``fm_needs_modulator``'s subject **is** decidable from measured data: the
    table names the oscillator distortion types, and the profile binds those
    names to real document paths. What is not decidable is the rule's own
    question (is another oscillator running), so the guard refuses the plan
    instead of claiming the rule was satisfied - and it refuses the *subject*,
    not everything, so unrelated operations stay available.

    ``route_into_off_filter`` is no longer a guard case (2026-10-11): its
    deciding inputs - the destination->module join and the filter's projected
    on/off flag - are now in the world, so the rule is **evaluated** by
    `_evaluate_route_pairs` and only a decided violation refuses. Its disclosure
    lives in :func:`evaluation_for`.
    """
    if rule_id == "fm_needs_modulator":
        return {
            "enforceable": True,
            "kind": "subject_path_changed",
            "subjects": list(distortion_type_subjects()),
            "why": (
                "the rule is a blocking rule whose subject is decidable and whose deciding input is not; "
                "a plan that changes the subject cannot be shown to satisfy it"
            ),
            "decision": "refuse_before_any_write",
        }
    return None


def _normalise(text: Any) -> str:
    return " ".join(str(text).strip().lower().replace("_", " ").split())


def _subject_paths(profile: dict[str, Any]) -> dict[str, str]:
    """``normalised subject name -> measured state path`` for the guarded rule.

    The mapping comes from the profile's **own declared names** (id, label,
    aliases, state_key), which the live binding already relies on. A subject the
    profile does not bind is skipped *with its reason kept by the caller* rather
    than spelled as a path the document does not have.
    """
    out: dict[str, str] = {}
    for name in distortion_type_subjects():
        wanted = _normalise(name)
        for control in profile.get("controls") or []:
            candidates = {_normalise(control.get("id")), _normalise(control.get("label"))}
            candidates.update(_normalise(alias) for alias in control.get("aliases") or [])
            candidates.add(_normalise((control.get("binding") or {}).get("state_key")))
            if wanted in candidates:
                key = (control.get("binding") or {}).get("state_key")
                if key:
                    out[wanted] = f"{_STATE_ROOT}.{key}"
                break
    return out


def disable_targets(profile: dict[str, Any]) -> list[dict[str, Any]]:
    """Every DISABLE candidate as an addressable change, or a named refusal.

    Measured defect this replaces (2026-10-10): the table's candidates used to be
    spelled as document paths *verbatim* (`settings.Oscillator 1 Distortion
    Type`), and the codec creates unknown keys - so a structural change wrote 13
    **phantom keys** and changed none of the real settings (reproduced on the
    fixture; the captured live document has 781 settings keys, none containing a
    space, and `osc_1_distortion_type`/`reverb_on` are the real ones). The plan
    would still have verified: it compared the keys it had just created.

    Each row is therefore either ``state_key`` + a **justified** value in the
    document's own space, or ``reason`` saying what is missing.
    """
    rows: list[dict[str, Any]] = []
    for name, spec in DISABLE_CANDIDATES.items():
        row: dict[str, Any] = {"candidate": str(name), "off": spec["off"], "why": spec.get("why")}
        key = None
        control: dict[str, Any] | None = None
        wanted = _normalise(name)
        for item in profile.get("controls") or []:
            candidates = {_normalise(item.get("id")), _normalise(item.get("label"))}
            candidates.update(_normalise(alias) for alias in item.get("aliases") or [])
            candidates.add(_normalise((item.get("binding") or {}).get("state_key")))
            if wanted in candidates:
                control = item
                key = (item.get("binding") or {}).get("state_key")
                break
        if not key:
            row["reason"] = "disable_target_unmapped"
            row["detail"] = "the profile binds no control to this candidate, so its document path is unknown"
            rows.append(row)
            continue
        row["state_key"] = str(key)
        row["path"] = f"{_STATE_ROOT}.{key}"
        law = str((control or {}).get("law", {}).get("kind") or "none")
        value = spec["off"]
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            row["value"] = float(value)
            row["value_space"] = "native"
            rows.append(row)
            continue
        if law in ("none", "unmeasured"):
            row["reason"] = "disable_value_not_measurable"
            row["detail"] = (
                f"the off value {value!r} is a display value and this control declares no conversion "
                f"(law {law!r}), so no value in the document's own space can be justified"
            )
            rows.append(row)
            continue
        if str((control or {}).get("type")) == "enum":
            # A display value on a measured enum converts to the **member index**,
            # and the index is what the document's own space holds: the captured
            # live document stores `osc_1_distortion_type = 0.0` and
            # `filter_1_style = 0.0`, i.e. member 0 of each measured member list
            # ("None" / "12dB"), for enum controls whose `range` is 0..n-1.
            # Emitting the *display* string instead would put a string into a
            # numeric leaf - the phantom-value shape this function exists to
            # prevent (2026-10-11; the law that makes this decidable is the
            # measured-sweep derivation in `profile_build.derive_measured_law`).
            from ... import values as values_module  # noqa: PLC0415 - provider -> Core is allowed

            try:
                native = float(values_module.to_native(control, value))
            except Exception as exc:  # noqa: BLE001 - classified as a named refusal
                row["reason"] = "disable_value_not_measurable"
                row["detail"] = (
                    f"the off value {value!r} could not be converted through the control's own law: "
                    f"{type(exc).__name__}: {exc}"
                )
                rows.append(row)
                continue
            row["value"] = native
            row["value_space"] = "native"
            row["value_from_display"] = value
            rows.append(row)
            continue
        row["reason"] = "disable_value_not_measurable"
        row["detail"] = (
            f"the off value {value!r} is a display value and the control's law {law!r} does not "
            "establish the document's own space for it (measured only for enum member indices)"
        )
        rows.append(row)
    return rows


def _dig_display(control: dict[str, Any], native: Any) -> Any:
    """Projected-world native value -> the display value the rule speaks about."""
    ctype = control.get("type")
    if ctype == "enum":
        members = [str(member.get("id")) for member in control.get("enum_values") or []]
        try:
            return members[int(native)]
        except (ValueError, TypeError, IndexError):
            return None
    return native


def _guard_fm_subject(
    changed: dict[str, Any], profile: dict[str, Any], rule: dict[str, Any]
) -> list[Finding]:
    """Refuse a plan that enters `fm_needs_modulator`'s subject (Decision 1).

    Both spellings are matched, because a plan can address the same setting two
    ways: the profile's measured ``state_key`` (a control or a caller-written
    state path) and the DISABLE table's own name (the structural companion set
    used to spell exactly that name as a path). Matching only one of them is how
    a guard becomes decorative.
    """
    subjects = distortion_type_subjects()
    mapping = _subject_paths(profile)
    by_key = {_normalise(name): path for name, path in mapping.items()}
    out: list[Finding] = []
    for path, value in sorted(changed.items()):
        text = str(path)
        key = _normalise(text.split(".", 1)[1] if "." in text else text)
        subject = None
        for name in subjects:
            if key == _normalise(name) or path == by_key.get(_normalise(name)):
                subject = name
                break
        if subject is None:
            continue
        out.append(
            Finding(
                kind="hard",
                rule=str(rule["id"]),
                subject={"state_path": text, "subject": subject},
                message=(
                    "this change enters a setting the rule "
                    f"{rule['id']!r} governs, and that rule is declared but NOT evaluated; "
                    "the layer refuses instead of claiming it was satisfied"
                ),
                detail={
                    "evaluated": False,
                    "reason": "rule_not_evaluated",
                    "missing_input": (
                        "the other oscillator's level from the live document (untouched state)"
                    ),
                    "observed": value,
                    "guard": "subject_path_changed",
                },
                informational=False,
            )
        )
    return out


def _destination_modules(profile: dict[str, Any]) -> dict[str, str]:
    """``destination -> module label`` from the profile's registry join."""
    declared = ((profile.get("modulation") or {}).get("destination_modules") or {})
    return {str(key): str(value) for key, value in declared.items() if value}


def _module_switches(profile: dict[str, Any]) -> dict[str, str]:
    """``module label -> on/off flag state key`` from the profile's join."""
    declared = ((profile.get("modulation") or {}).get("module_switches") or {})
    return {str(key): str(value) for key, value in declared.items() if value}


def _scope_modules(destination_modules: dict[str, str]) -> set[str]:
    """The module labels the rule's measured predicate covers.

    Derived from the measured destination *prefixes* through the registry's own
    join, so the scope cannot drift into a hand-typed module list.
    """
    return {
        module
        for destination, module in destination_modules.items()
        if any(destination.startswith(prefix + "_") for prefix in MEASURED_FILTER_SCOPE)
    }


def _flag_off(value: Any) -> bool | None:
    """``True`` = the switch is OFF, ``False`` = ON, ``None`` = not decidable."""
    if isinstance(value, bool):
        return not value
    if isinstance(value, (int, float)):
        return float(value) == 0.0
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("0", "off", "false"):
            return True
        if text in ("1", "on", "true"):
            return False
    return None


def _projected_flag(world: dict[str, Any], switch_key: str) -> tuple[Any, str]:
    """The projected value of a module switch and where it came from.

    The plan's own write to the switch wins (a plan that turns the filter ON in
    the same transaction must not be judged against the state it starts from);
    otherwise the live document the world carries; otherwise nothing.
    """
    path = f"{codec_module.MUTABLE_ROOT}.{switch_key}"
    changed = dict(world.get("state_paths_changed") or {})
    if path in changed:
        return changed[path], "planned"
    document = world.get("live_document")
    settings = document.get("settings") if isinstance(document, dict) else None
    if isinstance(settings, dict) and switch_key in settings:
        return settings.get(switch_key), "live_document"
    return None, "missing"


def _slot_destination(world: dict[str, Any], slot: Any) -> str | None:
    """The live destination of a route slot, from the document in the world."""
    try:
        number = int(slot)
    except (TypeError, ValueError):
        return None
    document = world.get("live_document")
    settings = document.get("settings") if isinstance(document, dict) else None
    mods = settings.get(codec_module.ROUTE_TABLE_KEY) if isinstance(settings, dict) else None
    if isinstance(mods, list) and 1 <= number <= len(mods) and isinstance(mods[number - 1], dict):
        return str(mods[number - 1].get("destination") or "")
    return None


def _evaluate_route_pairs(
    world: dict[str, Any], changed: dict[str, Any], rule: dict[str, Any], profile: dict[str, Any]
) -> list[Finding]:
    """Decide ``route_into_off_filter`` for every pair the plan creates.

    The rule (``adapter/dependencies.py``, measured): *a source routed to a
    filter that is OFF is silent* - it fires only when a route into
    ``filter_{1,2}_*`` exists **and** that filter is off in the projected state.
    The two deciding inputs now exist in the world: the destination->module join
    derived from the registry (316/316 destinations, measured 2026-10-11) and the
    module's on/off flag in the live document, overlaid with the plan's own
    writes. So this rule is **evaluated**, not guarded: a pair into an ON filter
    (or into a non-filter destination) passes, and only a pair the rule actually
    forbids refuses.

    What remains fail-closed is named per finding (``missing_input``): an
    undecidable destination, a source-only update whose slot has no live
    destination, a switch whose projected value is absent, a write that addresses
    the table as a table (no pair shape to decide), a count without its specs, or
    a control bound into the table (the parameter batch is invisible to the
    state scan). The subject scan itself is unchanged from the owner's
    safety-gate correction (2026-10-11) - every representation a pair can be
    created through is still read.

    Operations that create no pair - a removal, an amount/power-only update, a
    write that clears a pair field, every read - are deliberately not refused:
    the rule's subject is untouched.
    """
    out: list[Finding] = []
    destination_modules = _destination_modules(profile)
    module_switches = _module_switches(profile)
    scope = _scope_modules(destination_modules)

    def undecided(subject: dict[str, Any], observed: str, missing: str) -> None:
        out.append(
            Finding(
                kind="hard",
                rule=str(rule["id"]),
                subject=subject,
                message=(
                    f"this plan creates a modulation pair and the rule {rule['id']!r} cannot be "
                    "decided for it; the layer refuses instead of claiming it was satisfied"
                ),
                detail={
                    "evaluated": False,
                    "reason": "rule_not_evaluated",
                    "missing_input": missing,
                    "guard": "route_pair_created",
                    "observed": observed,
                },
                informational=False,
            )
        )

    def violation(subject: dict[str, Any], destination: str, module: str, switch: str, value: Any, source: str) -> None:
        out.append(
            Finding(
                kind="hard",
                rule=str(rule["id"]),
                subject=subject,
                message=f"{rule['message']}: {destination!r} targets {module}, which is OFF",
                detail={
                    "evaluated": True,
                    "reason": "route_into_off_filter",
                    "destination": destination,
                    "module": module,
                    "switch": switch,
                    "switch_value": value,
                    "value_source": source,
                    "evidence": rule.get("evidence"),
                },
                informational=False,
            )
        )

    def decide(subject: dict[str, Any], destination: str) -> None:
        module = destination_modules.get(destination)
        if module is None:
            undecided(
                subject,
                f"destination {destination!r} is not in the registry's destination->module join",
                "the destination's module (the registry's destination->module join does not cover it)",
            )
            return
        if module not in scope:
            return  # decided: the rule's measured subject (Filter 1 / Filter 2) is not entered
        switch = module_switches.get(module)
        if not switch:
            undecided(
                subject,
                f"module {module!r} has no measured on/off switch row",
                f"an on/off flag for module {module!r}",
            )
            return
        value, source = _projected_flag(world, switch)
        off = _flag_off(value)
        if off is None:
            undecided(
                subject,
                f"the projected value of {switch!r} is {value!r} (source: {source})",
                f"the value of {switch!r} in the live document or the plan",
            )
            return
        if off:
            violation(subject, destination, module, switch, value, source)

    for spec in world.get("routes_create_specs") or []:
        spec = dict(spec or {})
        subject = {"route_create": spec}
        destination = str(spec.get("destination") or "")
        if not destination:
            undecided(subject, f"create names no destination: {spec!r}", "the destination of the created pair")
            continue
        decide(subject, destination)
    # Fail closed on a **count without its subject**: a plan that creates pairs
    # whose specs the world does not carry is not decidable either, and a guard
    # that silently skipped it would be bypassable by any caller that reaches the
    # evaluator with a different world shape.
    for specs_key, count_key in (("routes_create_specs", "routes_create"), ("routes_update_specs", "routes_update")):
        shortfall = int(world.get(count_key) or 0) - len(list(world.get(specs_key) or []))
        if shortfall > 0:
            undecided(
                {
                    "world": count_key,
                    "counted": int(world.get(count_key) or 0),
                    "carried": len(list(world.get(specs_key) or [])),
                },
                f"the projected world counts {count_key} without carrying the specs ({shortfall} missing)",
                "the created pairs' (source, destination) specs",
            )
    for spec in world.get("routes_update_specs") or []:
        spec = dict(spec or {})
        subject = {"route_update": spec}
        destination = spec.get("destination")
        if destination not in (None, ""):
            decide(subject, str(destination))
            continue
        if spec.get("source") in (None, ""):
            continue  # no pair field is touched
        live_destination = _slot_destination(world, spec.get("slot"))
        if live_destination:
            decide(subject, live_destination)
        else:
            undecided(
                subject,
                f"update names a source for slot {spec.get('slot')!r}, whose live destination is unknown",
                f"the live destination of slot {spec.get('slot')!r} (settings.{codec_module.ROUTE_TABLE_KEY})",
            )

    for path, value in sorted(changed.items()):
        if not addresses_route_table(path):
            continue
        pair = codec_module.parse_route_pair_path(path)
        if pair is not None and value in (None, ""):
            # clearing a pair field *removes* a pairing - the rule's subject is a
            # created pair, so this is deliberately not refused
            continue
        subject = {"state_path": str(path), "slot": pair.index if pair else None, "field": pair.field if pair else None}
        if pair is None:
            undecided(
                subject,
                f"state write of {str(path)} = {value!r} addresses the route table without a pair field",
                "a single pair field write whose destination can be decided",
            )
            continue
        if pair.field == "destination":
            decide(subject, str(value))
            continue
        live_destination = _slot_destination(world, pair.index)
        if live_destination:
            decide(subject, live_destination)
        else:
            undecided(
                subject,
                f"state write of {str(path)} = {value!r}: slot {pair.index} has no live destination",
                f"the live destination of slot {pair.index} (settings.{codec_module.ROUTE_TABLE_KEY})",
            )

    # A control write does not go through the state channel, so a control whose
    # declaration binds it into the route table would create a pair through the
    # parameter batch. No Vital control does today, but the guard must be decided
    # from the profile rather than from the assumption that none ever will:
    # a binding that addresses the table is the same subject - and it carries no
    # pair shape, so it stays fail-closed.
    for control_id in sorted(world.get("controls_changed") or {}):
        control = _control_by_id(profile, control_id)
        if control is None:
            continue
        binding = control.get("binding") or {}
        # a binding's state_key is relative to the mutable root (`_subject_paths`
        # joins it the same way), while a full dotted path is accepted as written
        for candidate in (binding.get("state_key"), control.get("id")):
            if not candidate:
                continue
            spellings = (str(candidate), f"{codec_module.MUTABLE_ROOT}.{candidate}")
            if any(addresses_route_table(spelling) for spelling in spellings):
                undecided(
                    {"control": str(control_id), "binding": str(candidate)},
                    f"control {control_id!r} is bound into the route table ({candidate})",
                    "a pair shape in the parameter channel (it is invisible to the state scan)",
                )
                break
    return out


def _control_by_id(profile: dict[str, Any], control_id: Any) -> dict[str, Any] | None:
    for control in profile.get("controls") or []:
        if str(control.get("id")) == str(control_id):
            return control
    return None


def addresses_route_table(path: Any) -> bool:
    """True when a dotted path is the route table or anything inside it.

    The guard uses this rather than only the two leaf fields: writing
    ``settings.modulations`` (the whole table) or ``settings.modulations.3`` (a
    whole slot) can create pairings too, and a guard that recognised only the
    leaves would be bypassable by writing their parent.

    The root comes from the codec (`MUTABLE_ROOT`), like the table key does: a
    hand-typed copy would keep matching a root the codec no longer writes.
    """
    parts = str(path).split(".")
    return (
        len(parts) >= 2
        and parts[0] == codec_module.MUTABLE_ROOT
        and parts[1] == codec_module.ROUTE_TABLE_KEY
    )


def _state_path_display(profile: dict[str, Any], path: str, native: Any) -> Any:
    for control in profile.get("controls") or []:
        binding = control.get("binding") or {}
        if binding.get("state_key") == path:
            return _dig_display(control, native)
    return native


def _evaluate(world: dict[str, Any], rules: Sequence[dict[str, Any]], profile: dict[str, Any]) -> list[Finding]:
    """Only the rules whose inputs exist in the projected world are decided.

    A rule whose subject **is** decidable but whose deciding input is not is
    guarded: a plan that enters the subject is refused, naming the rule and the
    missing input, instead of being reported as checked (`rules_not_evaluated`).
    """
    findings: list[Finding] = []
    changed = dict(world.get("state_paths_changed") or {})
    controls_changed = dict(world.get("controls_changed") or {})

    for rule in rules:
        rid = rule.get("id")
        if rid == "direct_out_bypasses_filters":
            for path, native in changed.items():
                display = _state_path_display(profile, str(path), native)
                if isinstance(display, str) and display.strip().upper() == DIRECT_OUT:
                    findings.append(
                        Finding(
                            kind="hard",
                            rule=rid,
                            subject={"state_path": str(path)},
                            message=rule["message"],
                            detail={"evidence": rule.get("evidence"), "display": display},
                            informational=True,
                        )
                    )
        elif rid == "fm_needs_modulator":
            findings.extend(_guard_fm_subject(changed, profile, rule))
        elif rid == "route_into_off_filter":
            findings.extend(_evaluate_route_pairs(world, changed, rule, profile))
        elif rid == "percentage_units_are_not_one_space":
            for control_id, native in controls_changed.items():
                for control in profile.get("controls") or []:
                    if control.get("id") != control_id:
                        continue
                    if (
                        control.get("unit") == "%"
                        and isinstance(native, (int, float))
                        and not isinstance(native, bool)
                        and float(native) == 1.0
                    ):
                        findings.append(
                            Finding(
                                kind="hard",
                                rule=rid,
                                subject={"control": control_id},
                                message=rule["message"],
                                detail={"evidence": rule.get("evidence"), "value": native},
                            )
                        )
    return findings


def dependencies(profile: dict[str, Any]) -> DependencyRuleSet:
    """The Vital rule set: the measured table plus a projected-world evaluator."""

    def evaluate(world: dict[str, Any], rule_rows: Sequence[dict[str, Any]]) -> list[Finding]:
        return _evaluate(world, rule_rows, profile)

    return DependencyRuleSet(rules=[dict(rule) for rule in RULES], evaluate=evaluate)
