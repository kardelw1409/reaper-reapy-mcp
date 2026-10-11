"""P0 profile schema tests: one test per row of the 07 §4 validation table.

The fixture provider is deliberately generic (the boundary rules forbid a
provider identifier in core fixtures - 08 §8.5).
"""

from __future__ import annotations

import copy

from reaper_reapy_mcp.plugins.profile import (
    PROFILE_SCHEMA_VERSION,
    ProfileValidation,
    validate_profile,
)


def valid_profile() -> dict:
    return {
        "profile_schema_version": PROFILE_SCHEMA_VERSION,
        "provider": "demo",
        "identity": {
            "plugin_id": "demo",
            "vendor": "Demo Audio",
            "format": "vst3",
            "fx_name": "VST3i: Demo Synth",
            "class_id": "AABBCCDDEEFF00112233445566778899",
            "module": "demo.vst3",
            "compatible_versions": ["1.0.0"],
            "fingerprint": {"param_count": 12},
        },
        "provenance": {
            "generated_utc": "2026-01-01T00:00:00Z",
            "generator": "demo_provider/registry",
            "provider_revision": "demo-provider-r1",
            "provider_artifact_digest": "sha256:aaaa",
            "sources": [{"file": "registry.json", "sha256": "bbbb"}],
            "source_digest": "sha256:cccc",
        },
        "modules": [
            {"id": "tone", "kind": "filter", "label": "Tone", "parent": None,
             "controls": ["tone_cutoff"], "state_refs": []}
        ],
        "controls": [
            {
                "id": "tone_cutoff", "label": "Tone Cutoff", "module": "tone",
                "type": "number", "unit": "semitones", "range": {"min": 0, "max": 100},
                "discrete": False, "aliases": [],
                "authority": {"read": "parameter", "write": "parameter"},
                "binding": {"index": 3, "ident": "3:1001", "state_key": "tone_cutoff"},
                "law": {"kind": "identity", "params": {}},
                "evidence": {"strength": "strong", "source": "fit.json"},
            },
            {
                "id": "tone_shape", "label": "Shape", "module": "tone", "type": "enum",
                "enum_values": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
                "discrete": True, "aliases": [],
                "authority": {"read": "state", "write": "state"},
                "binding": {"state_key": "tone_shape"},
                "law": {"kind": "enum_index", "params": {}},
            },
        ],
        "values": {"kinds": ["affine", "enum_index", "exponential", "identity", "none"],
                   "unit_traps": ["%"]},
        "modulation": {
            "supported": True, "identity": "slot", "slots": 8,
            "sources": ["src_a"], "destinations": ["tone_cutoff"],
            "write_channel": "state", "fields": ["amount", "bipolar"],
            "line_mapping_write_supported": False,
        },
        "state": {
            "supported": True, "codec": "demo_tree_v1",
            "capture_scope": {
                "channels": ["document", "routes", "parameters"],
                "parameters": {"scope": "full_vector", "ident_required": True},
                "restore_order": ["document", "parameters"],
                "verify_order": ["document", "parameters"],
            },
            "restore_points": "deferred_v1",
        },
        "dependencies": [{"id": "rule_a", "kind": "hard", "message": "m", "when": {}}],
        "write_order": ["state", "parameters", "verify"],
        "verification": {"tolerances": {"state.float": 1e-05, "parameter.normalized": 0.001},
                         "comparators": {"enum": "exact", "number": "abs_le"}},
        "limits": ["demo limit"],
        "x_provider": {},
    }


def rules(result: ProfileValidation) -> list[str]:
    return result.codes()


def test_valid_profile_passes_and_is_cacheable():
    result = validate_profile(valid_profile())
    assert result.ok and result.problems == []
    assert result.cacheable and not result.rebuild_required


def test_row1_unknown_schema_version_refuses_rebuild():
    profile = valid_profile()
    profile["profile_schema_version"] = 999
    result = validate_profile(profile)
    assert not result.ok and result.rebuild_required
    assert "schema_version_known" in rules(result)


def test_row2_missing_identity_field_refuses_rebuild():
    profile = valid_profile()
    del profile["identity"]["class_id"]
    result = validate_profile(profile)
    assert not result.ok and result.rebuild_required
    assert result.codes().count("identity_fields_present") == 1
    profile2 = valid_profile()
    profile2["identity"]["fingerprint"] = {}
    assert validate_profile(profile2).rebuild_required


def test_row3_control_module_must_exist_when_modules_present():
    profile = valid_profile()
    profile["controls"][0]["module"] = "no_such_module"
    result = validate_profile(profile)
    assert "control_module_exists" in rules(result)


def test_row4_authority_spaces_and_parameter_binding():
    profile = valid_profile()
    profile["controls"][0]["authority"] = {"read": "telepathy", "write": "parameter"}
    result = validate_profile(profile)
    assert result.codes().count("authority_legal") == 1
    profile2 = valid_profile()
    profile2["controls"][0]["binding"] = {}
    result2 = validate_profile(profile2)
    assert "authority_legal" in rules(result2)
    profile3 = valid_profile()
    profile3["controls"][0]["authority"] = {"read": "parameter", "write": "state"}
    profile3["controls"][0]["binding"] = {}
    assert "authority_legal" in rules(validate_profile(profile3))


def test_row5_enum_members_and_discreteness():
    profile = valid_profile()
    profile["controls"][1]["enum_values"] = [{"id": "a", "label": "A"}]
    assert "enum_members" in rules(validate_profile(profile))
    profile2 = valid_profile()
    profile2["controls"][1]["discrete"] = False
    assert "enum_members" in rules(validate_profile(profile2))


def test_row6_law_kind_known_and_none_carries_no_params():
    profile = valid_profile()
    profile["controls"][0]["law"] = {"kind": "magic", "params": {}}
    assert "law_kind_known" in rules(validate_profile(profile))
    profile2 = valid_profile()
    profile2["controls"][0]["law"] = {"kind": "none", "params": {"scale": 2}}
    assert "law_kind_known" in rules(validate_profile(profile2))
    profile3 = valid_profile()
    profile3["controls"][0]["law"] = {"kind": "unmeasured", "params": {}}
    assert validate_profile(profile3).ok


def test_row7_modulation_declarations():
    profile = valid_profile()
    profile["modulation"]["slots"] = 0
    assert "modulation_supported" in rules(validate_profile(profile))
    profile2 = valid_profile()
    profile2["modulation"]["supported"] = False
    assert "modulation_supported" in rules(validate_profile(profile2))
    profile3 = valid_profile()
    profile3["modulation"] = {"supported": False}
    assert validate_profile(profile3).ok


def test_row8_dependency_rule_ids_unique_and_kinds_known():
    profile = valid_profile()
    profile["dependencies"] = [
        {"id": "rule_a", "kind": "hard"},
        {"id": "rule_a", "kind": "soft"},
    ]
    assert "dependency_rules_unique" in rules(validate_profile(profile))
    profile2 = valid_profile()
    profile2["dependencies"] = [{"id": "rule_a", "kind": "explosive"}]
    assert "dependency_rules_unique" in rules(validate_profile(profile2))


def test_row9_write_order_permutation_with_verify_last():
    profile = valid_profile()
    profile["write_order"] = ["state", "parameters"]
    assert "write_order_valid" in rules(validate_profile(profile))
    profile2 = valid_profile()
    profile2["write_order"] = ["verify", "state", "parameters"]
    assert "write_order_valid" in rules(validate_profile(profile2))
    profile3 = valid_profile()
    profile3["write_order"] = ["state", "state", "verify"]
    assert "write_order_valid" in rules(validate_profile(profile3))


def test_row10_provenance_sources_non_empty_with_hashes():
    profile = valid_profile()
    profile["provenance"]["sources"] = []
    result = validate_profile(profile)
    assert "provenance_sources" in rules(result)
    assert not result.cacheable and not result.rebuild_required
    profile2 = valid_profile()
    profile2["provenance"]["sources"] = [{"file": "registry.json"}]
    assert "provenance_sources" in rules(validate_profile(profile2))
    profile3 = valid_profile()
    del profile3["provenance"]
    assert "provenance_sources" in rules(validate_profile(profile3))


def test_row11_provider_revision_and_artifact_digest_required_for_cache():
    profile = valid_profile()
    profile["provenance"]["provider_revision"] = ""
    result = validate_profile(profile)
    assert "provider_revision_present" in rules(result) and not result.cacheable
    profile2 = valid_profile()
    del profile2["provenance"]["provider_artifact_digest"]
    assert "provider_revision_present" in rules(validate_profile(profile2))


def test_row12_capture_scope_legality():
    profile = valid_profile()
    profile["state"]["capture_scope"]["channels"] = ["document", "magic"]
    assert "capture_scope_legal" in rules(validate_profile(profile))
    profile2 = valid_profile()
    profile2["state"]["capture_scope"]["parameters"] = {"scope": "planned_only"}
    assert "capture_scope_legal" in rules(validate_profile(profile2))
    profile3 = valid_profile()
    profile3["state"]["capture_scope"] = {"channels": ["parameters"],
                                         "parameters": {"scope": "declared_subset"}}
    assert validate_profile(profile3).ok


def test_row13_duplicate_control_ids_refused():
    profile = valid_profile()
    profile["controls"].append(copy.deepcopy(profile["controls"][0]))
    assert "no_duplicate_controls" in rules(validate_profile(profile))


def test_validator_does_not_mutate_the_profile():
    profile = valid_profile()
    snapshot = copy.deepcopy(profile)
    validate_profile(profile)
    assert profile == snapshot
