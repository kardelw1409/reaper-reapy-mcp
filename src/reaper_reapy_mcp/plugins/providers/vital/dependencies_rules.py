"""Vital's architectural dependency rules (10 §3; port of the measured rule table).

Ported from the POC's `adapter/dependencies.py`: the rule table with its
recorded evidence, the DISABLE candidates for a structural transition, and the
percentage-control derivation. Two things about the port are important enough to
state in the module rather than in a commit message:

1. **Rules are data.** The table below is the same measured table (id, kind,
   meaning, evidence). Core decides what a `hard` finding does; this module only
   produces findings.
2. **What the V3 evaluator can decide from the projected world, and what it
   cannot.** The POC predicates read the *live document's flags* (an oscillator's
   destination switch, a filter's on/off state). Core's projected world carries
   the plan's changes and the route counts, not the untouched live flags
   (`plan.project_world`). So the ported evaluator decides exactly the rules whose
   inputs are in the world and leaves the rest **unfired rather than guessed**,
   with `rules_not_evaluated()` naming them and the signal each would need. Making
   the flag-reading rules live requires the dependency hook to receive the live
   document - a Core change that reopens [05]/[08], which P7's scope explicitly
   excludes. This is recorded as an open item, not smoothed over.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from ...model import Finding
from ...provider import DependencyRuleSet
from . import codec as codec_module

__all__ = [
    "DISABLE_CANDIDATES",
    "DIRECT_OUT",
    "FM_TYPES",
    "PERCENTAGE_CONTROLS",
    "RULES",
    "addresses_route_table",
    "disable_set",
    "disable_targets",
    "distortion_type_subjects",
    "percentage_controls_for",
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


def rules_not_evaluated() -> list[dict[str, Any]]:
    """The rules this evaluator cannot decide, with an honest status each.

    Three things are reported per rule and they are deliberately separate:

    - ``evaluated: false`` - the rule is **not** decided. It is never presented
      as checked.
    - ``blocking`` - whether the measured table itself declares the rule a
      blocker (`kind: hard` and not `informational`). The layer does not soften
      or harden the provider's own declaration.
    - ``guard`` - the subject test that *is* enforceable, or ``None`` with the
      measured reason why it is not (a guard is only built from data the
      registry actually carries; a hand-typed list would be the guess this
      module refuses to make).
    """
    needs: dict[str, str] = {
        "fm_needs_modulator": "the other oscillator's level from the live document (untouched state)",
        "route_into_off_filter": "the created routes' (source, destination) pairs AND the filter on/off flags",
        "wave_frame_needs_multiframe_wavetable": "the created routes' destination pairs and the wavetable frame count",
    }
    unguarded: dict[str, str] = {
        "wave_frame_needs_multiframe_wavetable": (
            "the measured table declares this rule informational, so a violation can only ever be an "
            "advisory: refusing on it would harden the provider's own declaration"
        ),
    }
    rows: list[dict[str, Any]] = []
    for rule in RULES:
        if not rule.get("needs_live_flags"):
            continue
        rid = str(rule["id"])
        guard = _guard_for(rid)
        rows.append(
            {
                "rule": rid,
                "evaluated": False,
                "blocking": bool(rule.get("kind") == "hard" and not rule.get("informational")),
                "needs": needs.get(rid, "an input the projected world does not carry"),
                "guard": guard,
                "guard_enforceable": bool(guard and guard.get("enforceable")),
                "why_not_enforced": None if guard and guard.get("enforceable") else unguarded.get(rid, guard_reason(rid)),
                "why_recorded": (
                    "the route specs now ride in the projected world (`routes_create_specs` / "
                    "`routes_update_specs`, added 2026-10-11), so this rule's SUBJECT is decidable; "
                    "what is still missing is the deciding input named above (the destination's kind "
                    "and the filter's on/off flag), which live in the registry and the live document"
                    if rule.get("id") == "route_into_off_filter"
                    else (
                        "Core's plan.project_world carries the plan's changed paths/controls and route "
                        "counts, not the route specs or the live flags; passing them is a Core change "
                        "(reopens [05]/[08])"
                    )
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

    ``route_into_off_filter`` has the same shape since the owner's safety-gate
    correction (2026-10-11): its subject - a **created** ``(source, destination)``
    pair - is decidable from the plan, while the deciding inputs (whether the
    destination is a filter input and whether that filter is on) are not. A plan
    that creates a pair cannot be shown to satisfy the rule, so it is refused;
    operations that create no pair are untouched.
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
    if rule_id == "route_into_off_filter":
        return {
            "enforceable": True,
            "kind": "route_pair_created",
            "subjects": [
                "routes_create[*]",
                "routes_update[*] naming source/destination",
                f"any state write under {_STATE_ROOT}.{codec_module.ROUTE_TABLE_KEY} that does not clear a pair field",
                "a control whose declared binding addresses the route table (it would create a pair "
                "through the parameter batch, which the state scan cannot see)",
            ],
            "why": (
                "the rule is a blocking rule whose subject is decidable (a created pair) and whose "
                "deciding inputs are not (the registry carries no destination families, and the "
                "filter on/off flag lives in the live document); a plan that creates a pair cannot "
                "be shown to satisfy it"
            ),
            "not_subject": [
                "routes_remove[*]",
                "routes_update[*] that carries no source/destination",
                "a state write that clears a pair field (an empty value removes a pair)",
                "state writes outside the route table (e.g. the per-slot amount field)",
            ],
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


def _guard_route_pair(
    world: dict[str, Any], changed: dict[str, Any], rule: dict[str, Any], profile: dict[str, Any]
) -> list[Finding]:
    """Refuse a plan that **creates** a ``(source, destination)`` pair.

    Owner decision (2026-10-11, safety-gate correction): route changes were
    allowed while this rule was declared but not evaluated, which is the exact
    contradiction a fail-closed layer must not have. The subject is decidable
    from the plan - and it must be read from **every** way a pair can be created,
    not only the route operations, because the state channel can write the same
    two fields directly:

    - ``routes_create`` - every entry creates a pair;
    - ``routes_update`` that names a non-empty ``source``/``destination``;
    - a state write of a non-empty value to ``<root>.modulations.<slot>.source``
      or ``.destination`` (the codec's own representation, parsed by the codec),
      or to the table / a whole slot, which can create pairs too;
    - a ``routes_create``/``routes_update`` count whose specs the world does not
      carry (not decidable);
    - a **control** whose declared binding addresses the route table (it would
      create a pair through the parameter batch, which the state scan cannot see).

    Operations that create no pair - a removal, an amount/power-only update, a
    write that clears a pair field, every read - are deliberately **not** refused:
    the rule's subject is untouched, so their safety provably does not depend on
    it, and the layer does not block for the appearance of completeness.
    """
    out: list[Finding] = []
    #: Bound once and never rebound: an earlier version reused this name for an
    #: integer inside the loop below, which silently replaced the explanation with
    #: a number in every later finding (found by an independent audit, 2026-10-11).
    missing_input = (
        "whether the destination is a filter input (the registry carries no destination "
        "families) and whether that filter is on (the flag lives in the live document)"
    )

    def emit(subject: dict[str, Any], why: str) -> None:
        out.append(
            Finding(
                kind="hard",
                rule=str(rule["id"]),
                subject=subject,
                message=(
                    f"this plan creates a modulation pair, and the rule {rule['id']!r} is declared "
                    "but NOT evaluated; the layer refuses instead of claiming it was satisfied"
                ),
                detail={
                    "evaluated": False,
                    "reason": "rule_not_evaluated",
                    "missing_input": missing_input,
                    "guard": "route_pair_created",
                    "observed": why,
                },
                informational=False,
            )
        )

    for spec in world.get("routes_create_specs") or []:
        spec = dict(spec or {})
        emit(
            {"route_create": spec},
            f"create {spec.get('source')!r} -> {spec.get('destination')!r}",
        )
    # Fail closed on a **count without its subject**: a plan that creates pairs
    # whose specs the world does not carry is not decidable either, and a guard
    # that silently skipped it would be bypassable by any caller that reaches the
    # evaluator with a different world shape.
    for specs_key, count_key in (("routes_create_specs", "routes_create"), ("routes_update_specs", "routes_update")):
        shortfall = int(world.get(count_key) or 0) - len(list(world.get(specs_key) or []))
        if shortfall > 0:
            emit(
                {
                    "world": count_key,
                    "counted": int(world.get(count_key) or 0),
                    "carried": len(list(world.get(specs_key) or [])),
                },
                f"the projected world counts {count_key} without carrying the specs ({shortfall} missing)",
            )
    for spec in world.get("routes_update_specs") or []:
        spec = dict(spec or {})
        touched = {
            field: spec[field]
            for field in ("source", "destination")
            if spec.get(field) not in (None, "")
        }
        if touched:
            emit({"route_update": spec, "fields": sorted(touched)}, f"update names {sorted(touched)}")

    for path, value in sorted(changed.items()):
        if not addresses_route_table(path):
            continue
        pair = codec_module.parse_route_pair_path(path)
        if pair is not None and value in (None, ""):
            # clearing a pair field *removes* a pairing - the rule's subject is a
            # created pair, so this is deliberately not refused
            continue
        emit(
            {"state_path": str(path), "slot": pair.index if pair else None, "field": pair.field if pair else None},
            f"state write of {str(path)} = {value!r}",
        )

    # A control write does not go through the state channel, so a control whose
    # declaration binds it into the route table would create a pair through the
    # parameter batch. No Vital control does today, but the guard must be decided
    # from the profile rather than from the assumption that none ever will:
    # a binding that addresses the table is the same subject.
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
                emit(
                    {"control": str(control_id), "binding": str(candidate)},
                    f"control {control_id!r} is bound into the route table ({candidate})",
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
            findings.extend(_guard_route_pair(world, changed, rule, profile))
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
