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
