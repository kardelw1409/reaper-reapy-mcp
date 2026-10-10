# 07 — Capability Profile Schema

Status: **[D] proposed** (rev. 2026-10-11, Phase C remediation → Phase D consolidation — see [18](18-remediation-register.md) and [19](19-owner-decisions.md)).
A Capability Profile is the **versioned, cacheable, static** description of
what a plugin/version can do. It never contains live values. Everything in this document is derived
from the POC's proven profile/cache construction [A: 02 §6-§7] generalized to any provider, with the
Phase-C corrections: the cache key covers **every behaviour-affecting determinant** (AUD-008), a
missing required identity fails closed for writes (AUD-008), and cache writes never destroy a foreign
or mismatched file (AUD-012).

## 1. Artifact shape (JSON, `profile_schema_version: 1`)

```json
{
  "profile_schema_version": 1,
  "provider": "vital",
  "identity": {
    "plugin_id": "vital",
    "vendor": "Vital Audio",
    "format": "vst3",
    "fx_name": "VST3i: Vital (Vital Audio)",
    "class_id": "56535456697461766974616C00000000",
    "module": "vital.vst3",
    "compatible_versions": ["1.5.5"],
    "fingerprint": {"param_count": 2858}
  },
  "provenance": {
    "generated_utc": "2026-…Z",
    "generator": "vital_provider/registry",
    "provider_revision": "vital-provider-2026.10-r1",
    "provider_artifact_digest": "sha256 over provider.artifact_paths()",
    "sources": [{"file": "vital_correspondence.json", "sha256": "…"}, …],
    "source_digest": "sha256 over canonical source list",
    "measured_on": {"host": "reaper", "version": "1.5.5", "evidence": ["phase2b_native_law.json", …]}
  },
  "modules": [ {"id": "filter_1", "kind": "filter", "label": "Filter 1", "parent": null,
                "controls": ["filter_1_cutoff", …], "state_refs": []} ],
  "controls": [ {"id": "filter_1_cutoff", "label": "Filter 1 Cutoff", "module": "filter_1",
                 "type": "number", "unit": "semitones", "range": {"min": 8, "max": 136},
                 "discrete": false, "aliases": [], 
                 "authority": {"read": "parameter", "write": "parameter"},
                 "binding": {"index": 104, "ident": "104:48629", "state_key": "filter_1_cutoff"},
                 "law": {"kind": "exponential", "params": {...}},
                 "evidence": {"strength": "strong", "source": "phase3_law_fit_final.json"}} ],
  "values": {"kinds": ["affine","enum_index","exponential","identity","none"],
             "unit_traps": ["%"]},
  "modulation": {
    "supported": true,
    "identity": "slot",
    "slots": 64,
    "sources": ["lfo_1", …], "destinations": ["filter_1_cutoff", …],
    "pairs_with_precedent": {"lfo_1|osc_1_level": 24, …},
    "source_family_rules": {"lfo_*": {"forbidden_destinations": ["modulation_*_amount"]}},
    "depth_model": {"kind": "per_destination_fit", "accepted": 246, "refused": 70,
                    "refused_destinations": ["lfo_1_frequency", …], "evidence": [...]},
    "write_channel": "state",
    "fields": ["amount","bipolar","stereo","power","bypass","line_mapping"],
    "line_mapping_write_supported": false,
    "structural_limits": {"one_route_per_state_write": true}
  },
  "state": {
    "supported": true,
    "codec": "vital_base64_json_v1",
    "structural_keys": ["modulations","wavetables","sample","custom_warps","lfos","random_values"],
    "structural_ops": [{"id": "wavetable_select", "target": "wavetables", "verified": true,
                        "limits": ["no frame growth"]}],
    "capture_scope": {"channels": ["document","routes","parameters"],
                      "parameters": {"scope": "full_vector", "ident_required": true},
                      "restore_order": ["document","parameters"],
                      "verify_order": ["document","parameters"]},
    "restore_points": "deferred_v1"
  },
  "dependencies": [
    {"id": "fm_needs_modulator", "kind": "hard", "when": {...}, "message": "…", "disable_set": {...}},
    {"id": "route_to_off_filter", "kind": "hard", "informational": false, "when": {...}}
  ],
  "write_order": ["state", "parameters", "verify"],
  "verification": {"tolerances": {"state.float": 1e-5, "parameter.normalized": 1e-3},
                   "comparators": {"enum": "exact", "number": "abs_le"}},
  "limits": ["VST2 build not supported", "one route per state write", "no line_mapping writes"],
  "x_provider": {}
}
```

Field tables for the object types are in [05 §2](05-universal-core-contract.md). This document adds
the profile-level rules.

## 2. Separation of the four state categories (normative)

| Artifact | Lifetime | Content | Where |
|---|---|---|---|
| **Capability Profile** | static until identity/source/provider changes | declarations only; no live values | cache file (key-addressed) |
| **Runtime State** | live | current parameter values, current document/routes | host, read on demand |
| **Transactional Snapshot** | one transaction | document + routes + the **full captured parameter vector** (AUD-002) | memory only during the transaction; complete-capture-or-refuse |
| **Persistent Restore Point** | durable, user-owned | explicit artifact with content hash + manifest | **deferred from V1** (AUD-009/Q-09) |

Rules: a snapshot is never silently promoted to a restore point; a profile never contains a live
value (the POC cache stores only derived metadata, and its file is deletable at any moment) [A: 02 §7];
within a write, a `not_captured` entry inside the declared capture scope is a **refusal**, not a
named gap ([11 §2.3](11-planning-transactions-and-verification.md)).

## 3. Versioning & compatibility

1. `profile_schema_version` (integer) is bumped when the **shape** changes; the cache format key
   (`capability-map-v1` equivalent) is bumped with it, so old files are rebuilt rather than
   mis-read [A: POC `CACHE_FORMAT_KEY` / `CACHE_SCHEMA`].
2. `identity.compatible_versions` declares the measured/claimed plugin versions. **V1 policy
   (resolved proposed; AUD-014 Q-03): a live version not in the list refuses writes — no override
   flag exists in V1.** `plugin_describe` reports `VersionMismatch` as an advisory with the measured
   set; `plugin_write`/`plugin_modulation` refuse (`VersionMismatch`, `write_calls_attempted: 0`).
   This supersedes the Phase-A acknowledgement-flag idea, which the audit rejected as a bypass that
   undermines fail-closed safety. VST2 Vital fails closed the same way (Q-11).
3. `provenance.source_digest` is part of the cache key: regenerating the provider's data produces a
   new key and the old profile is never served [A].
4. **`provenance.provider_revision` + `provider_artifact_digest` are part of the cache key
   (AUD-008).** The effective profile depends on provider **logic**, not only registry JSON:
   dependency rules, law-fitting behaviour, codec behaviour, write order, verification policy and
   declared limits all live in provider code. The provider therefore declares an
   `implementation_revision` and a list of behaviour-affecting `artifact_paths()`, and the key
   includes a digest over them. **Rule for change:** any semantic change to any declared artifact
   requires bumping `implementation_revision` in the same change; a release that alters behaviour
   without a revision bump is a release bug. A registry-data edit changes `source_digest`; a logic
   edit changes `provider_artifact_digest`; either invalidates the cached profile.
5. Identity fingerprint fields that must match live host reads: `fx_name`, `class_id`, `module`,
   `param_count`; `version` recorded, absence tolerated, presence mismatched → `VersionMismatch`
   [A: POC `REQUIRED_IDENTITY_FIELDS` analog].
6. **Fail-closed identity (AUD-008):** a missing **required** identity field means: no writes, no
   modulation mutations, no restore, and **no caching as a fully validated profile**. Explicitly
   limited diagnostic/provisional reads remain permitted, but their results are marked provisional
   in `meta` and may never be used as the basis of a mutation.

## 4. Validation rules (profile load/build-time)

| Check | Behaviour on failure |
|---|---|
| schema version known | refuse, rebuild from provider |
| identity fields present (provider-required subset) | refuse, rebuild |
| every control's `module` exists in `modules[]` (when both present) | validation error |
| `authority.write/read` ∈ {parameter, state}; a control with `authority.write=parameter` has a parameter binding | validation error |
| `type=enum` has ≥2 `enum_values`; `discrete` consistent | validation error |
  | `law.kind` known; `none`/`unmeasured` carry no params | validation error |
| `modulation.supported=false` ⇒ no `slots`/fields required; when true, `slots ≥ 1` | validation error |
| every dependency rule id unique; `kind` ∈ known set | validation error |
| `write_order` is a permutation of declared stages with `verify` last | validation error |
| `provenance.sources[]` non-empty with hashes | refuse cache write (a profile without provenance is not cacheable) |
| `provenance.provider_revision` non-empty + `provider_artifact_digest` present | refuse cache write (an undetermined provider revision cannot be a validated cache entry — AUD-008) |
| `state.capture_scope.channels` ⊆ supported channels; parameters scope `full_vector` when a document channel exists | validation error (a provider claiming a partial scope without a justification breaks AUD-002) |
| duplicate control ids; wrapper-only names | refuse build (POC registry refuses REAPER-wrapper-only names and duplicates unless explicitly included) [A] |

## 5. Invalidation

A cached profile is invalid when **any** of the following hold; each has an explicit handling rule
(no silent reuse):

| Trigger | Handling |
|---|---|
| fingerprint mismatch at use (fx_name/class/param_count/module) | re-resolve instance once; rebuild profile; never serve the old key |
| live version ∉ `compatible_versions` | `VersionMismatch`; writes refused (V1 policy §3.2) |
| source digest changed | new key; old file never served |
| **provider revision or artifact digest changed (AUD-008)** | new key; old file never served |
| content hash mismatch / truncated file | refuse the file; **quarantine by atomic rename** (bytes preserved); rebuild; write fresh via atomic temp+replace (AUD-012) |
| **file present at the expected path whose key differs from the computed key** | refuse to replace; quarantine; rebuild; the foreign bytes are preserved (AUD-012) |
| schema/format key changed | refuse; rebuild |
| cache file unreadable/unparsable | refuse; rebuild; report reason in `meta` |

**The refusal discipline, corrected (Phase C, AUD-012).** The Phase-A text claimed the POC
"leaves the old file byte-identical and writes the new map at its own name". Source verification
([17 AV-06/AV-07](17-planning-evidence-index.md)) shows `save()` **overwrites whatever sits at the
path** — no identity check, no quarantine, no atomic write. The five cases are therefore re-specified
as **V3 requirements**, not inherited behavior:

1. no usable identity → build in memory; **no cache read or write**; writes/mutations disabled
   (§3.6);
2. no file → build; write via temp file + `os.replace`; report `source: built`;
3. unreadable/unparsable → validate identity; **quarantine** (`<path>.invalid-<utc>`, atomic rename)
   if a file exists; write fresh; report the reason;
4. foreign key at the expected path → **never replace in place**: quarantine, then write fresh at the
   expected path; the quarantined bytes are preserved and the quarantine path is reported;
5. content-hash / source-digest / provider-revision / format mismatch → REFUSE the file, quarantine,
   rebuild, report.

Interrupted writes leave at most a `*.tmp-*` file that is never read (the target only ever appears
via atomic replace); a partial target file cannot exist.

## 6. Aliases and provenance/confidence

- `aliases[]` are **provider-declared** alternative names accepted by `plugin_find`/requests.
  The POC has no alias table in `controls.py` but does accept state keys in `plan.resolve_control_name`
  and provides `suggest()` — V3 makes aliases explicit profile data so this behaviour is declared,
  not emergent [A + [D] formalization].
- `evidence` = `{strength: strong|weak|none, source: <artifact>}`; surfaced in `plugin_find` output.
  A control whose law is `none`/`unmeasured` must be shown with that fact rather than a guessed
  range (POC DEC-10: `none` is a first-class answer) [A].
- Profile `limits[]` lists honest restrictions (VST2 unsupported, one route per state write, no
  line_mapping writes) — these are provider-declared, not Core knowledge.

## 7. Where profiles come from (V1)

Two construction modes, both provider-owned:

1. **Data-built (Vital V1):** the provider builds profiles from its measured registry files
   (`vital_correspondence.json`, `vital_scales_full.json`, `modulation_vocabulary.json`, plus
   provider code tables for modules/dependencies/limits). No live discovery is required for V1
   describe/find/read-of-parameter-lists; live reads supply **values and fingerprints only**.
2. **Probe-built (future providers):** a provider may implement `discover()` that reads host
   enumeration (param count/names) to produce a *minimal* profile. This is explicitly **not** in V1
   scope; the Core contract must merely not preclude it ([08 §5](08-provider-and-host-interfaces.md)).

## 8. Required tests (profile)

- Schema validation table (§4) — one test per failure row.
- Cache key stability and **change matrix (AUD-008)**: same determinants ⇒ same key; a change to any
  of — provider dependency rules, codec revision, verification policy, provider compatibility/
  version declaration, registry data — ⇒ different key; old profile never served.
- Foreign/mismatched-file discipline (AUD-012): the file at the expected path is **byte-identical**
  after a cache write that quarantined it; the quarantine file preserves the original bytes; an
  interrupted write leaves no partial target (simulated temp-file interruption).
- Truncation/corruption: partial file fails both content-hash and count checks → quarantine+rebuild.
- Identity-required fields missing ⇒ writes/mutations/restore disabled; provisional reads marked;
  no validated-profile cache write.
- Version policy: live version ∉ `compatible_versions` ⇒ writes refused with `write_calls_attempted:0`.
- Serialization round-trip: profile → JSON → profile equality modulo excluded volatile fields
  (`written_utc`-style fields excluded from content hash — POC precedent [A]).
