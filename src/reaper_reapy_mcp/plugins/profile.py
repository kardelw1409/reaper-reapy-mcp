"""Capability Profile schema: version, validation (contract 07 §1, §4).

P0 status: the schema version constant and the [07 §4] validation table as a
validator. Profile *building* and the cache key are P4; provenance digests are
used here only to decide cacheability (07 §4 rows 10/11).

The validator returns problems instead of raising: the caller decides between
"refuse + rebuild" and "refuse the cache write" exactly as the table demands.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "PROFILE_SCHEMA_VERSION",
    "CACHE_FORMAT_KEY",
    "LAW_KINDS",
    "LAW_DIRECTIONS",
    "CONFIRMATION_STATES",
    "CALIBRATION_STATES",
    "AUTHORITY_SPACES",
    "DEPENDENCY_KINDS",
    "REQUIRED_IDENTITY_FIELDS",
    "SUPPORTED_CAPTURE_CHANNELS",
    "ProfileProblem",
    "ProfileValidation",
    "validate_profile",
]

#: Bumped when the profile SHAPE changes; the cache format key moves with it so
#: old files are rebuilt rather than mis-read (07 §3.1).
#: v2 (2026-10-11): the calibration block, per-control `host_steps`, `unit_source`,
#: `law_candidate` and `evidence.confirmation`, and `law.params.direction` for a
#: measured descending law. A v1 entry is refused by its own schema check (the
#: preload skips it, the runtime path quarantines it and rebuilds).
PROFILE_SCHEMA_VERSION = 2
CACHE_FORMAT_KEY = "capability-map-v2"

#: Legal conversion directions a numeric law may declare (P22).
LAW_DIRECTIONS = ("ascending", "descending")

#: Legal confirmation states an `evidence.confirmation` record may carry.
#: `consistent_within_step` is strictly weaker than `confirmed`: the law agrees
#: with the host's text to within the host's own display granularity, not to its
#: printed digits, and it promotes nothing.
CONFIRMATION_STATES = ("confirmed", "consistent_within_step", "declined", "insufficient_points")

#: Legal calibration-block states: applied, refused (a configured artifact failed), or
#: absent (no preparation run yet - a normal state, not a refusal).
CALIBRATION_STATES = ("applied", "refused", "absent")

#: The measured enum-boundary states (P23): the block's own state, plus the singular
#: state a *control* carries when no band map could be attached (a ramping display
#: has no bands; an undetermined one was not measurable). A measured control carries
#: the plural `states` list of the transition levels instead.
ENUM_BOUNDARY_BLOCK_STATES = ("applied", "refused", "absent")
ENUM_BOUNDARY_EVIDENCE_STATES = ("no_band_structure", "undetermined", "unverified_interior")

LAW_KINDS = ("affine", "enum_index", "exponential", "identity", "none", "power", "unmeasured")
AUTHORITY_SPACES = ("parameter", "state")
DEPENDENCY_KINDS = ("hard", "soft", "incompatible", "warning", "info")
SUPPORTED_CAPTURE_CHANNELS = ("document", "routes", "parameters")

#: Identity fields that must match live host reads (07 §3.5); the fingerprint
#: carries the remaining discriminators.
REQUIRED_IDENTITY_FIELDS = ("fx_name", "class_id", "module")

#: Problem kinds mirror the [07 §4] "Behaviour on failure" column.
REFUSE_REBUILD = "refuse_rebuild"
VALIDATION_ERROR = "validation_error"
REFUSE_CACHE_WRITE = "refuse_cache_write"


@dataclass
class ProfileProblem:
    rule: str
    message: str
    kind: str = VALIDATION_ERROR


@dataclass
class ProfileValidation:
    ok: bool
    problems: list[ProfileProblem] = field(default_factory=list)

    @property
    def rebuild_required(self) -> bool:
        """True when the profile must be rebuilt rather than used (07 §4)."""
        return any(p.kind == REFUSE_REBUILD for p in self.problems)

    @property
    def cacheable(self) -> bool:
        """False when the profile may be used but must not be cached (07 §4)."""
        return not any(p.kind == REFUSE_CACHE_WRITE for p in self.problems)

    def codes(self) -> list[str]:
        return [p.rule for p in self.problems]


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_number(value: Any) -> bool:
    """A real, **finite** number.

    `json` parses the bare tokens `NaN`, `Infinity` and `-Infinity`, so a
    validating profile could otherwise carry a value no consumer can compare -
    and `json.dumps` would write those non-standard tokens back out.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value)


def validate_profile(profile: dict[str, Any]) -> ProfileValidation:
    """Encode the [07 §4] validation table. One problem row per failed rule."""
    problems: list[ProfileProblem] = []
    if not isinstance(profile, dict):
        return ProfileValidation(
            ok=False,
            problems=[ProfileProblem("schema_version_known", "profile is not an object", REFUSE_REBUILD)],
        )

    # Row 1 - schema version known.
    version = profile.get("profile_schema_version")
    if version != PROFILE_SCHEMA_VERSION:
        problems.append(
            ProfileProblem(
                "schema_version_known",
                f"profile_schema_version {version!r} is not known (expected {PROFILE_SCHEMA_VERSION})",
                REFUSE_REBUILD,
            )
        )

    # Row 2 - identity fields present (provider-required subset).
    identity = profile.get("identity")
    if not isinstance(identity, dict):
        problems.append(ProfileProblem("identity_fields_present", "identity block missing", REFUSE_REBUILD))
        identity = {}
    for field_name in REQUIRED_IDENTITY_FIELDS:
        if not _nonempty(identity.get(field_name)):
            problems.append(
                ProfileProblem(
                    "identity_fields_present",
                    f"required identity field {field_name!r} missing or empty",
                    REFUSE_REBUILD,
                )
            )
    fingerprint = identity.get("fingerprint")
    if not isinstance(fingerprint, dict) or not _is_number(fingerprint.get("param_count")):
        problems.append(
            ProfileProblem(
                "identity_fields_present",
                "identity.fingerprint.param_count missing or not numeric",
                REFUSE_REBUILD,
            )
        )

    modules = profile.get("modules")
    controls = profile.get("controls")
    module_ids: set[str] = set()
    if isinstance(modules, list):
        for module in modules:
            if isinstance(module, dict) and _nonempty(module.get("id")):
                module_ids.add(module["id"])
    if isinstance(controls, list):
        seen: set[str] = set()
        for control in controls:
            if not isinstance(control, dict):
                continue
            control_id = control.get("id")
            # Row 13 - duplicate control ids.
            if _nonempty(control_id):
                if control_id in seen:
                    problems.append(
                        ProfileProblem("no_duplicate_controls", f"duplicate control id {control_id!r}")
                    )
                seen.add(control_id)
            # Row 3 - every control's module exists when both are present.
            if module_ids and _nonempty(control.get("module")) and control["module"] not in module_ids:
                problems.append(
                    ProfileProblem(
                        "control_module_exists",
                        f"control {control_id!r} names unknown module {control['module']!r}",
                    )
                )
            # Row 4 - authorities legal; parameter-write requires a binding.
            authority = control.get("authority")
            if not isinstance(authority, dict):
                problems.append(
                    ProfileProblem("authority_legal", f"control {control_id!r} has no authority block")
                )
            else:
                for direction in ("read", "write"):
                    if authority.get(direction) not in AUTHORITY_SPACES:
                        problems.append(
                            ProfileProblem(
                                "authority_legal",
                                f"control {control_id!r} authority.{direction} "
                                f"{authority.get(direction)!r} not in {AUTHORITY_SPACES}",
                            )
                        )
                binding = control.get("binding")
                if authority.get("write") == "parameter":
                    if not isinstance(binding, dict) or not binding:
                        problems.append(
                            ProfileProblem(
                                "authority_legal",
                                f"control {control_id!r} writes the parameter channel "
                                "but declares no parameter binding",
                            )
                        )
                if authority.get("read") == "parameter":
                    if not isinstance(binding, dict) or not binding:
                        problems.append(
                            ProfileProblem(
                                "authority_legal",
                                f"control {control_id!r} reads the parameter channel "
                                "but declares no parameter binding",
                            )
                        )
            # Row 5 - enum members and discreteness.
            ctype = control.get("type")
            enum_values = control.get("enum_values")
            if ctype == "enum":
                if not isinstance(enum_values, list) or len(enum_values) < 2:
                    problems.append(
                        ProfileProblem(
                            "enum_members",
                            f"enum control {control_id!r} declares fewer than 2 enum_values",
                        )
                    )
                if control.get("discrete") is False:
                    problems.append(
                        ProfileProblem(
                            "enum_members",
                            f"enum control {control_id!r} declares discrete=false "
                            "(enums are discrete, 05 §4.4)",
                        )
                    )
            # Row 6 - law kind known; none/unmeasured carry no params.
            law = control.get("law")
            if isinstance(law, dict):
                kind = law.get("kind")
                if kind not in LAW_KINDS:
                    problems.append(
                        ProfileProblem("law_kind_known", f"control {control_id!r} law kind {kind!r} unknown")
                    )
                if kind in ("none", "unmeasured") and law.get("params"):
                    problems.append(
                        ProfileProblem(
                            "law_kind_known",
                            f"control {control_id!r} law kind {kind!r} must not carry params",
                        )
                    )
                # Row 6b - a declared conversion direction is one of the two the
                # spine model expresses (P22); a typo must not silently read as
                # ascending.
                params = law.get("params")
                if isinstance(params, dict) and "direction" in params:
                    if str(params["direction"]) not in LAW_DIRECTIONS:
                        problems.append(
                            ProfileProblem(
                                "law_direction_known",
                                f"control {control_id!r} law direction {params['direction']!r} "
                                f"not in {LAW_DIRECTIONS}",
                            )
                        )
                # Row 6e - a declared **storage grid** (P25): the plugin's engine
                # rounds every set, so the provider declares the step. `values` snaps
                # both directions onto it; a step that is not a positive finite number
                # would silently change every conversion of the control, so it is a
                # `REFUSE_REBUILD` - the loader consults `rebuild_required`/`cacheable`
                # and never `ok`, so only this kind actually stops a stored entry.
                if isinstance(params, dict) and "quantization" in params:
                    grid = params["quantization"]
                    step = grid.get("step") if isinstance(grid, dict) else None
                    origin = grid.get("origin", 0.0) if isinstance(grid, dict) else None
                    if (
                        not isinstance(grid, dict)
                        or not _is_number(step)
                        or float(step) <= 0.0
                        or not _is_number(origin)
                    ):
                        problems.append(
                            ProfileProblem(
                                "law_quantization_valid",
                                f"control {control_id!r} declares a storage grid that is not a "
                                f"usable step ({grid!r})",
                                kind=REFUSE_REBUILD,
                            )
                        )
                    else:
                        # and the grid has to be one the *range* can be walked on: the
                        # snap rounds in the control's own space with the declared
                        # origin, while a plugin that rounds its value does not clamp
                        # (measured: `values.convertToEngineValue` in the provider this
                        # layer was built against). A range whose ends are not multiples
                        # of the step away from the origin is a grid the conversions
                        # cannot honour, so it is refused rather than approximated.
                        bounds = control.get("range") or {}
                        low, high = bounds.get("min"), bounds.get("max")
                        if _is_number(low) and _is_number(high):
                            step_value, origin_value = float(step), float(origin)
                            aligned = all(
                                abs(
                                    (float(bound) - origin_value) / step_value
                                    - round((float(bound) - origin_value) / step_value)
                                )
                                <= 1e-9
                                for bound in (low, high)
                            )
                            if not aligned:
                                problems.append(
                                    ProfileProblem(
                                        "law_quantization_valid",
                                        f"control {control_id!r} declares a storage grid "
                                        f"(origin {origin!r}, step {step!r}) that its range "
                                        f"{bounds!r} does not sit on",
                                        kind=REFUSE_REBUILD,
                                    )
                                )
            # Row 6c - a near-fit candidate is a RECORD, never a conversion: it must
            # name a known kind and carry the residual that refused it, and the
            # control it sits on must not already have a working law.
            candidate = control.get("law_candidate")
            if candidate is not None:
                if not isinstance(candidate, dict) or candidate.get("kind") not in LAW_KINDS:
                    problems.append(
                        ProfileProblem(
                            "law_candidate_shape",
                            f"control {control_id!r} law_candidate is not a known-kind object",
                        )
                    )
                else:
                    worst = candidate.get("worst_state")
                    ratio = worst.get("ratio") if isinstance(worst, dict) else None
                    if not _is_number(ratio) or float(ratio) <= 1.0:
                        problems.append(
                            ProfileProblem(
                                "law_candidate_shape",
                                f"control {control_id!r} law_candidate must record a refused fit "
                                f"(worst ratio > 1), got {ratio!r}",
                            )
                        )
                    if isinstance(law, dict) and law.get("kind") not in ("none", "unmeasured"):
                        problems.append(
                            ProfileProblem(
                                "law_candidate_shape",
                                f"control {control_id!r} carries a law_candidate although its law "
                                f"{law.get('kind')!r} converts",
                            )
                        )
            # Row 6f - a declaration a measurement replaced (P24). A declared
            # `enum_index` whose sweep changed at every step it walked is a ramp:
            # the profile may model it numerically, but only as a *number* with a
            # numeric law and a real range, and the evidence must say which
            # declaration it replaced. A control that still claims members may not
            # claim to have been converted, and one that was converted may not keep
            # a member list (that would leave two models of one parameter live).
            override = (control.get("evidence") or {}).get("enum_declaration_overridden")
            if override is not None:
                valid = isinstance(override, dict) and (
                    override.get("declared_kind") == "enum_index"
                    and _is_number(override.get("declared_members"))
                    and float(override["declared_members"]) >= 2
                    and _is_number(override.get("measured_states"))
                    and float(override["measured_states"]) >= 3
                    and isinstance(override.get("why"), str)
                    and bool(override.get("why"))
                )
                if not valid:
                    problems.append(
                        ProfileProblem(
                            "enum_declaration_overridden_valid",
                            f"control {control_id!r} enum_declaration_overridden must name the "
                            "declared kind, at least two declared members, at least three "
                            "measured states and the reason",
                        )
                    )
                if control.get("type") != "number":
                    problems.append(
                        ProfileProblem(
                            "enum_declaration_overridden_valid",
                            f"control {control_id!r} overrides an enum declaration but is not a "
                            f"number (type {control.get('type')!r})",
                        )
                    )
                if isinstance(law, dict) and law.get("kind") not in (
                    "affine", "power", "exponential",
                ):
                    problems.append(
                        ProfileProblem(
                            "enum_declaration_overridden_valid",
                            f"control {control_id!r} overrides an enum declaration with law "
                            f"{law.get('kind')!r}: only a numeric law may replace a member list",
                        )
                    )
                if control.get("enum_values"):
                    problems.append(
                        ProfileProblem(
                            "enum_declaration_overridden_valid",
                            f"control {control_id!r} overrides an enum declaration and still "
                            "carries a member list",
                        )
                    )
                # A conversion is only usable as a *number* if its range is known:
                # `values.to_normalized` falls back to the identity space for a
                # range-less control, so a display value would be planned as the
                # normalized parameter (measured on a cache-shaped profile with
                # `range: null`: `display_to_write_space(control, 4095.5)` plans
                # `normalized: 4095.5`). The sweep that produced the law is also what
                # produced the range, so requiring one costs nothing and refuses the
                # only profile that could be written nonsense through.
                range_spec = control.get("range")
                if not (
                    isinstance(range_spec, dict)
                    and _is_number(range_spec.get("min"))
                    and _is_number(range_spec.get("max"))
                    and float(range_spec["max"]) > float(range_spec["min"])
                ):
                    # `REFUSE_REBUILD`, not a plain validation error: the loader
                    # consults `rebuild_required`/`cacheable` and never `ok`, so only
                    # these kinds actually stop a stored entry from being served (a
                    # self-consistent corrupt entry is a cache *hit* otherwise, which
                    # is the shape this row was written against). The builder always
                    # carries the sweep's range, so a rebuild lands on a valid profile.
                    problems.append(
                        ProfileProblem(
                            "enum_declaration_overridden_valid",
                            f"control {control_id!r} overrides an enum declaration without a "
                            f"usable range ({range_spec!r}): a converted control is served as a "
                            "number and must carry the sweep's own range",
                            kind=REFUSE_REBUILD,
                        )
                    )
            # and the shape itself, whether or not the row was converted: a `number`
            # control carries no member list and no enum geometry (positions and
            # switch points are enum-only, and a number control carrying them would
            # leave the read path inverting a mapping the write path does not use)
            if control.get("type") == "number":
                params_for_shape = (law or {}).get("params") if isinstance(law, dict) else None
                geometry = (
                    sorted(
                        name
                        for name in ("positions", "switch_points", "positions_source")
                        if isinstance(params_for_shape, dict) and name in params_for_shape
                    )
                    if params_for_shape
                    else []
                )
                if control.get("enum_values") or geometry:
                    problems.append(
                        ProfileProblem(
                            "number_control_shape_valid",
                            f"control {control_id!r} is a number control but carries "
                            f"{('a member list' if control.get('enum_values') else geometry)!r}",
                        )
                    )
            # Row 6d - host step metadata (P22): numbers and a bool, nothing else.
            steps = control.get("host_steps")
            if steps is not None:
                valid = (
                    isinstance(steps, dict)
                    and all(_is_number(steps.get(name)) for name in ("step", "small", "large"))
                    and isinstance(steps.get("is_toggle"), bool)
                    and isinstance(steps.get("source"), dict)
                )
                if not valid:
                    problems.append(
                        ProfileProblem(
                            "host_steps_valid",
                            f"control {control_id!r} host_steps must carry numeric step/small/large, "
                            "a boolean is_toggle and a source block",
                        )
                    )
            # Row 6e - a filled unit must name where it came from (P22): an
            # unattributed unit is what the no-invention rule exists to prevent.
            unit_source = control.get("unit_source")
            if unit_source is not None:
                if not isinstance(unit_source, dict) or not _nonempty(unit_source.get("kind")):
                    problems.append(
                        ProfileProblem(
                            "unit_provenance_present",
                            f"control {control_id!r} carries a unit_source without a kind",
                        )
                    )
                elif not _nonempty(control.get("unit")):
                    problems.append(
                        ProfileProblem(
                            "unit_provenance_present",
                            f"control {control_id!r} carries a unit_source but declares no unit",
                        )
                    )
            # Row 6f - a confirmation record states one of the three legal states.
            evidence = control.get("evidence")
            confirmation = evidence.get("confirmation") if isinstance(evidence, dict) else None
            if confirmation is not None:
                if not isinstance(confirmation, dict) or confirmation.get("state") not in CONFIRMATION_STATES:
                    problems.append(
                        ProfileProblem(
                            "confirmation_state_known",
                            f"control {control_id!r} evidence.confirmation state "
                            f"{(confirmation or {}).get('state') if isinstance(confirmation, dict) else None!r} "
                            f"not in {CONFIRMATION_STATES}",
                        )
                    )
                elif confirmation.get("state") == "confirmed" and not isinstance(
                    evidence.get("strength_before"), str
                ):
                    problems.append(
                        ProfileProblem(
                            "confirmation_state_known",
                            f"control {control_id!r} promotes evidence on a confirmation but does "
                            "not record strength_before",
                        )
                    )

    # Row 7 - modulation declarations.
    modulation = profile.get("modulation")
    if isinstance(modulation, dict):
        if modulation.get("supported") is False:
            if "slots" in modulation or "fields" in modulation:
                problems.append(
                    ProfileProblem(
                        "modulation_supported",
                        "modulation.supported=false must not declare slots/fields",
                    )
                )
        elif modulation.get("supported") is True:
            slots = modulation.get("slots")
            if not isinstance(slots, int) or slots < 1:
                problems.append(
                    ProfileProblem("modulation_supported", "modulation.slots must be >= 1 when supported")
                )

    # Row 8 - dependency rule ids unique, kinds known.
    dependencies = profile.get("dependencies")
    if isinstance(dependencies, list):
        seen_rules: set[str] = set()
        for rule in dependencies:
            if not isinstance(rule, dict):
                continue
            rule_id = rule.get("id")
            if _nonempty(rule_id):
                if rule_id in seen_rules:
                    problems.append(
                        ProfileProblem("dependency_rules_unique", f"duplicate dependency rule id {rule_id!r}")
                    )
                seen_rules.add(rule_id)
            if rule.get("kind") not in DEPENDENCY_KINDS:
                problems.append(
                    ProfileProblem(
                        "dependency_rules_unique",
                        f"dependency rule {rule_id!r} kind {rule.get('kind')!r} not in {DEPENDENCY_KINDS}",
                    )
                )

    # Row 9 - write_order is a permutation of declared stages with verify last.
    write_order = profile.get("write_order")
    if not isinstance(write_order, list) or not write_order:
        problems.append(ProfileProblem("write_order_valid", "write_order missing or empty"))
    else:
        declared = [str(s) for s in write_order]
        if len(set(declared)) != len(declared):
            problems.append(ProfileProblem("write_order_valid", f"write_order has duplicates: {declared}"))
        if declared and declared[-1] != "verify":
            problems.append(
                ProfileProblem("write_order_valid", f"write_order must end with 'verify': {declared}")
            )
        if "verify" in declared[:-1]:
            problems.append(
                ProfileProblem("write_order_valid", f"'verify' appears out of position in {declared}")
            )

    # Row 10 - provenance.sources[] non-empty with hashes.
    provenance = profile.get("provenance")
    if not isinstance(provenance, dict):
        problems.append(
            ProfileProblem("provenance_sources", "provenance block missing", REFUSE_CACHE_WRITE)
        )
    else:
        sources = provenance.get("sources")
        if not isinstance(sources, list) or not sources:
            problems.append(
                ProfileProblem(
                    "provenance_sources",
                    "provenance.sources[] is empty; a profile without provenance is not cacheable",
                    REFUSE_CACHE_WRITE,
                )
            )
        else:
            for source in sources:
                if not isinstance(source, dict) or not _nonempty(source.get("sha256")):
                    problems.append(
                        ProfileProblem(
                            "provenance_sources",
                            "a provenance source has no sha256",
                            REFUSE_CACHE_WRITE,
                        )
                    )
        # Row 11 - provider revision + artifact digest present.
        if not _nonempty(provenance.get("provider_revision")):
            problems.append(
                ProfileProblem(
                    "provider_revision_present",
                    "provenance.provider_revision is empty (AUD-008)",
                    REFUSE_CACHE_WRITE,
                )
            )
        if not _nonempty(provenance.get("provider_artifact_digest")):
            problems.append(
                ProfileProblem(
                    "provider_revision_present",
                    "provenance.provider_artifact_digest is missing (AUD-008)",
                    REFUSE_CACHE_WRITE,
                )
            )

    # Row 11b - the calibration block (P22): an `applied` block must name the
    # artifact whose hash is key material, and a refused one must say why. An
    # `applied: true` with no artifact reference would make the merges it
    # justifies unattributable.
    calibration = profile.get("calibration")
    if calibration is not None:
        if not isinstance(calibration, dict) or not isinstance(calibration.get("applied"), bool):
            problems.append(
                ProfileProblem(
                    "calibration_block_valid",
                    "the calibration block must be an object with a boolean `applied`",
                )
            )
        else:
            artifact = calibration.get("artifact")
            has_artifact = isinstance(artifact, dict) and _nonempty(artifact.get("sha256"))
            if calibration["applied"] and not has_artifact:
                problems.append(
                    ProfileProblem(
                        "calibration_block_valid",
                        "calibration.applied is true but the artifact reference (file+sha256) "
                        "is missing",
                    )
                )
            if calibration["applied"] and not _is_number(calibration.get("method_version")):
                problems.append(
                    ProfileProblem(
                        "calibration_block_valid",
                        "calibration.applied is true but method_version is not numeric",
                    )
                )
            if artifact is not None and not isinstance(artifact, dict):
                problems.append(
                    ProfileProblem("calibration_block_valid", "calibration.artifact is not an object")
                )
            counts = calibration.get("counts")
            if counts is not None and not isinstance(counts, dict):
                problems.append(
                    ProfileProblem("calibration_block_valid", "calibration.counts is not an object")
                )
            refusals = calibration.get("refusals")
            if refusals is not None and not isinstance(refusals, list):
                problems.append(
                    ProfileProblem("calibration_block_valid", "calibration.refusals is not a list")
                )
            state = calibration.get("state")
            if state is not None and state not in CALIBRATION_STATES:
                problems.append(
                    ProfileProblem(
                        "calibration_block_valid",
                        f"calibration.state {state!r} not in {CALIBRATION_STATES}",
                    )
                )

    # Row 11c - the measured enum band boundaries (P23). A `switch_points` list is
    # what the *read* path inverts with, so a malformed one would silently misread
    # every value: it must carry exactly one finite, non-decreasing point per
    # member, start at 0 and stay inside the normalized domain. A boundary that
    # claims an evidence level must name the artifact it came from.
    boundaries = profile.get("enum_boundaries")
    if boundaries is not None:
        if not isinstance(boundaries, dict) or not isinstance(boundaries.get("applied"), bool):
            problems.append(
                ProfileProblem(
                    "enum_boundaries_block_valid",
                    "the enum_boundaries block must be an object with a boolean `applied`",
                )
            )
        else:
            artifact = boundaries.get("artifact")
            if boundaries["applied"] and not (
                isinstance(artifact, dict) and _nonempty(artifact.get("sha256"))
            ):
                problems.append(
                    ProfileProblem(
                        "enum_boundaries_block_valid",
                        "enum_boundaries.applied is true but the artifact reference is missing",
                    )
                )
            tolerance = boundaries.get("tolerance")
            if boundaries["applied"] and (not _is_number(tolerance) or not 0.0 < float(tolerance) < 1.0):
                problems.append(
                    ProfileProblem(
                        "enum_boundaries_block_valid",
                        f"enum_boundaries.tolerance {tolerance!r} is not a usable tolerance",
                    )
                )
            if boundaries.get("state") not in ENUM_BOUNDARY_BLOCK_STATES:
                problems.append(
                    ProfileProblem(
                        "enum_boundaries_block_valid",
                        f"enum_boundaries.state {boundaries.get('state')!r} "
                        f"not in {ENUM_BOUNDARY_BLOCK_STATES}",
                    )
                )

        # Row 11c - the value-bounds block (P25): the plugin's own parameter bounds,
        # attached where a declared range was a corpus sample narrower than them. An
        # `applied` block must name its source and the controls it widened; the
        # per-control record lives in `evidence.value_bounds`. A missing block is
        # legal (a profile built without the fact table), so absence is not a problem.
        value_bounds = profile.get("value_bounds")
        if value_bounds is not None:
            if not isinstance(value_bounds, dict) or not isinstance(value_bounds.get("state"), str):
                problems.append(
                    ProfileProblem(
                        "value_bounds_block_valid",
                        "the value_bounds block must be an object with a `state`",
                    )
                )
            else:
                widened = value_bounds.get("controls")
                if value_bounds["state"] == "applied":
                    if not _nonempty(value_bounds.get("source")):
                        problems.append(
                            ProfileProblem(
                                "value_bounds_block_valid",
                                "value_bounds.state is applied but the source is missing",
                            )
                        )
                    if not isinstance(widened, list) or not widened:
                        problems.append(
                            ProfileProblem(
                                "value_bounds_block_valid",
                                "value_bounds.state is applied but no control is listed",
                            )
                        )
                elif value_bounds["state"] not in ("none", "absent"):
                    problems.append(
                        ProfileProblem(
                            "value_bounds_block_valid",
                            f"value_bounds.state {value_bounds['state']!r} is not one of "
                            "applied/none/absent",
                        )
                    )
            for control in profile.get("controls") or []:
                if not isinstance(control, dict):
                    continue
                record = ((control.get("evidence") or {}) if isinstance(control.get("evidence"), dict) else {}).get(
                    "value_bounds"
                )
                if record is None:
                    continue
                before = record.get("declared_before") if isinstance(record, dict) else None
                after = record.get("declared_after") if isinstance(record, dict) else None
                if not (
                    isinstance(before, dict)
                    and isinstance(after, dict)
                    and _is_number(before.get("min"))
                    and _is_number(before.get("max"))
                    and _is_number(after.get("min"))
                    and _is_number(after.get("max"))
                ):
                    # `REFUSE_REBUILD`, like the two rows below: an unreadable record is
                    # evidence a reader cannot check, and the loader consults only
                    # `rebuild_required`/`cacheable` - a plain validation error would
                    # leave the entry served (found by the round's own review,
                    # 2026-10-12; before the fix this exact corruption was a cache HIT).
                    problems.append(
                        ProfileProblem(
                            "value_bounds_block_valid",
                            f"control {control.get('id')!r} carries an unreadable value_bounds "
                            "record (before/after ranges required)",
                            kind=REFUSE_REBUILD,
                        )
                    )
                elif not (
                    float(before["min"]) >= float(after["min"])
                    and float(before["max"]) <= float(after["max"])
                ):
                    # the rule the fact table follows: a correction **widens only** -
                    # a record that narrows or shifts one side is not a correction
                    problems.append(
                        ProfileProblem(
                            "value_bounds_block_valid",
                            f"control {control.get('id')!r} value_bounds is not a widening "
                            f"(before {before!r}, after {after!r})",
                            kind=REFUSE_REBUILD,
                        )
                    )
                else:
                    # the range the control actually carries has to *be* the corrected
                    # one: a record that says "widened to X" while the spine stayed (or
                    # was tampered) elsewhere documents nothing, and every write would
                    # run through the wrong mapping while the evidence claims otherwise
                    # (found by the round's review 2026-10-12: a range tampered to
                    # -999..999 validated `ok` and was served as a cache hit).
                    control_range = control.get("range") or {}
                    if not (
                        _is_number(control_range.get("min"))
                        and _is_number(control_range.get("max"))
                        and abs(float(control_range["min"]) - float(after["min"])) <= 1e-9
                        and abs(float(control_range["max"]) - float(after["max"])) <= 1e-9
                    ):
                        problems.append(
                            ProfileProblem(
                                "value_bounds_block_valid",
                                f"control {control.get('id')!r} records a widening to {after!r} "
                                f"but its range is {control_range!r}",
                                kind=REFUSE_REBUILD,
                            )
                        )

    for control in profile.get("controls") or []:
        if not isinstance(control, dict) or control.get("type") != "enum":
            continue
        control_id = control.get("id")
        law = control.get("law")
        params = law.get("params") if isinstance(law, dict) else None
        if not isinstance(params, dict):
            continue
        members = control.get("enum_values")
        count = len(members) if isinstance(members, list) else 0
        points = params.get("switch_points")
        if points is not None:
            valid = (
                isinstance(points, list)
                and len(points) == count
                and all(_is_number(point) for point in points)
                and float(points[0]) == 0.0
                and all(0.0 <= float(point) <= 1.0 for point in points)
                and all(
                    float(later) >= float(earlier)
                    for earlier, later in zip(points, points[1:])
                )
            )
            if not valid:
                problems.append(
                    ProfileProblem(
                        "enum_switch_points_valid",
                        f"control {control_id!r} switch_points must be {count} finite, "
                        "non-decreasing points in [0, 1] starting at 0",
                    )
                )
        positions = params.get("positions")
        if positions is not None:
            if not (
                isinstance(positions, list)
                and len(positions) == count
                and all(_is_number(position) and 0.0 <= float(position) <= 1.0 for position in positions)
            ):
                problems.append(
                    ProfileProblem(
                        "enum_switch_points_valid",
                        f"control {control_id!r} positions must be {count} numbers in [0, 1]",
                    )
                )
        # Row 6g - the measured *bands*, and the invariant that makes a write target
        # safe: the value written for a member must lie inside the band whose member it
        # selects, or the read path reports a member the host never prints for it. The
        # bands are carried in the evidence precisely so this can be re-checked here
        # rather than taken on trust (an independent review produced a two-ULP band
        # where a rounded midpoint escaped its own band).
        evidence = params.get("boundary_evidence")
        bands = evidence.get("band_ends") if isinstance(evidence, dict) else None
        if bands is not None:
            if not (
                isinstance(bands, list)
                and len(bands) == count + 1
                and all(_is_number(band) and 0.0 <= float(band) <= 1.0 for band in bands)
                and all(
                    float(later) >= float(earlier) for earlier, later in zip(bands, bands[1:])
                )
            ):
                problems.append(
                    ProfileProblem(
                        "enum_band_ends_valid",
                        f"control {control_id!r} boundary_evidence.band_ends must be "
                        f"{count + 1} non-decreasing numbers in [0, 1]",
                    )
                )
            else:
                if isinstance(points, list) and len(points) == count:
                    if [float(point) for point in points] != [float(band) for band in bands[:-1]]:
                        problems.append(
                            ProfileProblem(
                                "enum_band_ends_valid",
                                f"control {control_id!r} switch_points disagree with the bands "
                                "they claim to start",
                            )
                        )
                targets = (
                    [float(position) for position in positions]
                    if isinstance(positions, list) and len(positions) == count
                    else [index / max(1, count - 1) for index in range(count)]
                )
                # the last band is closed at the top of the domain unless the bands
                # stop short of it (an unlabelled region above the last member)
                closed_top = float(bands[-1]) == 1.0
                for index, target in enumerate(targets):
                    low, high = float(bands[index]), float(bands[index + 1])
                    ok = low <= target <= high if (closed_top and index == count - 1) else (
                        low <= target < high
                    )
                    if not ok:
                        problems.append(
                            ProfileProblem(
                                "enum_write_target_in_band",
                                f"control {control_id!r} member {index} would be written to "
                                f"{target!r}, outside its band [{low!r}, {high!r})",
                            )
                        )
            checked = evidence.get("band_interiors_checked")
            verified = evidence.get("band_interiors_verified")
            if isinstance(checked, int) and isinstance(verified, int) and checked != verified:
                problems.append(
                    ProfileProblem(
                        "enum_band_interior_verified",
                        f"control {control_id!r} carries a band map whose interiors the "
                        f"measurement did not confirm ({verified} of {checked})",
                    )
                )
        evidence = params.get("boundary_evidence")
        if evidence is not None:
            artifact = evidence.get("artifact") if isinstance(evidence, dict) else None
            if not isinstance(artifact, dict) or not _nonempty(artifact.get("sha256")):
                problems.append(
                    ProfileProblem(
                        "enum_boundary_evidence_valid",
                        f"control {control_id!r} boundary_evidence must name the artifact it "
                        "was measured from",
                    )
                )
            else:
                declared = evidence.get("state")
                if declared is not None and declared not in ENUM_BOUNDARY_EVIDENCE_STATES:
                    problems.append(
                        ProfileProblem(
                            "enum_boundary_evidence_valid",
                            f"control {control_id!r} boundary_evidence.state {declared!r} "
                            f"not in {ENUM_BOUNDARY_EVIDENCE_STATES}",
                        )
                    )

    # Row 12 - capture scope legality.
    state = profile.get("state")
    if isinstance(state, dict):
        capture = state.get("capture_scope")
        if isinstance(capture, dict):
            channels = capture.get("channels")
            if not isinstance(channels, list) or not channels:
                problems.append(ProfileProblem("capture_scope_legal", "capture_scope.channels missing"))
            else:
                for channel in channels:
                    if channel not in SUPPORTED_CAPTURE_CHANNELS:
                        problems.append(
                            ProfileProblem(
                                "capture_scope_legal",
                                f"capture channel {channel!r} not in {SUPPORTED_CAPTURE_CHANNELS}",
                            )
                        )
                params_spec = capture.get("parameters")
                if "document" in channels:
                    scoped = params_spec.get("scope") if isinstance(params_spec, dict) else None
                    if scoped != "full_vector":
                        problems.append(
                            ProfileProblem(
                                "capture_scope_legal",
                                "a document channel requires parameters.scope='full_vector' "
                                "(AUD-002: a document restore resets parameters)",
                            )
                        )

    return ProfileValidation(ok=not problems, problems=problems)
