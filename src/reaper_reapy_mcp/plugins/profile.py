"""Capability Profile schema: version, validation (contract 07 §1, §4).

P0 status: the schema version constant and the [07 §4] validation table as a
validator. Profile *building* and the cache key are P4; provenance digests are
used here only to decide cacheability (07 §4 rows 10/11).

The validator returns problems instead of raising: the caller decides between
"refuse + rebuild" and "refuse the cache write" exactly as the table demands.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "PROFILE_SCHEMA_VERSION",
    "CACHE_FORMAT_KEY",
    "LAW_KINDS",
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
PROFILE_SCHEMA_VERSION = 1
CACHE_FORMAT_KEY = "capability-map-v1"

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
    return isinstance(value, (int, float)) and not isinstance(value, bool)


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
