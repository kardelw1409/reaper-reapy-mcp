# IMPLEMENTATION-STATUS — Universal Plugin MCP V3

Long-lived implementation journal for the Universal Plugin MCP V3 work in this
repository. Phase definitions, gates and acceptance criteria live in
`docs/universal-plugin-mcp-v3/14-implementation-plan.md`; binding decisions in
`docs/universal-plugin-mcp-v3/19-owner-decisions.md`; the entry point is
`docs/universal-plugin-mcp-v3/20-implementation-handoff.md`.

Rules this journal follows: results are recorded only after the phase's tests
have actually run; counts are reported verbatim; anything not executed is named
as not executed; deviations are recorded with the reason instead of being
smoothed over.

---

## 0. State before implementation (fixed at session start, 2026-10-10)

- Repository: `C:\repos\reaper-reapy-mcp`, branch `feature/vst-bridge` at
  `c060def` ("Add implementation handoff document and update README for V3 planning status"),
  clean working tree.
- Only untracked file: `composer_v1_2_lm_probe.md` (user's; untouched).
- The previously staged user change `src/controllers/project_controller.py`
  (26+/3-, recorded in `01 §7` / `17 T-13`) was committed by the owner in
  `5f199cf` together with the planning documents. It is not pending and was not
  touched. **Correction to the planning notes:** `20 §6` and `13 §6` still
  describe it as staged; the repository state is authoritative.
- Preconditions checked: owner decisions 1-4 are `OWNER_APPROVED` (19 §6); no
  decision remains `OWNER_APPROVAL_REQUIRED` (19 §5 has none); `src/reaper_reapy_mcp/plugins/`
  and `tests/plugins/` did not exist; `pyproject.toml` version 0.1.3; toolchain
  `py -3.11` (Python 3.11.9, pytest 8.4.2, reapy 0.10.0, mcp present).
- No live mutation was performed at any point of this work. No REAPER project,
  track, FX chain, route or plugin state was changed.

### Measured environment fact used by P0-1 (new, not in the planning docs)

`import reaper_reapy_mcp` (the pre-existing package `__init__` -> `server` ->
`controllers` chain) performs host I/O at import time: `controllers/base_controller.py`
does `from reapy import reascript_api` at module level, and that import opens
sockets to the reapy ports and reads package data (verified with a
`sys.addaudithook` probe, 2026-10-10). This is pre-existing behaviour and is
consistent with the approved eager startup lifecycle (19 Decision 4 / AUD-006
Option A); **it was deliberately not changed** — Decision 4 forbids a lifecycle
redesign and 20 §6 forbids touching unrelated behaviour. The consequence for
P0-1 is recorded under P0 below.

---

## 1. P0 — Contracts and owner decisions

**Status:** completed 2026-10-10. Suite: `py -3.11 -m pytest tests/plugins -q`
-> **38 passed, 1 xfailed** (the xfail is the declared P1 protocol-conformance
stub). Regression: `py -3.11 -m pytest tests/test_midi_mock.py -q` -> 2 passed.

**Created (all new files, nothing existing modified):**

| File | Content |
|---|---|
| `src/reaper_reapy_mcp/plugins/__init__.py` | package docstring (import discipline, frozen topology), exports `setup_plugin_tools` |
| `src/reaper_reapy_mcp/plugins/errors.py` | closed 05 §7 taxonomy: 28 `PluginError` subclasses, stable `code`, `to_dict() -> {code, message, detail}`, `ERROR_CODES` |
| `src/reaper_reapy_mcp/plugins/model.py` | declarative dataclasses of 05 §2 / §2.8 / §2.9 (incl. the eight-object transaction set), `Stage`/`Verdict`/`FinalState`, the normative `VERDICT_FINAL_STATE` table |
| `src/reaper_reapy_mcp/plugins/profile.py` | `PROFILE_SCHEMA_VERSION`, `CACHE_FORMAT_KEY`, the 07 §4 validation table as `validate_profile()` |
| `src/reaper_reapy_mcp/plugins/provider.py` | `PluginProvider` and `StateCodec` protocols (08 §2-§3), `DependencyRuleSet` |
| `src/reaper_reapy_mcp/plugins/host/__init__.py` | host package docstring (only `reaper.py` may import the host API) |
| `src/reaper_reapy_mcp/plugins/host/contract.py` | `HostBridge` protocol incl. the slice-locating signature and the transport chokepoint |
| `src/reaper_reapy_mcp/plugins/tools.py` | `TOOL_NAMES`/`SPEC`/`DISPATCH`, schema-2 envelope builders, docstring synthesis, registration closure; **no tool body** |
| `tests/plugins/conftest.py` | `src` bootstrap; nothing host-related |
| `tests/plugins/l0_boundary.py` | static L0 boundary checker (host imports; provider identifiers) |
| `tests/plugins/test_p0_contracts.py` | import discipline, error taxonomy, tool tables, envelope, registration closure, P1 stub |
| `tests/plugins/test_p0_boundary.py` | boundary clean + four failure probes (non-vacuity) |
| `tests/plugins/test_p0_profile_schema.py` | one test per 07 §4 row + valid fixture + no-mutation check |

**Critical tests (14 P0 G):**

| # | Test | Result |
|---|---|---|
| P0-1 | `test_plugin_package_import_is_side_effect_free` | PASS with a recorded qualification: measured in a fresh process with the **pre-existing** package chain warmed first; during the plugin package's own import there are **zero** socket events and **zero** registry/cache file reads (`host_calls == []`, `bad_opens == []`). The literal command `python -c "import reaper_reapy_mcp.plugins"` succeeds, but in a cold process it first executes the pre-existing `__init__` chain, which does host I/O — see §0 and the design note |
| P0-2 | `test_l0_checker_detects_host_import_probe` (+ lazy-import, identifier, allowed-importer, providers-package probes) | PASS — the checker fails on a probe that imports the host API in core, so it cannot pass vacuously |
| P0-3 | `test_tool_names_disjoint_from_existing_host_tools` | PASS — 57 names parsed from `src/mcp_tools.py`, intersection with the 7 `plugin_*` names is empty, union 64 |
| P0-4 | `test_every_mutating_tool_declares_dry_run_true` | PASS — `plugin_write` and `plugin_modulation` declare `dry_run` default `True`; read-only tools declare no mutating ops |

**Acceptance criteria:** all plugin modules import with zero side effects of
their own (measured); boundary tests pass; the 07 §4 table is a validator with
one test row per rule; all owner decisions are recorded (`OWNER_APPROVED`, no
`OWNER_APPROVAL_REQUIRED` remains). **Failure conditions:** none of the P0
failure conditions (behaviour instead of a contract; a decision asserted
without the owner; host work at import; a name collision) occurred.

**Deviations / observations to carry forward:**

1. P0-1's literal reading ("no host call for `python -c "import reaper_reapy_mcp.plugins"`")
   is not achievable without changing the pre-existing `__init__` import chain,
   which Decision 4 and 20 §6 forbid. The plugin layer's own import is
   side-effect free and is measured as such. Flagged for the owner; not a
   plugin-layer gate failure. (If the owner ever wants the literal property, the
   minimal change is a PEP 562 lazy `reaper_reapy_mcp/__init__.py`; it does not
   change the server startup lifecycle.)
2. `tools.py` classifies an unexpected internal exception as the envelope-level
   code `InternalError` — an envelope fault code, not an addition to the closed
   05 §7 domain taxonomy. Registration with no dispatch body returns the same
   code (honest "not implemented in this phase" answer instead of a traceback).
3. The L0 boundary test is static (AST + text) by necessity: a runtime
   `sys.modules` check would be vacuous because the parent package imports host
   modules before `plugins/` loads.
4. No contract in 05/06/07/08/09/11 was changed in P0.

---

## 2. P1 — Universal models, identity objects and mock fixtures

**Status:** completed 2026-10-10. Suite: `py -3.11 -m pytest tests/plugins -q`
-> **79 passed, 0 failed, 0 xfailed** (the P0 xfail stub was replaced by the real
protocol-conformance test in `test_p1_mocks.py`).

**Created / extended:**

| File | Content |
|---|---|
| `src/reaper_reapy_mcp/plugins/model.py` (extended) | real bodies: `utc_now_iso`, `text_digest`, `canonical_json`, `final_state_for`; `StateRevision.compute` (explicit `none` markers, never an empty-set hash); `RevisionTracker` (seed/check/advance/tri-state `is_own_verified_write`); `TargetedStateSlice.is_valid_for`/`block_digest`; `StateSnapshot.captured_channels`/`missing_channels`/`is_complete_for`; `HostAccounting.zero_writes`/`as_dict`; `Outcome.to_dict`/`mapping_is_consistent` |
| `src/reaper_reapy_mcp/plugins/resolve.py` | session-scoped `TargetToken` + `TargetTokenRegistry`: issuance from fresh facts, revalidation with the moved fields in the refusal detail, redacted `__repr__`, `to_meta()` as the only sanctioned id carrier |
| `tests/plugins/fixtures.py` | block/chunk builders (single, foreign, duplicate, CRLF-capable), `split_outside` / `byte_diff_span` / `char_span_of_lines`, demo document/params/facts/profile, dotted-path helpers |
| `tests/plugins/mock_provider.py` | `MockProvider` + codec A (`b64-json-block-v1`): control table, laws, `%`-trap, two-route model, hard dependency with a real evaluator, capture scope, write order, verification policy |
| `tests/plugins/mock_codec_b.py` | codec B (`tree-lines-v1`): plain `path=json` lines, section-split batching (two units where codec A writes one) |
| `tests/plugins/mock_host.py` | full `HostBridge` double: call log + counters, four injection modes, byte-accurate multi-block chunks, replaceable slot, external param/document/slot hooks, mock-side locator with unique-or-refuse rules |
| `tests/plugins/test_p1_models.py` | verdict↔final_state table (total, single-valued, consistent with `Outcome`), counters rule, revision determinism/`none` markers, tracker behaviour, slice validity, snapshot completeness, §2.9 field sets |
| `tests/plugins/test_p1_identity.py` | token issuance/revalidation/refusals (location, name, build, counts), no silent re-target, redaction, no token in any serialized outcome |
| `tests/plugins/test_p1_mocks.py` | protocol conformance, multi-block selection (foreign first, third after), span exactness, ordinal correlation for two identical instances, count disagreement, splice byte fidelity, stale-slice refusal, all four injection modes, batched read, codec B round-trip and batching difference |

**Critical tests (14 P1 G):**

| # | Test | Result |
|---|---|---|
| P1-1 | `test_valid_token_revalidates_against_unchanged_facts`, `test_stale_token_is_refused_with_the_fields_that_moved`, `test_location_change_is_detected`, `test_identity_build_change_is_detected`, `test_unknown_or_expired_token_is_refused` | PASS — a stale token fails with `TargetChanged`, the registry is unchanged (no silent re-target) |
| P1-2 | `test_tracker_reports_external_change_and_never_self_conflicts` | PASS — advancing from the own verified read-back removes the self-conflict; an interleaved external value conflicts |
| P1-3 | `test_locate_selects_the_identity_matching_block_not_the_first`, `test_span_covers_only_the_selected_block`, `test_splice_keeps_every_other_byte_and_updates_only_the_target` | PASS — non-selected blocks byte-identical (`split_outside` equality) |
| P1-4 | `test_store_nothing_write_reports_api_success_and_stores_nothing`, `test_store_nothing_state_write_reports_success_without_changing_the_chunk` | PASS — "API success, nothing stored" is reproducible |
| P1-5 | `test_serialized_outcome_never_contains_the_token`, `test_token_repr_and_meta_never_expose_the_whole_id_through_repr` | PASS — the outcome carries no token; `meta` is the only sanctioned carrier |

**Acceptance criteria:** every 05 §2.9 object has its normative fields (asserted);
the mocks can produce every F1–F12 situation on demand (injections + external
hooks); both codecs pass the same shape-independent helpers (decode/verify/
snapshot/restore) and have provably different batching.

**Failure conditions:** none occurred — no token was serialized anywhere, the
mocks reproduce the host failure modes including `store-nothing`, no model field
was added without the documents.

---

## 3. P2 — Universal transaction mechanics (mock-proven)

**Status:** completed 2026-10-10. Suite: `py -3.11 -m pytest tests/plugins -q`
-> **107 passed, 0 failed**.

**Created / extended:**

| File | Content |
|---|---|
| `src/reaper_reapy_mcp/plugins/values.py` | law dispatch (`identity`/`affine`/`exponential`/`enum_index`/`none`), range/enum/bool validation, the `%`-trap, `display_to_write_space` and the inverses used for space-correct verification |
| `src/reaper_reapy_mcp/plugins/dependencies.py` | finding evaluation (provider evaluator or declarative `when` signals), `hard_findings`, `raise_on` → `DependencyViolation` with rows |
| `src/reaper_reapy_mcp/plugins/plan.py` | `LiveState`, `build_plan` (items name their channel), `state_changes`, `parameter_pairs`, `project_world` (live state overlaid with the plan) |
| `src/reaper_reapy_mcp/plugins/transaction.py` | the state machine: preflight → plan → snapshot → dry-run → ordered units with per-unit token+revision revalidation → verify in the written space → commit \| per-channel verified rollback; counters; cancellation; the target lock |
| `src/reaper_reapy_mcp/plugins/resolve.py` (extended) | one token per validated target (so two requests share the lock), `lock_for()` target-scoped in-process lock (19 Decision 3) |
| `src/reaper_reapy_mcp/plugins/errors.py` (extended) | `error_from_code` (problem rows carry codes) |
| `tests/plugins/test_p2_transaction.py` | the P2 matrix (26 cases; extended to 33 by the independent-review follow-ups) |

**Critical tests (14 P2 G and the B-matrix rows that belong to P2):**

| # | Case | Result |
|---|---|---|
| P2-1 | `test_capture_failure_refuses_with_zero_writes`, `test_unreadable_parameter_ident_in_the_vector_refuses` | PASS — `SnapshotUnavailable`, `write_calls_attempted == 0`, nothing written (B-32) |
| P2-2 | `test_failure_after_verified_state_units_restores_both_channels`, `test_state_write_that_does_not_land_is_write_not_applied_and_restored` | PASS — both channels restored and compared; `FAILED_AND_RESTORED` only when both true (B-33/B-34) |
| P2-3 | `test_document_reset_restores_parameters_after_the_document` | PASS — the reset fixture proves the restore order document → parameters on the call log (B-37) |
| P2-4 | `test_external_change_before_a_unit_refuses_with_zero_writes` | PASS — `StateConflict` before the unit, zero writes (B-42) |
| P2-5 | `test_dry_run_default_applies_nothing`, `test_registered_write_tool_keeps_the_dry_run_default_end_to_end` | PASS — the default performs zero writes through the real engine + mock host (AUD-009/AUD-011) |
| P2-6 | `assert_counters_consistent` inside every P2 test | PASS — `attempted == verified + unknown` and the verdict↔final_state mapping hold in every reached state (B-45/B-46) |
| extra | `test_rollback_failure_is_loud_and_unknown` (B-36), `test_restore_write_that_lands_wrong_is_loud_and_never_claims_restored` (B-35), `test_failed_and_not_restored_when_both_channels_diverged_externally`, `test_externally_diverged_channel_is_left_untouched_during_rollback` (B-53), `test_cancellation_*` (B-51/B-52), `test_concurrent_transactions_on_one_target_are_serialized` (B-50), codec-B genericity (AUD-004), document-before-parameters order, one-batch parameter write | PASS |

**Reached verdicts:** `ALL_VERIFIED`, `NO_WRITES`, `REFUSED_BEFORE_ANY_WRITE`,
`FAILED_AND_RESTORED`, `FAILED_AND_PARTIALLY_RESTORED`, `FAILED_AND_NOT_RESTORED`,
`ROLLBACK_FAILED` (7 of 8). `REFUSED_AFTER_A_WRITE` is declared unreachable in
V1 single-request transactions (11 §3) and is covered by the mapping test only.

**Failure conditions:** none occurred — no write proceeded with an incomplete
capture, no rollback was reported verified without per-channel comparison, no
verdict maps to two final states, no test uses a wall-clock sleep, and no test
passes only because the mock smooths over the document→parameters order (the
order is asserted on the call log).

**Design decisions recorded (P2 note in 14-implementation-plan.md):** see the
appended design note — revision digest components, verified-vs-unknown effect
accounting, the both-channels-first restorability check, cancellation's error
code, engine-built ordered units, one token per target.

---

## 4. P3 — Target-aware, read-only bridge + L3 read validation

**Status:** completed 2026-10-10. Suite: `py -3.11 -m pytest tests/plugins -q`
-> **134 passed, 0 failed, 0 skipped** (the live read conformance ran against the
running REAPER; it is skipped with an explicit reason when REAPER is absent).
Regression: `py -3.11 -m pytest tests/test_midi_mock.py -q` -> 2 passed.

**Created / extended:**

| File | Content |
|---|---|
| `src/reaper_reapy_mcp/plugins/host/reaper.py` | the REAPER bridge, **read paths only**: lazy host client import (module import is I/O-free), lazy single connection, the single timeout/wedge chokepoint with failure classification, `list_tracks`, `fx_facts` (identity from the chunk headers, correlated by the host's own name ordinal), `param_idents`, batched `read_params`, `read_state_chunk`, `locate_state_slice`; plus the pure `parse_blocks` / `locate_block_span` |
| `src/reaper_reapy_mcp/plugins/resolve.py` (extended) | instance resolution: `iter_instances`, `resolve_instances`, `resolve_one` (selector narrowing, `InstanceNotFound`/`AmbiguousInstance`, token issuance); token version comparison added (presence tolerated, a differing present pair is a difference) |
| `tests/plugins/test_p3_slicing.py` | B-26…B-31 run against BOTH locators (production + mock) on the same fixtures; CRLF chunks; stale-slice rule; movement/replacement detection |
| `tests/plugins/test_p3_no_write.py` | write-token source scan, attribute probe, I/O-free import in a fresh process, lazy-import assertion |
| `tests/plugins/test_p3_conformance.py` | the read suite against the mock and the real bridge; the wedge chokepoint; transport classification |
| `tests/plugins/live/probe_l3_read.py` | read-only L3 probe writing per-run evidence |
| `tests/plugins/live/probe_fxguid.py` | the FX-GUID repopulation measurement, **gated and NOT executed** (a live mutation belongs to P8) |

**Critical tests (14 P3 G):**

| # | Test | Result |
|---|---|---|
| P3-1 | `test_b26_non_first_target_block_is_selected_exactly`, `test_b26b_splicing_changes_only_the_selected_span`, `test_b27_preceding_foreign_block_is_never_the_target_or_the_document` | PASS on both locators — the span covers only the target block; all other bytes identical |
| P3-2 | `test_b28_two_identical_instances_need_a_consistent_ordinal`, `test_b31_inconsistent_counts_refuse`, `test_b29_module_string_discriminates_the_build`, `test_missing_identity_evidence_refuses_instead_of_guessing` | PASS — `TargetSliceAmbiguous`/`Unreadable` with zero reads beyond the chunk; the module string separates two builds that share a class id |
| P3-3 | `test_read_conformance_against_the_real_bridge_when_reachable` (ran live) + `probe_l3_read.py` evidence `l3_read__2026-10-10T174642Z__4a61640e7cef.json` | PASS live — the open instance resolves with `fx_name`/`class_id`/`module`/`param_count` (2858, matching the recorded measurement), the located slice spans lines 29-865 and `slice_outside_bytes_intact: true`. **FX-GUID repopulation behaviour remains UNMEASURED** (the probe refused, exit 2): L-3 stays open |
| P3-4 | live read suite + mock counter assertions (`count("read_params") == 1`) | PASS — one batched hop for the index list, never per control |
| P3-5 | `test_module_source_contains_no_write_token`, `test_bridge_class_exposes_no_write_verb` | PASS — no write verb exists in the module (mechanical scan + attribute probe) |

**Acceptance criteria:** the live read of the instance on the open project
succeeds and matches its recorded identity; no write path exists in the module
(grep-asserted, and `test_importing_the_read_bridge_performs_no_host_io` proves
importing it touches nothing); slice location is unique-or-refusal on every
fixture in the suite; the FX-GUID behaviour is **not asserted** — it is
implemented as a gated measurement and honestly reported as unexecuted.

**Failure conditions:** none occurred — no write verb appeared, no block was
chosen by position alone, no live read was reported without a recorded evidence
artifact, and the selected span is byte-identical outside the slice.

**Design note:** the P3 note (in 14-implementation-plan.md) records the read
bridge's design decisions: identity from chunk headers, the name-ordinal
correlation, the lazy host import, and why the FX-GUID measurement is deferred
(and what it would change).

---

## 5. Independent review (GLM-5.3 Flash subagent, 2026-10-10)

Task given (read-only, no file edits): review `transaction.py`,
`plugins/host/reaper.py`, `resolve.py` and the P2/P3 tests against [11 §3-§4],
[05 §2.8/§2.9/§5/§7], [14 P2/P3] and [12 §5], answering four questions
(F1–F12 coverage; counter/rollback honesty; AUD-001 addressing; test claims vs
assertions) with `file:line` evidence.

Findings and dispositions (each was verified against the source before acting):

| Finding | Severity | Disposition |
|---|---|---|
| An exception between a write verb's return and the effect increment (the verify read dying, a cancellation) left that write **uncounted**, breaking `attempted == verified + unknown` and suppressing the normative `write_effects_unknown > 0` for B-18/B-54 | high | **FIXED** — `_issue_write()` counts the attempt and marks the effect provisionally unknown before the verb; `_establish_effect()` upgrades it only after a read-back establishes it. Regression test: `test_a_read_failure_after_a_write_keeps_the_counters_honest` |
| A rollback could report the parameters channel verified without any comparison when the captured vector is empty | medium | **FIXED** — an empty captured vector is now compared against the live vector (`test` reachable via a zero-parameter provider) |
| A zero-unit document restore returned "restored" without a comparison | low | **FIXED** — it now proves an empty change set from the codec |
| The rollback's revision advance digested a hex digest instead of block text | low | **FIXED** |
| `outcome.stage` collapsed to `rollback` for post-write failures, losing the F4/F5 `stage: verify` distinction (06 §2 example) | low | **FIXED** — the failing stage is reported; `rollback` only when the compensation itself failed |
| A failed restorability probe was labelled `not_restored (external_change)` | low | **FIXED** — the probe's own reason is reported and both conditions are marked loudly |
| The locator's host-count cross-check was opt-in: `ordinal` alone could pick among N identity-equal blocks | low | **FIXED in both locators** — a multi-match correlation now requires the host-derived count to agree |
| F8 (pre-write transport failure), F5's forward parameter arm, B-42's mid-sequence arm, the B-28 write half, the reads counter and the 7-tool registration sweep were not exercised | low (test gaps) | **FIXED** — five new tests + the registration sweep added; each failed/passed against the real behaviour before/after |

Acknowledged and NOT changed (recorded, not hidden): `REFUSED_AFTER_A_WRITE`
remains unreachable in V1 single-request transactions by contract (11 §3) and is
covered by the mapping table only; the full two-transaction interleaving is
demonstrated by lock blocking plus zero writes rather than by a racing schedule;
the FX-GUID repopulation measurement remains unexecuted (P8-class mutation);
cancellation is reported through `PlanError` because the closed taxonomy has no
cancellation code (flagged for the owner in the P2 design note).

Suite after the fixes: `py -3.11 -m pytest tests/plugins -q` ->
**141 passed, 0 failed, 0 skipped**. Regression: 2 passed.

---

## 6. Owner decision recorded for P0-1 (restated for the next agent, unchanged)

The owner confirmed, and this journal records verbatim so it is not re-litigated:

- the existing **eager startup** REAPER MCP is kept as it is;
- the startup chain is **not** to be changed merely so the server can start with
  REAPER closed;
- the import-time side-effect boundary is a property of the **new plugin
  package**, and it is measured there (P0-1: zero socket events and zero
  registry/cache reads during the plugin package's own import);
- host I/O performed by the pre-existing `__init__.py -> server -> controllers`
  chain is documented separately (§0) and is **not** attributed to the plugin
  layer, and the old MCP is not credited with a guarantee it does not provide;
- no refactor of the old startup lifecycle was performed for that test.

If the literal property ("`import reaper_reapy_mcp.plugins` performs no host
I/O in a cold process") is ever wanted, the minimal change is a PEP 562 lazy
`reaper_reapy_mcp/__init__.py`; it is out of scope while the owner decision above
stands.

---

## 7. P4 — Capability profile and cache

**Status:** completed 2026-10-10. Suite: `py -3.11 -m pytest tests/plugins -q`
-> **164 passed, 0 failed** (141 before P4 + 23 new). Regression:
`py -3.11 -m pytest tests/test_midi_mock.py -q` -> 2 passed.
Targeted: `py -3.11 -m pytest tests/plugins/test_p4_cache.py -q` -> **29 passed**
(the file grew to 29 during the audit follow-ups, §10).

**Created / extended:**

| File | Content |
|---|---|
| `src/reaper_reapy_mcp/plugins/cache.py` (new) | `build_capability_profile` (Core wraps the provider profile with `provider_revision`, the digest over `provider.artifact_paths()` and the digest over the declared sources — AUD-008); `compute_cache_key` with all five determinants explicit and `__absent__` markers (never an empty-string hash); `ProfileCache` with validate-identity-before-replacement, quarantine by **atomic rename** (`os.rename`, collision-free path), atomic temp+`os.replace` publish, content hash + count cross-check, identity comparison, **key re-derivation from the entry's own provenance**, user-local default directory with `REAPER_MCP_PLUGIN_CACHE` override (empty value = configuration error), a source-tree warning, and `CacheStatus.snapshot()` so a resolution never reports a later call's outcome |
| `src/reaper_reapy_mcp/plugins/resolve.py` (extended) | `identity_missing_fields`, `version_refusal` (07 §3.2 measured set, no override), `ProfileResolution` with `write_blockers()`/`require_writable()`/`meta()` (the fail-closed write gate of AUD-008), `resolve_instance_profile` (cache hit/build/rebuilt, provisional reads with no cache read *or* write when a required identity field is missing) |
| `tests/plugins/test_p4_cache.py` (new) | the [09 §9] matrix: directory policy, key stability, the **B-47 change matrix** (fingerprint / registry data / provider revision / artifact digest / schema / format), five refusals, **B-55** byte-compare quarantine, **B-56** interrupted write and stray temp files, truncation failing two independent checks, hot-path zero-parameter-reads, round-trip equality modulo volatile stamps, no-identity fail-closed, version gate, plus the audit regressions (§10) |

**Critical tests (14 P4 G):**

| # | Test | Result |
|---|---|---|
| P4-1 | `test_b55_foreign_file_at_expected_path_is_never_replaced_in_place`, `test_quarantine_uses_an_atomic_rename_and_never_a_copy` | PASS — bytes preserved at the quarantine path, `os.rename` used exactly once, the target never carried the foreign bytes after the publish |
| P4-2 | `test_no_file_is_built_and_written_atomically`, `test_b56_stray_temp_file_is_never_read_and_never_blocks`, `test_b56_failure_between_temp_write_and_replace_leaves_no_partial_target` | PASS — a completed write leaves no temp file; a stray temp is never read; a failed `os.replace` leaves no partial target; `test_b56…` (the failure case) was written *and* failed *before* the guard existed |
| P4-3 | `test_hot_path_cache_hit_performs_zero_host_parameter_reads` | PASS — a cache hit reads zero parameters (and still reads facts, so the assertion is not vacuous) |
| P4-4 | `test_b47_every_determinant_changes_the_key`, `test_b47_old_profile_is_never_served_after_a_determinant_changes` | PASS — seven determinants produce seven distinct keys; a regenerated source produces a new key and the old entry is untouched and unreachable |
| P4-5 | `test_p4_5_no_identity_means_no_cache_file_and_writes_refused` | PASS — `missing_identity == ["param_count"]`, `source: none`, no entry file, `require_writable()` raises `SnapshotUnavailable` naming the field, `meta.provisional == True` |

**Acceptance criteria:** describe works fully offline against the mock provider;
the key covers all five components (B-47); the cache directory is configurable
with a user-local default outside any source tree; `meta.cache` reports `source`,
refusals, quarantine paths and warnings; no foreign bytes are lost (B-55).
**Failure conditions:** none occurred — no cache hit served a profile whose
identity disagreed with the live instance; no foreign file was overwritten or
deleted; no profile was written without identity; the key omits no component; the
default directory is not inside a source tree.

**Deviations / observations carried forward:**

1. The quarantine stamp is a filesystem-safe UTC form
   (`YYYYMMDDTHHMMSSZ`), not the ISO string: the ISO form contains `:`, which is
   illegal in Windows file names. The instant is preserved, the separators are not.
2. `cache_format_key` was **not** bumped although the entry envelope gained
   `provider_id`: the cache has never been released, there is no file to
   mis-read, and the loader refuses an entry without it (§10 finding 3).
3. P4 delivers the profile/cache layer; the tool bodies that expose it arrive in
   P9 (the plan's wiring point). The P4 tests drive `resolve_instance_profile`
   directly, which is exactly what the P9 body will call.

---

## 8. P5 — Host write primitives and the State Codec contract

**Status:** completed 2026-10-10. Suite: `py -3.11 -m pytest tests/plugins -q`
-> **180 passed, 0 failed** (164 before P5 + 16 new/migrated).
Targeted: `py -3.11 -m pytest tests/plugins/test_p5_transport.py -q` -> 16 passed.

**Created / extended:**

| File | Content |
|---|---|
| `src/reaper_reapy_mcp/plugins/host/reaper.py` (extended) | the write half of the bridge: the pure `splice_block_text` (and `split_lines_keeping_endings`), `write_params` (one batched, ident-addressed hop with read-back, `effects_verified` deliberately left `None`), `write_state_slice` (stale-slice `StateConflict` before splicing, one `SetTrackStateChunk`, declared settle/retry timing, transport read-back, `_locate_by_class` by class **and module**), `re_read_slice`; the connection attempt now also runs through the timeout chokepoint |
| `tests/plugins/conftest.py` (extended) | the **live-write gate**: an autouse fixture replacing the real bridge's write verbs with a loud refusal, plus a `live_write` marker that no test carries before P8. "No live mutation in this phase" is mechanical |
| `tests/plugins/test_p3_no_write.py` (migrated) | the P3 phase gate ("no write verb exists") is superseded by P5 and replaced by: no provider predicate/verify callback/tolerance/plugin vocabulary **in the code** (comments and docstrings are stripped before the scan, with a non-vacuity probe), no provider import, the full contract exposed, I/O-free import, the live-write guard proven by triggering it, and an AST check that no test binds a real bridge and calls a write verb on it |
| `tests/plugins/test_p5_transport.py` (new) | splice byte-fidelity (LF, CRLF, span at EOF, a form-feed/`\u2028`/NEL line before the block, sibling blocks byte-identical), one conformance suite over **two codecs** with different shapes and batching, store-nothing, drop-after-return, sequential-unit rebasing, the transport-attempt accounting |

**Critical tests (14 P5 G):**

| # | Test | Result |
|---|---|---|
| P5-1 | `test_p5_1_splice_changes_only_the_selected_span`, `test_p5_1b_crlf_chunk_keeps_its_line_endings`, `test_p5_1d_mock_transport_leaves_sibling_blocks_byte_identical`, `test_p5_1e…` (line-break space), `test_p5_1f…` (EOF span) | PASS — the prefix and suffix outside the span are byte-identical, the sibling/foreign blocks parse unchanged, and the line-breaking case is a regression test that **failed before its fix** (§10 finding 8) |
| P5-2 | `test_p5_2_codec_a_passes_the_shared_contract`, `test_p5_2_codec_b_passes_the_same_contract_with_different_batching`, `test_p5_2b_one_engine_suite_two_shapes_and_batchings` | PASS — one suite, two codecs, 1 unit vs 2 units for the same logical change, and the engine executes whatever ordered units a codec returns |
| P5-3 | `test_p5_3_store_nothing_is_write_not_applied_and_never_counted_as_landed`, `test_p5_transport_never_establishes_an_effect_itself` | PASS — `WriteNotApplied` with the mismatch evidence in the channel rows; a write whose read-back never happened is `unknown`; the bridge never claims an effect |
| P5-4 | `test_p5_4_sequential_units_rebase_on_the_read_back`, `test_p5_4b_a_stale_slice_is_refused_by_the_transport` | PASS — two creates land in two distinct new slots (a re-used plan-time slot would collide and fail verification), and a stale slice is refused before the setter |
| P5-5 | `test_live_write_gate_refuses_a_real_bridge_write`, `test_no_test_file_pairs_a_real_bridge_with_a_write_verb` | PASS — the real bridge's write verbs are refused inside the suite, and a second (AST) guard catches the same mistake statically |

**Acceptance criteria:** two codecs pass one conformance suite; splice
byte-fidelity is proven on multi-block chunks (LF, CRLF, EOF, exotic
line-breakers); snapshot/restore is complete and mock-verified; the bridge
contains no provider predicate or verification callback.
**Failure conditions:** none occurred — no live mutation was performed or
possible; no splice changed a byte outside the span (after fixing finding 8); the
bridge gained no semantics; the conformance suite is not tailored to one codec.

**Not live-certified (explicit):** the real bridge's write verbs
(`write_params`, `write_state_slice`) were **never executed against REAPER** in
this block. They are a tested abstraction: proven against the mock host and
against captured/reconstructed chunk strings, with the pure splice proven on the
live chunk's own line structure. Their behaviour against a real instance is a P8
measurement.

**Deviations / observations carried forward:**

1. The P3 phase gate was **migrated, not broken**: the old assertion is
   impossible by construction once P5 exists, and the file now asserts the
   properties that must hold instead (no semantics in the transport; no live
   write in the suite). This is recorded rather than silently rewritten.
2. `write_state_slice` re-reads the chunk for its attempt digest; the engine
   reads again for its own verification. Three chunk reads per state unit is the
   cost of keeping the transport's evidence and Core's verification independent.
3. A timed-out host call runs in a daemon thread and **cannot be cancelled**: a
   write that times out may still land. The engine accounts such a write as
   `write_effects_unknown` (B-54) instead of assuming it did not happen.

---

## 9. P6 — Modulation, dependencies and the tool body

**Status:** completed 2026-10-10. Suite: `py -3.11 -m pytest tests/plugins -q`
-> **197 passed, 0 failed** (180 before P6 + 17 new).
Targeted: `py -3.11 -m pytest tests/plugins/test_p6_modulation.py -q` -> 17 passed.

**Created / extended:**

| File | Content |
|---|---|
| `src/reaper_reapy_mcp/plugins/modulation.py` (new) | the modulation call: read-side listing (deterministic first match + the remaining matches as data), mutation-side identity (`require_single_route`: exactly one route or a typed refusal with the candidate list — B-48), route-table helpers, the depth hook call with the refusal preserved, create reporting the slots that **actually landed** (read from the read-back, never predicted), and `ModulationResult.to_envelope()` with an honest preflight outcome for refusals decided before a transaction exists |
| `tests/plugins/mock_provider.py` (extended) | codec A now verifies **only the fields a route op actually wrote** (an update of the amount says nothing about the source it kept), and the provider emits only the fields a request names. Both were defects the P6 tests exposed |
| `tests/plugins/test_p6_modulation.py` (new) | read vs mutation ambiguity, unknown/empty slot refusals, hard dependency with `write_calls_attempted == 0`, the projected-world fixture, depth acceptance/refusal/`force`, N routes ⇒ N ordered verified units, update/remove verification, `dry_run` default across the surface (B-49), envelope shape, no token in any payload |

**Critical tests (14 P6 G):**

| # | Test | Result |
|---|---|---|
| P6-1 | `test_p6_1_ambiguous_selector_refuses_a_mutation_with_zero_writes` | PASS — `InvalidRoute` with `reason: ambiguous_selector` and both candidate slots; zero writes; the route table unchanged |
| P6-2 | `test_p6_2_hard_dependency_refuses_a_route_mutation_with_zero_writes`, `test_p6_2b_projected_world_lets_a_correct_plan_through` | PASS — `DependencyViolation`, verdict `REFUSED_BEFORE_ANY_WRITE`, zero write calls; the raw (unprojected) judgement is shown to produce the finding, so the projection is what allows the correct plan |
| P6-3 | `test_p6_3_depth_refusal_is_a_first_class_answer_and_force_never_invents_a_factor`, `test_p6_3b_accepted_depth_reports_the_amount_law_and_evidence` | PASS — the declared non-affine refusal stands with `force: true`; an accepted depth reports the amount, law and evidence |
| P6-4 | `test_p6_4_three_route_change_is_three_ordered_verified_units`, `test_p6_4b_update_and_remove_land_and_are_verified` | PASS — three writes, three verified effects, three distinct new slots, and the reported `resolved_slot`s come from the read-back |
| P6-5 | `test_p6_5_every_mutating_op_applies_nothing_without_explicit_dry_run_false`, `test_b49_mutation_default_audit_over_the_registered_specs` | PASS — create/update/remove all perform zero writes without `dry_run: false`, and the spec audit covers every mutating tool |

**Acceptance criteria:** all [10 §4] tests green on mocks; the two-route model
exercises ordered-unit sequencing end to end; a mutation is never accepted with
unresolved dependency findings; an ambiguous mutating selector is refused.
**Failure conditions:** none occurred.

**Design notes (recorded):**

1. **The universal route table convention.** Core reads a codec's routes from
   `DocumentView["routes"]`, entries carrying a provider `slot` identity. The
   P2 engine's projection already relied on it; P6 makes it explicit, and the
   Vital codec (P7) exposes its 64-slot matrix in that form. A codec whose
   document has no route channel yields an empty table (no guessing).
2. Ownership: resolution of route *identity* is Core's (it is universal), while
   route *meaning* (vocabulary, depth fits, rules) stays provider data.
3. Pre-transaction refusals carry a preflight `Outcome`
   (`REFUSED_BEFORE_ANY_WRITE`, zero counters, the reason as a named gap) so a
   mutating call always answers with an outcome (06 §2) without inventing one.

---

## 10. P7 — Vital provider and codec isolation

**Status:** completed 2026-10-10. Suite: `py -3.11 -m pytest tests/plugins -q`
-> **226 passed, 0 failed**; after the audit follow-ups (§11) **236 passed**.
Targeted: `py -3.11 -m pytest tests/plugins/test_p7_vital.py -q` -> 26 passed,
`tests/plugins/test_p7_isolation.py` -> 3 passed, `tests/plugins/live` -> 1 passed.

**Created:**

| File | Content |
|---|---|
| `src/reaper_reapy_mcp/plugins/providers/__init__.py` | the provider-package rules (no host import, no legacy import, no import-time I/O) |
| `…/providers/vital/codec.py` | the ported host-free codec: `b64d`, `parse_block_framing`, `decode_document`, `encode_item_b`, `encode_item_a`, `wrap_lines`, `rebuild_block_text`, `dig`/`set_path`/`flat_paths`, `list_routings`, `find_free_slot`, `make_routing`, `clear_routing`, and `VitalCodec` implementing the `StateCodec` contract (one unit for path changes, one unit per route op, rooted-path enforcement, per-unit verification of the written fields only, snapshot/restore of the `settings` subtree) |
| `…/providers/vital/profile_build.py` | the registry loader (resolution order, absolute literal path, per-file schema checks, typed `IncompatibleSettings`, **no fallback**, nothing at import) and the profile builder (identity, modules, controls with laws/enums/units/ranges/evidence, the modulation model with the depth table, the dependency table, limits, provenance rows with real hashes) |
| `…/providers/vital/depth.py` | the ported depth algorithm (`display_number`, `display_unit`, `states_of`, `percent_controls`, `linear_fit`, `depth_fit`, `modulation_depth`, `depth_table`) |
| `…/providers/vital/dependencies_rules.py` | the ported rule table with its recorded evidence, `DISABLE_CANDIDATES`, the derived percentage set, the projected-world evaluator, and `rules_not_evaluated()` |
| `…/providers/vital/provider.py` | `VitalProvider`: `matches` (VST3 only, class id + module), `identify`, `version_gate`, `build_profile`, `normalize_request`, `convert_depth`, `dependencies`, `codec`, `write_order`, `verification_policy`, `capture_scope`, `limits`, `artifact_paths`, `implementation_revision` |
| `tests/plugins/fixtures/vital_registry/*.json` | pinned subsets of the real registry (13 controls covering affine/enum/none/unmeasured and the recorded depth destinations), each with source path + source sha256 in `_meta` |
| `tests/plugins/fixtures/vital_codec/*.json` | the **recorded measurements** of the live state chunk (item A bytes, the 184-byte item-B prefix, the 40-byte trailer, wrap width, the appended base64 artifact, both length fields, the recorded round-trip flags) + a document in the recorded shape |
| `tests/plugins/fixtures/build_vital_fixtures.py` | the manual refresh tool that regenerates those fixtures from `C:\REAPER` (not collected by pytest) |
| `tests/plugins/test_p7_vital.py` | byte-identity/grow/refusal codec tests, slice-exact targeting with a preceding foreign block, identity and version gates, profile validity + cacheability, registry-loader refusals, the recorded depth example and refusals, vocabulary/unbound-ident/unrooted-path refusals, the disable set, a two-route write through the engine with the Vital codec, and the declared contract constants |
| `tests/plugins/test_p7_isolation.py` | the recursive import boundary: fresh process, provider imported, no legacy module, no new top-level module, no registry read, cwd unchanged; the provider object reads nothing until first use; the L0 checker still passes with `providers/` exempt from the identifier ban |
| `tests/plugins/live/test_live_vital_read.py` | the **read-only** L3 read of the real instance through the provider + codec, writing per-run evidence |

**Critical tests (14 P7 G):**

| # | Test | Result |
|---|---|---|
| P7-1 | `test_importing_the_provider_pulls_in_no_legacy_module_and_no_registry_read`, `test_constructing_the_provider_still_reads_no_registry_until_first_use` | PASS with a recorded qualification (below): the provider package's own import adds no module outside `reaper_reapy_mcp`, pulls in no legacy module, reads no JSON and does not change the cwd |
| P7-2 | `test_codec_parse_rebuild_is_byte_identical_to_the_recorded_layout`, `test_codec_artifact_line_is_reappended_not_dropped`, `test_codec_grow_updates_both_recorded_length_fields` | PASS — a no-op rebuild is byte-identical, the appended artifact is identified and re-appended (with a non-vacuity counterpart), and a grown document updates **both** recorded length fields |
| P7-3 | `test_loader_refuses_a_missing_directory_naming_the_resolved_path`, `test_configured_directory_never_falls_back_to_the_default`, `test_empty_variable_value_is_a_configuration_error`, `test_expansion_characters_are_refused_rather_than_expanded` | PASS — every case is a typed `IncompatibleSettings` naming the variable and the resolved path, and the default is never reached |
| P7-4 | `test_provider_matches_only_the_measured_vst3_build` (+ the isolation tests: nothing is touched when no provider is used) | PASS — a VST2 build or an unexpected class id does not match (fail closed); the registry is read on first use only |
| P7-5 | `test_version_gate_refuses_an_unmeasured_version_without_an_override`, `test_v5_unbound_parameter_ident_refuses_a_control_write_with_a_named_reason` | PASS — `VersionMismatch` with `override: None`; an unbound parameter ident is a typed refusal, not a silent no-op |
| P7-6 | `test_v11_multi_block_targeting_keeps_every_other_block_byte_identical`, `test_v3_one_route_per_state_write_and_verification_from_the_read_back` | PASS — a preceding foreign block is byte-identical; two routes are two verified units and the read-back shows both |

**Acceptance criteria:** V-1…V-12 green on recorded artifacts (V-11/V-12
included); the live read matches the recorded identity; the host-touching
functions (`_get_chunk`, `_set_chunk`, `_default_ctrl`) are **not** ported
(`export_document` is not ported either, and the reason is recorded); no legacy
module is imported anywhere in the package; the registry directory's digest
appears in the profile provenance.

**Live read-only evidence** (`tests/plugins/live/evidence/p7_vital_read__*.json`,
one file per run): the open instance resolves through the provider as
`VST3i: Vital (Vital Audio)` / `Vital.vst3` / class `56535456…` /
`param_count 2858`; the located block spans 837 lines; the decoded document has
64 route slots and `synth_version 1.5.5` (inside the measured set); the JSON sits
at the recorded offset 184; and **a no-op rebuild of the live block is
byte-identical**. That last fact is the strongest available evidence for the
codec's framing port, because it uses real block bytes rather than the
reconstructed fixture.

**Deviations / observations carried forward (these are the honest gaps):**

1. **P7-1's literal wording** ("a clean process importing the provider pulls in
   no `reapy`/`ReaperController`…") is not attainable for a module *inside*
   `reaper_reapy_mcp`: importing any submodule executes the package
   `__init__`, which imports the pre-existing controller chain (and therefore
   `reapy` and a `ReaperController`) by design — the same qualification recorded
   for P0-1. What is measured is the provider package's own contribution, and
   the test's docstring says exactly that.
2. **The live plugin version is not a host fact.** `identify` carries whatever
   the host reports (currently nothing), the document's `synth_version` is
   readable (recorded live as 1.5.5) but Core has no hook that hands a decoded
   document to the provider before a write. The version gate is therefore
   implemented and tested as a pure check, and wiring the document version into
   the preflight is an **open item** for P8 (it needs a provider hook Core calls
   with the decoded document, or a Core change).
3. **Three of the five rule rows are declared but not evaluated.** The projected
   world carries the plan's changed paths/controls and route counts — not the
   route specs and not the untouched live flags the POC predicates read
   (`fm_needs_modulator`, `route_into_off_filter`,
   `wave_frame_needs_multiframe_wavetable`). They are in the profile, marked
   `evaluated: false`, and named by `rules_not_evaluated()` with the input each
   would need; firing them from signals that do not exist would be a guess, and
   a guess here refuses correct patches. Live-flag evaluation is an **open item**
   (Core change, reopens [05]/[08]).
4. **The document version and the parameter ident are the two P8 measurements**
   that would move the Vital provider from "read-and-routes certified on mocks
   and fixtures" to "live-certified". Nothing in this block claims more.
5. `profile_build.VITAL_MODULE` is the **exact recorded header spelling**
   (`Vital.vst3`), because the slice correlation compares it against the host's
   block header; `matches` compares case-insensitively. The earlier POC-derived
   label `vital.vst3` was wrong for correlation and was corrected here (the first
   P7 test run caught it).
6. The pinned codec fixture is **reconstructed**: the full chunk bytes were never
   archived (only their measurements plus item A, the 112-byte prefix head and the
   trailer), so the fixture asserts every recorded measurement and the live read
   supplies the real-bytes check (see above). The POC's own recorded
   `b64_identical: false` is explained by the appended artifact, which the codec
   now identifies from item A's recorded length field.

---

## 11. Independent audit of P4–P7 and its dispositions

An independent subagent (read-only, no file edits) audited `cache.py`,
`resolve.py`, the bridge's write half, `modulation.py`, the Vital codec and
profile loader, and the new test files against [09], [10] and the P4–P7 plan
sections, using static reading plus ten executable probes. Every substantive
finding was re-checked here before acting; the dispositions below say what
changed and what did not.

| # | Finding (severity) | Disposition |
|---|---|---|
| 1 | a **failed** quarantine rename was only a warning, and the next `save()` published over the un-quarantined file (medium) | **FIXED** — `save()` now refuses to publish when the occupied bytes could not be moved aside (`cache.occupied`), so the foreign bytes survive. Regression: `test_a_failed_quarantine_rename_never_leads_to_publishing_over_the_bytes`, verified to **fail** against the pre-fix behaviour |
| 2 | a profile whose validation says "not cacheable" could still be **served** on load (medium) | **FIXED** — the load gate is now `rebuild_required or not cacheable`, matching 09 §6, and the file is quarantined. Regression: `test_a_non_cacheable_entry_is_neither_written_nor_served` |
| 3 | the entry was trusted because its own `cache_key` matched; the profile's provenance was never re-derived (low) | **FIXED** — the loader re-computes the key from the entry's own provenance plus the live identity and quarantines a mismatch (`cache.key_material`); an entry without a provider id is refused too. Regressions: `test_an_entry_whose_provenance_does_not_derive_its_key_is_not_served`, `test_an_entry_without_a_provider_id_is_not_served` |
| 4 | a 48-bit file-name prefix could quarantine another key's valid entry (low) | **FIXED** — the prefix is now 24 hex characters (96 bits) |
| 5 | `json.dumps` sat outside the `OSError` guard, so a cache write could raise into a tool call (low) | **FIXED** — serialization is guarded; the refusal is `cache.not_serializable`. Regression: `test_an_unserializable_profile_cannot_raise_out_of_save` |
| 6 | the idempotent `save()` left `source: none` (nit) | **FIXED** — the branch reports `cache` |
| 7 | `_present` conflated "absent" with "empty" (nit) | **ACKNOWLEDGED, not changed** — both cases are non-cacheable by validation, so the conflation cannot admit an entry; tightening it would change every key for no safety gain |
| 8 | `splice_block_text` split with `str.splitlines` while the locator numbers lines with `split("\n")`: a form feed/NEL/LS/PS earlier in the chunk shifted the span and **destroyed** the block (high) | **FIXED** — the splice now splits on `"\n"` only (`split_lines_keeping_endings`). Regression: `test_p5_1e_a_line_break_that_only_str_splitlines_sees_cannot_shift_the_span`, verified to **fail** against the pre-fix behaviour |
| 9 | the write's read-back was re-located by class only, while the ordinal was computed among class **and module** matches: a sibling's digest could be reported, or a landed write refused (medium) | **FIXED** — relocation now matches class + module (the module comes from the slice's own header). Regression: `test_p5_1g_the_read_back_relocation_matches_class_and_module` |
| 10 | a timed-out host call cannot be cancelled, so a "wedged" write may still land (medium) | **ACKNOWLEDGED, documented** — cancellation is impossible through this host API; the engine already accounts such a write as `write_effects_unknown` (B-54) instead of assuming it did not land. Recorded in the P5 notes |
| 11 | the connection attempt bypassed the timeout chokepoint (low) | **FIXED** — `ensure()` now runs the connect through `call()` |
| 12 | a span at the end of the chunk gained a line ending that was not there (low) | **FIXED** — the ending comes from the replaced region only. Regression: `test_p5_1f_a_span_at_the_end_of_the_chunk_gains_no_line_ending` |
| 13 | the B-55 test could not see a copy-and-delete quarantine (test gap) | **FIXED** — a spy now asserts `os.rename` is used exactly once for the quarantine |
| 14 | the two-checks test is genuinely non-vacuous | **NO ACTION** (recorded) |
| 15 | the splice test's "independent expectation" mirrored the implementation (test gap) | **PARTLY FIXED** — the expectation stays as a second construction of the spec, and the new *`\x0c`/`\u2028`/NEL* and EOF tests (findings 8 and 12) cover what the mirror could not |
| 16 | the byte-identity test proves identity of a block the test itself synthesises (fixture limitation) | **ACKNOWLEDGED** — the recorded chunk bytes were never archived; the test now also documents that, and the **live** read (real 837-line block, byte-identical rebuild) supplies the real-bytes evidence |
| 17 | the Vital end-to-end test was vacuous about "verification from the read-back" | **FIXED** — the P7 suite now injects `store-nothing` on the Vital state write and requires the transaction to fail, so a verification that trusted the sent bytes would be caught |
| 18 | the isolation claim vs. the pre-existing package `__init__` chain (medium) | **ACKNOWLEDGED and re-worded** — the test measures the provider package's own contribution and says so; the literal wording needs the (out-of-scope) lazy `__init__` (P0-1, §6) |
| 19 | no host/legacy import, no import-time I/O, `export_document` absent | **NO ACTION** (confirmed by the audit) |

Suite after all follow-ups: `py -3.11 -m pytest tests/plugins -q` ->
**236 passed, 0 failed** (29 of them in `test_p4_cache.py`, 16 in
`test_p5_transport.py`); regression `tests/test_midi_mock.py` -> 2 passed; live
read suite -> 1 passed.

---

## 12. P4–P7 gate summary

| Gate (plan) | Result | Evidence | Residual risk |
|---|---|---|---|
| P4-1 foreign file never destroyed | PASS | byte-compare + `os.rename` spy + the occupied-path refusal | a rename denied by another process is now loud and non-destructive |
| P4-2 no partial target | PASS | stray-temp and failed-replace tests | none identified |
| P4-3 cache hit, zero parameter reads | PASS | mock-host counter assertion | none identified |
| P4-4 every determinant in the key | PASS | 7 distinct keys; stale profile unreachable | a key-material change needs a `provider_revision` bump by convention (§[07 §3.4]) |
| P4-5 fail-closed identity | PASS | no entry written; `SnapshotUnavailable` naming the field | none identified |
| P5-1 splice byte fidelity | PASS | LF/CRLF/EOF/line-breaker tests; sibling blocks byte-identical | not yet exercised against a live write (P8) |
| P5-2 two codecs, one suite | PASS | codec A/B conformance + batching difference | none identified |
| P5-3 "API success" is never an effect | PASS | store-nothing ⇒ `WriteNotApplied` + mismatch rows | none identified |
| P5-4 sequential rebase | PASS | distinct landed slots; stale slice refused | none identified |
| P5-5 no live write | PASS | conftest gate + AST check + the live-write refusal test | none identified |
| P6-1 ambiguous mutation refused | PASS | `InvalidRoute` + candidates + zero writes | none identified |
| P6-2 hard dependency, zero writes | PASS | `DependencyViolation`, `REFUSED_BEFORE_ANY_WRITE` | provider-side: 3 rule rows are declared but not evaluated (§10.3) |
| P6-3 depth refusal is an answer | PASS | `NoConversion` with the reason; `force` never converts | none identified |
| P6-4 N routes ⇒ N verified units | PASS | 3 writes, 3 verified effects, slots from the read-back | none identified |
| P6-5 dry-run default | PASS | all mutating ops + the spec audit | none identified |
| P7-1 provider import isolation | PASS with the §10.1 qualification | fresh-process measurement | the package `__init__` chain remains the pre-existing behaviour |
| P7-2 codec port validated | PASS | fixture byte-identity + grow + **live** no-op rebuild | the archived original chunk bytes do not exist |
| P7-3 registry errors, no fallback | PASS | typed `IncompatibleSettings` for missing/empty/relocated/broken paths | none identified |
| P7-4 unsupported provider | PASS | `matches` fails closed for VST2/other class ids; nothing is read when the provider is unused | the `UnsupportedProvider` path at the tool layer lands in P9 |
| P7-5 version/ident fail closed | PASS | `VersionMismatch` with no override; unbound ident refused | the live document version is not yet wired into a preflight (§10.2) |
| P7-6 slice-exact targeting | PASS | foreign-first multi-block test; live read | none identified |
| P7-7 import boundary | PASS | 3 isolation tests | literal wording per §10.1 |

## 13. What is not proven (carried into P8)

1. **No live mutation was performed** in this block: the bridge's write verbs,
   the Vital codec's encode path and every transaction were exercised against the
   mock host and fixtures only. `write_state_slice` on a real instance is
   unmeasured.
2. **The parameter ident binding** is host-provided and unbound; a Vital control
   write is refused with a named reason rather than silently ignored. P8
   measures the ident table.
3. **The live document version** (`synth_version`) is readable but not wired
   into the write preflight.
4. **Three Vital rule rows** (FM/RM, off-filter routes, wave-frame inertness)
   are declared and not evaluated; the projected world does not carry their
   inputs.
5. **The FX-GUID repopulation measurement (L-3)** remains unexecuted (a
   mutation, gated to P8): the identity of a replaced identical-build instance is
   not yet established by measurement.
6. **The archived chunk bytes** for the Vital fixture do not exist; the fixture
   is reconstructed from recorded measurements and validated against the live
   block.
7. **Rollback of a live parameter reset** (the POC's documented
   document-write-resets-parameters behaviour) is proven only on the mock host's
   reset fixture, not against REAPER.
8. The **cache tamper model** is "user-local data": the content hash and the key
   re-derivation detect corruption and misfiling, not a deliberate re-sealing by
   someone with write access to the cache directory.

## 14. Git state at the end of this block

- Branch `feature/vst-bridge`, HEAD `c060def` — **no commit was created** (none
  was authorised).
- The P0–P3 work remains **staged exactly as it was** (35 files).
- This block's changes are **unstaged modifications** of 5 staged files
  (`host/reaper.py`, `resolve.py`, `test_p3_no_write.py`, `conftest.py`,
  `mock_provider.py`) plus **28 untracked files** (the new modules, tests and
  fixtures listed above, and four per-run live-read evidence files).
- `composer_v1_2_lm_probe.md` (the user's untracked probe report) was not
  touched, and no destructive git operation was performed.

---

## 15. P8 — Live mutation validation against the Vital instance (block 3/3)

Live work was done **in the project that was already open** in the running
REAPER (one empty track, one Vital instance, fx index 0). The project was not
copied, closed, reopened, or restarted, and the addressed instance was never
removed or replaced. Before the first write a **baseline artifact** and a
**recovery artifact** were written (see 15.1). All write probes end in a
verified restore, and the restore is asserted before anything else is asserted.

### 15.1 Baseline and recovery set (P8.1)

- `tests/plugins/live/evidence/p8_baseline__20261010T195509Z__7eb5e9aa1a20.json`
- `tests/plugins/live/recovery/p8_recovery__20261010T195509Z__673f78d75fa6.json`
  (a **write-free** artifact: it is the set the restore is proved against)

Measured content of the baseline:

| Fact | Value |
| --- | --- |
| instance identity | `VST3i: Vital (Vital Audio)` / `Vital.vst3` / class `56535456697461766974616C00000000` |
| parameters | 2858, read back one batched hop (`inside_reaper` HOLD) |
| document slice | class-id tail + JSON: lines 29–865 (837 lines), 234 741 chars |
| settings leaves | 1072 writable + 1 plugin-owned (`settings.sample.samples`) |
| binding | 775/775 indexed controls bound, `refused_reasons {}`, 1.4544 s |
| block / vector digests | `00e83324…f3bbb` / `8a29383b…a7d6` |
| measured cost | idents 2.8855 s, values 0.3099 s, state read 0.4304 s, decode 0.0022 s |
| identity sample | `filter_1_cutoff` → index 104, live ident `104:48629`, "Filter 1 Cutoff", affine, 8–136 semitones, normalized 0.40625 |

Two host verbs were added for this and are **read-only and additive**:
`param_names(ref, indices)` and the pure `field_string(raw, index)`.

### 15.2 Version gate and live-verified identity binding (P8.2)

- The provider's `document_version()` hook reads `synth_version` from the
  decoded document; live value **1.5.5** lies inside the measured set, and a
  value outside it produces a refusal with a named `version_refusal`.
- `missing_document_version` is a **refusal**, not a pass, when a provider
  declares the hook and the document has no version.
- Binding copies the declared profile and replaces every ident with the **live**
  one (775/775, `ident_refreshed 0`, `declared_ident_kept 0`), so an ident can
  never be carried over from a declaration the host did not confirm.
- The Core stays free of the word "vital" (L0 architecture check still passes);
  all live naming lives in `plugins/providers/vital/`.

### 15.3 Live parameter write (P8.3)

Evidence: `p8_3_parameter_write__20261010T200037Z__8e1493d01ea9.json`
(plus the repeat runs `…T201130Z__daa959f878d4` and `…T204405Z__205dae201cea`).

| Step | Result |
| --- | --- |
| target | `filter_1_cutoff`, ident `104:48629`, 61.0 semitones (normalized 0.4140625) |
| transaction | `committed`, `ALL_VERIFIED`, 1 write call, 0 unknown |
| independent read-back | `agrees: true`, observed 0.4140625 (a **separate** read after the write) |
| restore | 2858/2858 parameters equal at tolerance 0.0, 1072 leaves, chunk digest returned to the run's baseline |

### 15.4 Live state-slice write (P8.4)

Evidence: `p8_4_state_write__20261010T202812Z__2580c9f0d484.json`
(and `…T204445Z__03d35005c60b` from the final sweep).

| Step | Result |
| --- | --- |
| case | `lfo_1` → `filter_1_cutoff`, depth 8 ⇒ amount 0.0625, slot 1 created |
| transaction | `committed`, 4 state rows verified, 1 write call, 0 unknown, 24 host reads |
| outside-span bytes | intact (span 837 → 838 lines: exactly the one added route line) |
| side effect measured | the document write moved **3 parameters** of 2858 (compared at tolerance 0.0) |
| restore | 1071 writable leaves `all_equal`, 2858/2858 parameters equal, `active_routes []`, chunk chars/lines/span identical to baseline |

The measured side effect is the reason the acceptance check compares the whole
parameter vector rather than only the controls the request named.

### 15.5 L-3 (identity across replacement) — measured, limitation stands (P8.5)

Evidence: `p8_l3_identity__20261010T203101Z__dcd339e9c219.json`

| Question | Measurement |
| --- | --- |
| does a second instance of the same build get a different handle? | yes (`cb3747e3…` vs `1b98434c…`) |
| does the handle change when the instance is removed and re-added? | **no** (`handle_changed: false`, identity facts identical) |
| does the target token then refuse? | **no** (`refused: false`, no differing fields) |
| was the addressed instance touched? | no — original block digest `00e83324…` and vector digest `8a29383b…` unchanged, fx count restored |
| extra fact | `TrackFX_AddByName(..., instantiate=-1)` adds; `0` / `1` only find |

So an **identical-build replacement stays indistinguishable**, and the layer
keeps failing closed by refusing to claim it can tell. `TargetToken.differences_against`
was strengthened to also compare `facts_snapshot["session_handle"]` (recorded
present → fresh absent yields `session_handle_unreadable`); the docstring states
that this does **not** close L-3.

### 15.6 Live vs mock semantics (P8.6)

The simulator and the live host were compared on the same scenarios:

| Scenario | Live | Mock/static |
| --- | --- | --- |
| write not applied by the host | genuine `WriteNotApplied` (`settings.sample.samples`, before the derived-leaf fix) | injection reproduces the same verdict and counters |
| write applied, read-back disagrees | genuine `FAILED_AND_NOT_RESTORED` + real rollback (same incident) | injection reproduces rollback ordering |
| host wedged / no answer | a 30 s sample-timeout classified `Wedged` (first baseline probe, before the HOLD fix) | injection covers the timeout path |
| counter invariant | the identity held in **every** live outcome (see the note below on where it is enforced) | asserted at 34 call sites |

**Where the counter invariant is actually enforced (corrected after the audit).**
`write_calls_attempted == write_effects_verified + write_effects_unknown` is
**maintained by construction** in `_issue_write` (which increments attempted and
unknown together) and `_establish_effect` (the only path that moves one from
unknown to verified); it is **asserted by the suites** (`assert_counters_consistent`,
34 call sites in `test_p2`/`test_p5`); and it was observed to hold in every live
outcome. What production code does **not** do is evaluate the equality itself -
an earlier draft of this journal implied otherwise, and an independent read-only
audit caught the over-statement.

### 15.7 P8.7 — disposition of the Core open questions (owner decision)

- **Implemented (additive, mandated by `07` §3.2):** the optional
  `document_version` provider hook and its refusal path.
- **Not implemented, needs the owner:** the three live-flag dependency rows
  (`state.derived_leaves`, `limits` text, and the flagged dependency rules)
  remain **declared but not evaluated** in the live path. Enabling the
  dependency rules changes refusal behaviour, so it is presented as a decision
  rather than done silently. Two options are offered in §20.

### 15.8 Defects that only live measurement could find (all fixed and covered)

| # | Defect | Why the mocks missed it | Fix |
| --- | --- | --- | --- |
| 1 | `_track_name` returned the return code `"1"` instead of the name | mock tuples had the name in position 0 | measured `field_string` positions (name is raw[2]) |
| 2 | `split_lines_keeping_endings` dropped the trailing newline (234 740 vs 234 741 chars) | the mock chunk had no multi-line/ending shape to lose | keep the trailing empty element across the splice |
| 3 | codec locator: the `{"author` marker is destroyed by the pad rewrite, and the "first brace" rule failed on a fixture with `0x7b` in the binary prefix | the fixtures had exactly one, unpadded document | ascending candidate scan that requires a `settings` object |
| 4 | pad inserted after the closing brace, corrupting the pad line | the mock tolerated any padding | pad inserted **before** `}` |
| 5 | restore demanded the plugin-owned `settings.sample.samples` (88 200 bytes; the plugin re-encodes it, observed drift 8 → 16 → 24 → 40 leading zero bytes per write cycle) | the mock document had no plugin-owned leaf | `state.derived_leaves`: refused as a write target (`derived_leaf_not_writable`), excluded from restore with the named gap `derived_leaf_not_restored` |
| 6 | FastMCP modelled `**kwargs` as one **required** field named `kwargs` | the tool bodies were called directly in tests | explicit `__signature__` (keyword-only, all optional) |
| 7 | omitted optional arguments arrive as `None`, so SPEC defaults (e.g. `dry_run`) were lost | direct calls passed the defaults | wrapper applies declared defaults on `None`, keeps supplied `False`/`0` |
| 8 | harness: positional ident read-back; `restore_changes` treated as a diff (it returns all leaves); outside-span check used stale line indices; P8.4 asserted before restoring | — | span located per chunk; restore in `finally`; assertions last |

---

## 16. P9 — Integration into the existing MCP server

### 16.1 Tool surface (7 tools, all registered in `create_server()`)

`plugin_instances`, `plugin_describe`, `plugin_find`, `plugin_read`,
`plugin_write`, `plugin_state`, `plugin_modulation` — one line in
`create_server()` plus `register_production_bodies()`; no new server, no
transport change, no change to the pre-existing tools' schemas.

### 16.2 What was verified through the real FastMCP client

`tests/plugins/test_p9_tools.py` (17 tests) drives `mcp.list_tools()` /
`mcp.call_tool()` over an in-process FastMCP instance, not the bodies:

- all 7 tools appear with **all parameters optional** and no `kwargs` field;
- a call with **no arguments at all** is accepted (the SPEC defaults apply,
  `dry_run` stays true);
- an explicit `dry_run=false` reaches the body and a dry run never reaches the
  host as a write (`write_calls_attempted 0`);
- the P3 "no write without a transaction" guard and the version gate cannot be
  bypassed through the tool surface;
- with `REAPER_MCP_PLUGIN_PROVIDERS` unset there are **zero** providers and a
  typed `UnsupportedProvider` is returned with the host contact **bombed out**;
- the pre-existing host tools keep byte-identical schemas and the tool list
  stays additive.

### 16.3 Counts

| Suite | Result |
| --- | --- |
| `tests/plugins/test_p9_tools.py` | 17 passed |
| `tests/plugins` (offline, `live/` excluded) | **277 passed** |
| `tests/test_midi_mock.py` (repo regression) | 2 passed |

---

## 17. P10 — End-to-end acceptance through the tool surface

### 17.1 Scenario

`tests/plugins/live/test_p10_acceptance.py` drives the **registered tools** end
to end against the live Vital instance:
`plugin_instances` → `plugin_describe` → `plugin_find` → `plugin_read` →
`plugin_write` (default dry run) → `plugin_write` (committed: a parameter **and**
one modulation route) → `plugin_state` (settings + routes) →
`plugin_modulation` (list) → restore the whole channel, verify, then assert.

Evidence: `tests/plugins/live/evidence/p10_acceptance__20261010T204137Z__c2fb13177e18.json`
(repeat run in the final sweep: `…T204331Z__a9d4b827827a.json`, identical shape).

### 17.2 Measured steps

| Step | Verdict / result |
| --- | --- |
| `plugin_instances` | 1 instance, target token in `meta` only |
| `plugin_describe` | 775 controls (775 indexed, 294 enums, 193 without a law), `state.derived_leaves` declared |
| `plugin_find` | 13 matches for "cutoff" |
| `plugin_read` | `filter_1_cutoff` display 60.0 / native 60.0 / normalized 0.40625 semitones |
| `plugin_write` (dry run) | `NO_WRITES`, **0** write calls, 13 host reads |
| `plugin_write` (committed) | `ALL_VERIFIED`, 2 write calls, 2 verified, 0 unknown, 43 host reads |
| `plugin_state` (inspect) | 781 settings keys, 64 routes, slice class `56535456697461766974616C00000000` |
| `plugin_modulation` (list) | 64 slots |
| `plugin_write` (restore) | `ALL_VERIFIED`, 1 write call, `active_routes []`, 24 host reads |

Post-write checks: outside-span bytes intact (873 vs 872 lines = the added route
line), route present (`lfo_1` → `filter_1_cutoff`, amount 0.0625, slot 1),
independent parameter read-back equal to the expected normalized value, and
**3 parameters moved** by the document write (measured).

Restore checks: 1071 writable leaves `all_equal`, 2858/2858 parameters equal at
tolerance 0.0, `active_routes []`, chunk chars 234 741 = baseline, 872 lines =
baseline, span 837 lines = baseline, outside-span intact, `verified: true`.

### 17.3 Acceptance criteria

| # | Criterion | Status |
| --- | --- | --- |
| 1 | the post-write document is validated by independent read-back | **PASS** (parameter and state channels, separate reads) |
| 2 | the restore is verified, not asserted | **PASS** (all leaves + whole parameter vector + routes + bytes outside the span) |
| 3 | byte-exact restore of everything outside the target span | **PASS** (identical chars, lines, span length, prefix and suffix) |
| 4 | the write effect matches the request | **PASS** (expected normalized value reached independently) |
| 5 | no unverified write remains | **PASS** (`attempted == verified + unknown`, unknown 0 in every run; the identity is maintained by construction and asserted by the suites, not evaluated in production - see §15.6) |
| 6 | failures are classified, not swallowed | **PASS** (live `WriteNotApplied` and `FAILED_AND_NOT_RESTORED` were observed and named) |
| 7 | the tool surface is the only entry point | **PASS** (the acceptance scenario uses only the 7 tools) |
| 8 | the pre-existing server keeps working | **PASS** (tool list additive, earlier tools' schemas unchanged, repo regression green) |

### 17.4 Commands and counts (static / mock / live kept separate)

| Command | Kind | Result |
| --- | --- | --- |
| `py -3.11 -m pytest tests/plugins -q --ignore=tests/plugins/live` | static + mock, **plus one conditionally-live read** | **294 passed** (11.55 s) |
| `py -3.11 -m pytest tests/plugins/live -q` | live read + live write | **4 passed** (148.64 s) |
| `py -3.11 -m pytest tests/test_midi_mock.py -q` | mock regression | **2 passed** |
| `py -3.11 -m pytest tests/plugins/live/test_p10_acceptance.py -q -s` | live end-to-end | **1 passed** (73.95 s) |

The live suite is the four tests that need REAPER: the P7 identification read,
the P8.3 parameter write, the P8.4 state write, and the P10 acceptance run.
Every offline test uses the mock host / mock provider; no live test is counted
as evidence for a claim about the mock path, or vice versa - **with one honest
exception the audit found**: `test_p3_conformance.py` is collected in the offline
set but constructs a real read bridge and reads the running host (it skips with a
named reason when no host is connected). It is a **live read**, it issues no
write, and it is the only one of the 294 that leaves the process.

### 17.5 Limitations carried forward

1. **L-3 stands** (identical-build replacement is indistinguishable; the layer
   fails closed instead of guessing) — see 15.5.
2. **The plugin-owned sample blob is not restored byte-exactly.** The plugin
   re-encodes it on every write cycle, so the whole-chunk digest is not a stable
   cross-run identity; it is excluded from restore and reported as the named gap
   `derived_leaf_not_restored`. Everything else returned to baseline, including
   the mutated-parameter set the plugin recalculated.
3. **One live write cannot be made to fail on demand**, so the `WriteNotApplied`,
   rollback, timeout and wedge paths are exercised by injection, and the two
   that occurred live are reported as observations rather than as coverage.
4. The document write moves parameters beyond those named in the request (3 of
   2858 measured); the acceptance check compares the full vector because of it.
5. **`force` is accepted, echoed and inert** (`plugin_modulation`), while
   `errors.py` describes a rule that "rejects the route unless forced". No
   provider or dependency rule reads it. Documentation conflict only - no safety
   consequence - and changing its semantics means changing refusal behaviour, so
   it is left as an owner question rather than changed silently.
6. **A `derived_leaf_not_restored` gap is produced on the rollback path.** A
   *successful* restore reports the exclusion as `derived_excluded` (the live
   artifacts `p8_4`/`p10` do exactly that), not as a named gap. Both are honest;
   if a uniform gap is wanted on every path, that is a small change - asked in §20.
7. **`_poison`-style guard coverage:** the source-level check that no test pairs a
   real bridge with a write verb was itself unprobed until the audit; it now has a
   negative probe (see §18, F3).

### 17.6 Closing verification: the live state against the **earliest** baseline

After every test run of the block, the live instance was compared once more - in
**dry-run mode, nothing written** - against the *first* artifact captured before
the first write (`p8_recovery__20261010T195509Z__673f78d75fa6.json`):

| Channel | Result |
| --- | --- |
| parameters | **2858 / 2858 equal** at tolerance 0.0, 0 mismatched, 0 missing, 0 extra |
| routes | `[]` - the baseline had no active route |
| document leaves | 1072 compared, **exactly 1 differing**: `settings.sample.samples` (the plugin-owned blob, 117 600 captured chars) |
| whole-chunk / block digest | differ, as a consequence of the blob above |

That single difference is the residual of §17.5 item 2 and nothing else: every
writable leaf, every parameter and the route table returned to the state captured
before the first write of this block. Artifact:
`p8_restore__20261010T211254Z__e637d9e8fe42.json` (`result: "dry run: nothing written"`).

*Note on the legacy checker:* this older script does not ask the provider which
leaves are derived, so it reports the plugin's own blob as a mismatch
(`declared_derived: []`). The transaction path does ask, which is why the same
leaf appears there as `derived_excluded` / `derived_leaf_not_restored` instead of
a silent difference.

---

## 18. Independent audit

**What ran, honestly labelled.** A separate agent with a **fresh context**, a
read-only brief and an adversarial task (try to falsify 11 claims) was run over
the working tree, the code, the tests and the evidence artifacts. It was **not**
possible to select or confirm the GLM model the brief named: the subagent
self-reports the same model string as this session, so the run is independent in
**process** (different context, read-only, adversarial, must produce file:line
proof) but **model-level independence is NOT established**. That limitation is
recorded rather than glossed.

**What it verified** (each with file:line or command output, re-checked here):

| Claim | Verdict |
| --- | --- |
| no provider brand name outside `providers/` (L0), enforced by a checker with negative probes | PASS |
| `host/reaper.py` is the only `reapy` importer inside `plugins/`, lazily | PASS |
| `dry_run` defaults true; a dry run calls no host write verb (literal call count) | PASS |
| the counter identity is asserted at 34 test sites | PASS (production conjunct over-stated - see §15.6) |
| read-back is independent of the write verb's return value; failure rolls back | PASS |
| derived leaves refused (`derived_leaf_not_writable`) and excluded (`derived_leaf_not_restored`) | PASS |
| exactly 7 bodies, additive wiring, no direct-bridge bypass | PASS |
| the 4 cited evidence artifacts exist and contain the cited numbers | PASS |
| no commit contains this work | PASS |

**What it found, and what was done with each finding:**

| # | Finding | Severity | Disposition |
| --- | --- | --- | --- |
| F1 | a **dry run swallowed a codec refusal** (`except PluginError: return []`), answering `ok: true, would_write: false` for a request the real run refuses | medium | **FIXED** - the preview now builds through the same `_build_units` path, so the refusal propagates; `_potential_units` deleted; two new tests (refusal + non-vacuous preview); the fix was **mutation-checked** by reinstating the old behaviour and showing the new test would fail on it |
| F2 | `_require_bound_parameters` (defence in depth) had **no test anywhere** | low | **FIXED** - a lax-provider test now drives it to `REFUSED_BEFORE_ANY_WRITE` with zero writes |
| F3 | the source-level bridge/write guard had **no negative probe** | low | **FIXED** - the scan is split into `bridge_write_offenders` and probed with a synthetic offender and a synthetic clean file |
| F4 | `bool(dry_run)` accepted a non-boolean, and `dry_run: "false"` (a truthy string) **opted a caller into applying** | low | **FIXED** - `values.strict_bool` at the engine and tool boundaries; 7 engine-level + 6 tool-surface parametrised cases |
| F5 | `force` is inert while `errors.py` describes it as a permit | informational | recorded as a residual (§17.5/§20), not changed silently |
| F6 | the `derived_leaf_not_restored` gap is produced on the rollback path only | informational | recorded (§17.5), owner question |
| F7 | `test_p3_conformance.py` is a conditionally-live read inside the "offline" collection | informational | recorded in §17.4 rather than hidden |

**After the fixes:** offline **294 passed** (was 277), live **4 passed**, repo
regression **2 passed**, and the acceptance scenario was re-run post-fix
(`p10_acceptance__20261010T210638Z__da780fdc98af.json`) with the same results:
dry run `NO_WRITES` / 0 writes, committed `ALL_VERIFIED` (2/2), restore
`ALL_VERIFIED`, 2858/2858 parameters equal, 1071 writable leaves equal,
`active_routes []`, outside-span bytes intact.

---

## 19. Git state at the end of this block

- Branch `feature/vst-bridge`, HEAD `c060def` — **no commit was created**
  (none was authorised for this block either).
- The P0–P3 work is still staged exactly as before; this block's changes are
  unstaged modifications plus new untracked files (the host/binding/transaction/
  api/registry modules, the provider changes, the tests and the live harness and
  evidence files).
- `composer_v1_2_lm_probe.md` was not touched. No destructive git operation was
  run.

---

## 20. Open questions for the owner at the end of block 3/3

1. **P8.7 dependency rules (needs a decision, deliberately not implemented):**
   should the live flag dependency rows be evaluated against the decoded
   document (which changes when the layer may refuse a write), or stay declared
   but not evaluated?
2. **The derived-leaf gap:** accept `derived_leaf_not_restored` as a documented
   residual, or require a restore path for plugin-owned leaves? (The current
   answer is "excluded and named", because writing the captured value is
   measurably overwritten by the plugin.)
3. **L-3:** accept fail-closed with the limitation recorded, or add a
   user-visible confirmation step for replacement-sensitive operations?
4. **Commit:** the block is complete but uncommitted; a commit needs the
   owner's go-ahead (branch `feature/vst-bridge`, HEAD `c060def`).
5. **`force` on `plugin_modulation`:** make it real, remove it, or keep it as an
   echo? It is currently accepted, echoed and read by nothing, while one docstring
   describes it as a refusal override (§17.5 item 5).
6. **The derived-leaf gap on a successful restore:** report it as a named gap on
   every path (currently the rollback path names it; a successful restore reports
   `derived_excluded`) - §17.5 item 6.

---

## 21. Release-review remediation (2026-10-10, after the owner's five decisions)

Scope: the five decisions in the owner's review brief
([19 §8](../docs/universal-plugin-mcp-v3/19-owner-decisions.md)) and their
mandatory fixes. **No new feature was added, no new dependency-rule algorithm was
written, and no live mutation was run** (see 21.6 for why that is the honest
choice here). Contract updates are recorded in
[05 §6/§7](../docs/universal-plugin-mcp-v3/05-universal-core-contract.md),
[06](../docs/universal-plugin-mcp-v3/06-mcp-tool-contracts.md),
[10 §2.3/§2.3a](../docs/universal-plugin-mcp-v3/10-state-modulation-dependencies.md),
[11 §2.7/§3](../docs/universal-plugin-mcp-v3/11-planning-transactions-and-verification.md),
[12 B-38](../docs/universal-plugin-mcp-v3/12-testing-strategy.md) and
[18 Addendum 2](../docs/universal-plugin-mcp-v3/18-remediation-register.md).

### 21.1 Decision 1 — the rules Vital declares but does not evaluate

What changed:

| Item | Before | Now |
| --- | --- | --- |
| rule status | `evaluated: false` in the profile only | `evaluated: false` + `blocking` + `needs` + `guard`/`guard_enforceable` + `why_not_enforced` in the profile **and** in `rules_not_evaluated()` |
| disclosure in a response | none (an empty findings list could read as "all checked") | `dependency_rules_not_evaluated` on **every** transaction result, dry run and refusal included |
| refusal on an unevaluable rule | none | the one rule whose **subject** is decidable is guarded: a plan that enters the subject refuses before the first write |
| finding rows | `rule/kind/message/subject` | `+ detail`, `evaluated`, `reason`, `missing_input` - and a refusal message that says *unevaluable* rather than *unmet* |

The guard is built from data the registry actually carries: the subject names come
from the provider's own measured DISABLE table (`… "Distortion Type"`), the
subject *paths* from the profile's declared names, and both spellings are matched
(the measured `state_key` and the subject name, because the structural companion
set used to spell exactly that name as a path). Nothing was hand-typed and no new
predicate was invented.

**Operations and their disposition (the table the brief asks for):**

| Operation | Enters the guarded subject? | Disposition |
| --- | --- | --- |
| parameter write (control with a parameter authority) | no | **unaffected** |
| state write to any path that is not a distortion type | no | **unaffected** (tested: `filter_1_cutoff`, `mode`, `reverb_on`, `osc_1_distortion_amount`) |
| state write to an oscillator distortion type / its name spelling | yes | **refused** (`DependencyViolation`, `rule_not_evaluated`, `write_calls_attempted: 0`, identical for `dry_run: true`) |
| structural change | yes (its DISABLE set contains the three types) | **refused** - see 21.2, where the deeper defect lives |
| route create/update/remove | the rule `route_into_off_filter` cannot be guarded | **allowed and disclosed**, not blocked: the registry has no destination families, so the guard would be a name-prefix guess, and the layer refuses to take a refusal on a guess. Residual L-9-adjacent, escalated in 21.7 |
| modulation depth / list | no | **unaffected** |

### 21.2 Decision 1's mandatory fix — two measured defects in the DISABLE set

Both were found while making the rule's subject addressable, both are decidable
offline, and both would have produced **false success**:

| # | Defect | Measurement | Fix |
| --- | --- | --- | --- |
| 1 | the DISABLE candidates were spelled as document paths **verbatim** (`settings.Oscillator 1 Distortion Type`), and the codec's `set_path` **creates** unknown keys | fixture: a structural write created **13 phantom keys** and changed **no** real setting; the captured **live** document has 781 settings keys, **none containing a space**, with `osc_1_distortion_type`/`reverb_on` as the real ones | targets resolve through the profile's measured bindings; a target that cannot be mapped, or whose off value has no justified form in the document's space, is a **typed refusal** (`disable_target_unmapped` / `disable_value_not_measurable`) |
| 2 | `disable_set()` compared candidate names against `str(control_dict)`, so "the candidates the plan **keeps on**" was inert and every candidate was disabled | direct read of the call site | names are compared normalised against the profile's own declared names |

Consequence for the structural path: on the real profile 10 of 13 targets are
addressable with a measured numeric off value, and the three oscillator
distortion **types** are refused with `disable_value_not_measurable` because their
off value is a display string and the control declares no conversion
(`law: none`, no measured members). So a structural change **refuses today**
instead of writing junk - a capability that never worked correctly, now reported
as L-9 rather than faked.

### 21.3 Decisions 2 and 5 — the derived leaf and one representation per fact

| Claim | Meaning | Where it is reported |
| --- | --- | --- |
| `derived_excluded` | **mechanism**: the captured leaves the restore set leaves out (codec field) | codec change set, live artifacts |
| `derived_leaf_not_restored` | **consequence**: one named-gap row per excluded leaf | `outcome.named_gaps` on **every** path where a restore ran |
| `rollback.verified` | the restore's own writes landed and were compared | outcome |
| `rollback.complete` | that set covered the whole capture | outcome (new) |

Consequences that had to change with it:

- a restore that restored everything except a declared derived leaf is
  `FAILED_AND_PARTIALLY_RESTORED` / `partially_restored`, **not**
  `FAILED_AND_RESTORED` / `unchanged_verified` - the latter would be a claim about
  state that is not unchanged (contract 11 §3 records the refinement; it
  strengthens an approved guarantee and weakens nothing);
- `tests/plugins/test_p2_transaction.py` asserted the old verdict and now asserts
  the pair (`verified: true`, `complete: false`, `excluded: [...]`, the gap);
- the live harness reports the same shape: `H.derived_leaf_gaps()` /
  `H.restore_completeness()`, used by the acceptance run and by `restore_p8.py`;
- the recovery tool (`restore_p8.py`) now excludes the declared derived leaf from
  its restore **request** as well - it previously asked for every captured leaf,
  which the codec refuses, so the *recovery* path would have been refused
  wholesale. A defect on the incident path, fixed.

### 21.4 Decisions 3 and 4 — identity, and `force`

**Identity (trace + policy).** The path is
`api._resolution` → `RS.resolve_instance_profile` → `resolve_one` → registry
`issue`/`revalidate` → `Transaction._revalidate_target`/`_revalidate` (before
every unit and before the parameter batch) → `write_state_slice`/`write_params`.

| Datum | What it establishes | What it does **not** |
| --- | --- | --- |
| `location` (track, fx) | where the write goes | that the instance is the earlier one |
| build facts (`fx_name`, `class_id`, `module`, `param_count`) | the build at that slot now | instance continuity |
| `session_handle` (FX handle, in-memory only) | that the *slot* is the same one (measured: it survives a repopulation) | instance continuity |
| `StateRevision` (document + full parameter vector) | the mutable state the plan was built against | instance identity |
| caller-supplied `identity`/`target_token` | checked against fresh facts (new) | continuity |

Implemented: caller-supplied identity mismatch or an unknown/mismatched token is
a typed `TargetChanged` refusal **before any write** (previously both were
silently unused); every response carries `meta.identity_basis` stating
`not_claimed: ["instance_continuity"]` with the measurement that limits it.
Boundary documented in 21.5.

**`force`.** No override exists in V1, so it is no longer accepted-and-ignored:
`force=true` → `UnsupportedOverride` (`reason: force_not_supported`) before any
host work, at the tool boundary **and** in the Core; a wrong type (`1`, `"false"`,
`[]`, `{}`) → `InvalidValue` naming the argument; the SPEC entry and the
synthesized docstring say so; a hard dependency conflict cannot be bypassed
(asserted end to end).

### 21.5 The identity boundary, stated exactly

Every **response** (success or refusal) carries `meta.identity_basis`; the
`outcome` block carries no identity claim at all. Verified by driving all 7 tools
through the registered FastMCP surface in three refusal shapes (zero providers,
missing arguments, the `force` refusal) plus the success shapes: **no envelope
without the disclosure** (probe, 2026-10-10; the refusal gap was an audit finding
- A2 in §21.9).

| Scenario | Behaviour |
| --- | --- |
| the caller supplies an `identity` or a session token that disagrees with the host | **refused** (`TargetChanged`) before the first write |
| the host facts move between the resolution and a write (build changed, handle changed, location changed) | **refused** (token revalidation / `StateConflict`) before that unit |
| the state moves under the transaction (external writer) | **refused** or rollback per Decision 3, with truthful partial/unknown outcomes |
| a different instance with **different state** occupies the slot | its state revision differs → **refused** before the first write |
| an identical-build instance with **identical** facts and state occupies the slot | **not detectable** (measured) → the write proceeds against what is there now, verified and restorable, and the response says continuity is **not claimed** |
| any operation that would need proven continuity to be safe | there is none today: every mutation is authorised against the state it captured and verified in the same call; if one is ever added, it must consult `identity_basis` and refuse |

### 21.6 Tests, and what was deliberately not run

| Command | Kind | Result |
| --- | --- | --- |
| `py -3.11 -m pytest tests/plugins -q -p no:cacheprovider --ignore=tests/plugins/live` | static + mock | **350 passed** |
| `py -3.11 -m pytest tests/plugins/live/test_live_vital_read.py -q -p no:cacheprovider` | live **read** | **1 passed** (1.96 s) |
| `py -3.11 -m pytest tests/test_midi_mock.py -q -p no:cacheprovider` | mock regression | **2 passed** |
| `py -3.11 tests/plugins/live/restore_p8.py --dry-run` | live **read** only, writes nothing | verification artifact regenerated with the canonical gap + completeness (§21.8) |

New test files: `test_p11_force_and_identity.py` (27), `test_p11_rules.py` (20),
`test_p11_gaps.py` (7) - 53 policy tests. Two of the rule tests run against the
**real** registry when the machine has it and skip otherwise (they also state what
the data yields, including the three refused disable targets).

**Not run, and why:** the live **write** suite (P8.3, P8.4, P10) was not
re-executed. The owner's brief forbids live mutations by default and allows one
only to diagnose a defect that mocks and existing evidence cannot establish; every
change here is a policy/representation change with mock coverage, and no live
measurement was needed to make or verify it. The consequence is recorded plainly:
the live-write artifacts in `tests/plugins/live/evidence/` (20:44–21:12Z)
**predate** this remediation, so they evidence the layer as it was then, not the
`failed_and_partially_restored` refinements or the guard added now.

### 21.7 Residual limitations after this remediation

1. **L-9 (new):** the oscillator distortion-type value space is unmeasured, so the
   structural DISABLE path refuses (10/13 targets are addressable). Fixing it is a
   measurement task or an owner decision to drop those three targets.
2. **`route_into_off_filter` has no enforceable guard** (no destination families
   in the registry). A route into a filter that is off can still be created; the
   layer discloses that the rule was not evaluated instead of pretending.
   Options for the owner: declare destination families in the registry (a
   measurement), let the dependency hook read the live document (a Core change
   with the same shape as the P8 version hook), or keep the disclosure.
3. **L-3 stands:** an identical-build replacement is indistinguishable; the layer
   discloses rather than claims (21.5).
4. **`wave_frame_needs_multiframe_wavetable`** stays informational and is never
   fired; a violation of it can only be reported as an advisory by its own
   declaration.
5. **Live re-verification of the write path after 21.1–21.4 is outstanding** and
   is the first thing to run once the owner authorises a live run (§21.6).

### 21.8 Closing verification of the live project (read-only)

`restore_p8.py --dry-run` was re-run after the harness change: it reads the live
instance, compares it against the earliest recovery artifact and writes nothing to
REAPER. Result: **2858/2858 parameters equal** at tolerance 0.0, `active_routes []`,
and of 1072 document leaves exactly one differs - `settings.sample.samples`, the
plugin-owned blob - now reported as the canonical named gap with
`restore_completeness: {verified: true, complete: false, excluded:
["settings.sample.samples"]}` instead of a bare `declared_derived` list. Artifact:
`tests/plugins/live/evidence/p8_restore__*` (newest run).

### 21.9 Independent audit of this remediation, and what it changed here

A second read-only adversarial audit ran over **this** remediation's diff (not the
P8–P10 one) with a fresh context; its brief was to falsify 15 specific claims. It
confirmed 12 outright and found **8 issues**, of which 6 were genuine defects in
the new code and were **fixed in this remediation** (not deferred):

| # | Finding | Severity | Disposition |
| --- | --- | --- | --- |
| A1 | the unevaluated-rules disclosure was **missing from `plugin_modulation` mutation responses** (the Core rebuilt the payload and dropped it) - exactly Decision 1's failure mode on one of the two mutation surfaces | high | **fixed**: `_mutation_data` carries `dependency_rules_not_evaluated` and the findings now carry `evaluated`/`reason`/`detail`; two tests (Core + registered tool) |
| A2 | "every response carries `meta.identity_basis`" was false for **refusal** envelopes (no `meta` at all) | medium | **fixed**: `tools.identity_meta()` is attached to every refusal envelope (missing-arg, dispatch, typed error, unexpected); the claim is now true and **tested on refusals** |
| A3 | `depth_conversion`'s docstring said `force` is always False while the code echoed the argument into a successful answer | medium | **fixed**: the depth result reports `force: False` (no override exists) |
| A4 | a non-dict or all-unknown supplied `identity` was **silently ignored** (the pre-remediation behaviour for that shape) | medium | **fixed**: `InvalidValue` / `identity_claim_not_comparable`, tested for a string, a bogus object and a list |
| A5 | `rollback.complete` defaulted to **True** on paths where no rollback ran | low-medium | **fixed**: default `False` (`complete` is a statement about a restore that ran), with a test |
| A6 | three branches had never run: the provider-report fallback, the `instance.token` alias, and `disable_targets`' display-space branch | low | **fixed**: one test each (the display branch via a synthetic profile with a real law) |
| A7 | the local name `keep` in the structural block held the set to **disable** | cosmetic | **fixed** (renamed `to_disable`) |
| A8 | a malformed-looking import line in `api.py` left by an edit | cosmetic | **fixed** |

It also independently **reproduced** the required measurements: the phantom-key
creation (`set_path` adds `settings.Oscillator 1 Distortion Type`), the captured
live document (781 settings keys, none with a space, none of the 13 candidate
names present), and `disable_targets` on the real profile (10 native, 3 refused,
0 display). Its two "cannot verify" statements are recorded rather than smoothed:
the index's staging **history** cannot be reconstructed from one snapshot (only
that the staged content predates this remediation, which it proved), and the
model behind the audit cannot be confirmed (the agent self-reported the same model
string as this session, so independence of *process* is established, not of
*model*).

**Re-run after the fixes:** offline **350 passed**, the three P11 files **53
passed**, live read **1 passed**, regression **2 passed**. No refusal bypass was
found by either audit, and no path was found where the gap/completeness pair can
contradict itself.

### 21.10 Git state at the end of the release-review remediation

- Branch `feature/vst-bridge`, HEAD `c060def`; **no commit was created and the
  index was not modified** by this remediation.
- The P0–P10 work had been staged by the owner **before** this task (115 paths,
  fingerprint `6c7abba6…`); this remediation's changes are therefore **unstaged
  modifications** of already-tracked files plus **new untracked files** - listed
  in §22 and in the final report.
- `composer_v1_2_lm_probe.md` remains untracked and untouched.

---

## 22. Safety-gate correction (2026-10-11, after the release-review report)

Two contradictions between the fail-closed policy and the code were returned by the
owner as a **correction** (decisions 6a/6b,
[19 §9](../docs/universal-plugin-mcp-v3/19-owner-decisions.md)). No new feature, no
new measurement, **no live mutation, no commit, no index change**.

### 22.1 Gate 1 — `route_into_off_filter`

**What was wrong.** The rule was declared but not evaluated, and route changes were
allowed. A plan that creates a pair was applied under a rule nobody could check -
the exact contradiction a fail-closed layer must not have.

**What it does now.** Creating a modulation `(source, destination)` pair is refused
**before the first write**, from both mutation surfaces and through every
representation the state channel offers:

| Creation spelling | Refused? |
| --- | --- |
| `routes_create[*]` (via `plugin_write` or `plugin_modulation op=create`) | **yes** |
| `routes_update[*]` naming a non-empty `source`/`destination` | **yes** |
| state write under `settings.modulations` that does not **clear** a pair field (including the table itself or a whole slot) | **yes** |
| a projected world that counts creates without carrying their specs | **yes** (not decidable ⇒ refused, so a different world shape cannot bypass it) |
| `routes_remove[*]`, `routes_update[*]` without source/destination, a state write that clears a pair field, `settings.modulation_<n>_amount` | no (the rule's subject - a created pair - is not entered) |
| every read-only operation | no (no write claim) |

The refusal is `DependencyViolation` with `unevaluable: ["route_into_off_filter"]`,
per-finding `evaluated: false`, `write_calls_attempted: 0`,
`final_state: unchanged_verified` - and it is **identical for `dry_run: true` and
`false`**, so a preview never presents it as applicable.

**How the subject is derived (no invented mapping).** `parse_route_pair_path` lives in
the codec next to the functions that write the representation
(`make_routing`/`clear_routing`), and `addresses_route_table` (in
`dependencies_rules`, not the codec - this sentence said "in the codec" and the
independent audit of 2026-10-11 corrected it, see §22.7) derives its root and table
name from the codec's own constants. A test cross-checks the two: if the layout ever
moved, the guard's test fails instead of silently passing. The deciding inputs are
still missing and the rule is **never presented as evaluated** - it is refused.
Recorded as **L-10**.

### 22.2 Gate 2 — instance continuity (L-3)

**What was wrong.** `not_claimed: ["instance_continuity"]` is a disclosure, not a
protection: a mutation carrying a session token was accepted, so the caller's
belief ("the instance I validated") stood in for a verification the host cannot
perform.

**What it does now.** A **caller-supplied token is refused for any mutation**, with
`IdentityContinuityUnprovable` (`reason: instance_continuity_not_provable`) **before
any host work** (before `fx_facts`, before any read, before any write verb), on both
mutation surfaces and for `dry_run` true and false. The refusal carries what cannot
be proven (`location`, `build_facts`, `session_handle`, `state_revision`), the
measurement that limits it (`p8_l3_identity__20261010T203101Z`) and the remedy.

**What it deliberately keeps available** (and why):

| Operation | Available | Basis |
| --- | --- | --- |
| read with a token (`plugin_read/describe/state/find`, `plugin_modulation list/depth`) | yes | a read makes no write claim; the token is still compared with fresh facts (mismatch ⇒ `TargetChanged`) |
| location-addressed mutation (controls, state, route removal/amount update) | yes | its safety is **in-call**: complete capture, per-unit revalidation, slice-exact write, verified read-back, verified restore. It claims "the instance at this location now", which the layer verifies |
| mutation carrying a token or an uncomparable identity claim | **no** | continuity is unprovable; accepting the claim would be an unbacked permission |

The distinction is stated in the code, the contracts (`05 §5/§7`, `06`, `11 §6`,
`12 B-58`) and in the identity disclosure every response carries. Recorded as
**L-11**.

### 22.3 What this blocks, and what it costs

| Capability | State after the correction |
| --- | --- |
| parameter write (location-addressed) | **available** (verified + restorable) |
| state write to a non-route path | **available** |
| route removal / amount-only update / pair-clearing write | **available** |
| **route creation** | **blocked** while the rule is unevaluable (**L-10**) |
| **any mutation carrying a token** | **blocked** (**L-11**) |
| all reads and diagnostics | **available** |

Both blocks are refusal paths with typed errors, not silent behaviour, and neither
can be bypassed from the other surface (proven by the disposition matrix in
`tests/plugins/test_p12_safety_gates.py`).

### 22.4 Tests

> **The counts below are the state *before* the independent audit's fixes
> (see §22.7).** They are kept as the record of that run; §22.7 carries the
> post-fix numbers. Nothing in the workspace was reverted - only the numbers moved.

| Command | Kind | Result |
| --- | --- | --- |
| `py -3.11 -m pytest tests/plugins -q -p no:cacheprovider --ignore=tests/plugins/live` | static + mock | **387 passed** |
| `py -3.11 -m pytest tests/plugins/test_p12_safety_gates.py -q -p no:cacheprovider` | the two gates | **36 passed** |
| `py -3.11 -m pytest tests/plugins/live/test_live_vital_read.py -q -p no:cacheprovider` | live **read** | **1 passed** (1.98 s) |
| `py -3.11 -m pytest tests/test_midi_mock.py tests/test_controller_structure.py -q -p no:cacheprovider` | repo regression (the 57 legacy tools' shape) | **2 passed** |

Test changes beyond new tests: `test_p7_vital.py` V-3 (the engine case is now the
refusal; the one-route-per-write property is proven on the codec's plan),
`test_p11_rules.py` (the route rule now has a guard),
`test_p11_force_and_identity.py` (the token test now asserts the refusal for
mutations and acceptance for reads), `test_p0_contracts.py` (the new error code in
the closed list). The live files were updated to the new policy and **not run**:
`test_p8_live_write.py` writes a document-only leaf chosen at runtime
(`H.document_only_leaves`) instead of a route, and `test_p10_acceptance.py` asserts
the route refusal as a recorded step.

### 22.5 Residual limitations after this correction

1. **L-10** - route creation is unavailable with Vital until the deciding inputs
   exist. Two options for the owner: a measured destination-family table in the
   registry, or a Core change handing the dependency hook the decoded document (the
   shape of the accepted P8 `document_version` hook).
2. **L-11** - a session token cannot authorise a mutation. If the owner wants the
   token to remain usable for writes, the only honest form is a host-side identity
   the replacement cannot reproduce, which this host does not expose (measured).
3. **The live write suite has not been run since any of this**: the newest live
   write evidence (P8.3/P8.4/P10) predates both the release-review remediation and
   this correction, and the two live files were changed in this correction.
4. **`not_claimed` disclosure remains** (`meta.identity_basis`, every response) even
   where a mechanism now refuses: the refusal covers the token case, and the
   disclosure covers the location case where a replacement is undetectable by
   construction - two different things, both stated.

### 22.6 Git state at the end of the correction

- Branch `feature/vst-bridge`, HEAD `c060def`; **no commit and no destructive
  operation were performed by this correction.**
- **The index moved during this session, and not by this work.** It was re-staged
  at `2026-10-11 05:16:43` (`.git/index` mtime) and now holds **127 paths**
  (`git diff --cached --name-only`), fingerprint
  `1345387640c0259581074981434cf6adf31aef7b78ea87f2939fef2b287f5e81` - the previous
  round measured 115 paths with fingerprint `6c7abba6…`. Every git command run by
  this work is read-only (`rev-parse`, `log`, `status`, `diff`, `diff --cached`,
  `ls-files`); **no `git add`, `commit`, `checkout`, `restore`, `stash` or `clean`
  was issued**, so the staging event is external (owner or editor). The evidence
  is consistent: the previously-untracked P11 files are staged, while
  `tests/plugins/test_p12_safety_gates.py` (created by this correction **after**
  05:16:43) is still untracked.
- Consequence to state plainly: the staged snapshot is **mid-flight** - it contains
  the state as of 05:16:43, and **23 paths** have worktree content newer than their
  staged copy (`AM`/`MM`): `IMPLEMENTATION-STATUS.md`, the eight documents touched
  by the two remediation rounds, `api.py`, `errors.py`, `plan.py`, the four Vital
  provider files, `live_harness.py`, the two live test files, `test_p0_contracts.py`,
  `test_p11_force_and_identity.py`, `test_p11_rules.py`, `test_p7_vital.py`.
- Untracked at the end: `composer_v1_2_lm_probe.md` (the owner's file - present,
  **not modified**, still untracked and absent from the index),
  `tests/plugins/test_p12_safety_gates.py` (new in this correction) and one
  per-run evidence artifact from this session's **read-only** live test
  (`p7_vital_read__20261011T033255Z.json`).
- **No new live evidence was produced by this correction beyond that read
  artifact**: nothing live was measured or mutated.

---

### 22.7 Independent audit of this correction, and what it changed (2026-10-11)

An independent review of the correction (working from the workspace, not from this
journal) **passed every safety claim** - no pair-creating bypass through the state
channel or the route API, no token bypass, the refusal provably precedes any host
contact - and returned **13 counter-examples** about truthfulness and coverage
rather than about the gate's decisions. Two of them were real gaps in the gate, one
was a live-harness hazard, and the rest were claims or tests that had stopped
matching the code. All 13 were closed here; **no safety claim was loosened, and no
contract was changed silently**.

| # | What the audit found | What was changed, and where |
| --- | --- | --- |
| 1 | the refusal's `missing_input` came back as an **integer** in some rows (a loop variable reused the name) | the explanation is bound once as `missing_input` and the shortfall counter renamed; the field is now the text it claims to be (`dependencies_rules._guard_route_pair`) |
| 2 | a control write does not appear in `state_paths_changed`, so a control whose **binding** addresses the route table could create a pair unseen (the audit called this the one hole it could not rule out from the registry) | the guard now walks `world["controls_changed"]` against the profile's declared bindings (both the relative `binding.state_key` and a full dotted path) and refuses; new test `test_the_guard_fires_on_a_control_bound_into_the_route_table`. No shipped Vital control is bound that way - this is the *decidability* case, so the guard does not rest on an assumption about the registry |
| 3 | `why_recorded` still said the create specs are not passed to the hook, while `plan.project_world` now passes them | the recorded reason is now rule-specific: for the route rule it says the specs ride in the world and the deciding inputs are still absent |
| 4 | `live_harness.document_only_leaves` could pick a **route-table** leaf, so the updated P8.4 live case would have refused itself | the candidate set now excludes anything the codec's own predicate calls a route-table path (`H.addresses_route_table`), `H.route_table_leaves` enumerates what was excluded, and both the live case and a new offline test (`test_p8_harness.py::test_document_only_leaves_never_offer_a_route_table_leaf`) assert the filter in both directions |
| 5 | `profile.state.structural_ops` still advertised `route_create` with `verified: true` while the gate refuses it | the entry now carries `available: false`, the reason in words, and `decided_by` - a capability description that contradicts the gate is a false claim |
| 6 | the route limitation was written as **L-9** in two places (L-9 is the unmeasured distortion-value space) | corrected to **L-10** in `19-owner-decisions.md` §9 and `18-remediation-register.md` addendum 3 |
| 7 | the Addendum-2 register row still read "not enforceable from measured data" for a rule that is now gated | row marked **superseded for that rule by Addendum 3**, with the second rule's status kept |
| 8 | a dead `if/else` in the disposition matrix (`... if tool != "plugin_write" else ...`, both arms identical) | removed: the default `dry_run` is read once |
| 9 | the codec cross-check test filtered on a hard-typed `".modulations."` | the filter is derived from `VC.ROUTE_TABLE_KEY` - a literal would stop checking the day the codec renames the table |
| 10 | this journal claimed `addresses_route_table` lives in the codec (it lives in `dependencies_rules`) and the predicate's root was hand-typed | §22.2 corrected, and the predicate now derives **both** the root (`codec_module.MUTABLE_ROOT`) and the table key from the codec |
| 11 | `test_the_registered_write_tool_refuses_a_route_create` asserted `ok is True` (the name described the opposite of the assertion) | renamed to `test_without_the_rule_table_the_write_tool_is_the_control`, with its role (the *control* proving the refusal comes from the rule table) stated in the docstring |
| 12 | the `plugin_modulation` example in `06` showed `op=create` answering `committed: true`, with no annotation | annotated: the example is now labelled shape-only and REFUSED, and the sentence that used to describe a live create result points at the remove case the tests actually pin |
| 13 | the L-3 mock fixture mutated the slot **in place**, and two docstrings claimed the replacement reproduced a "state digest" - neither is what the artifact records | `MockHostBridge.external_swap_slot_object` installs a **fresh** slot object (the P12 continuity tests use it and assert a new object was installed); `api.py`, `errors.py`, `05` §5, `19` §9, `12` B-38 and the P12 module docstring now say what the artifact says: the same handle and the same build facts, **no exposed datum** separating the replacement, and the probe's digests answered a different question (that the addressed instance had not moved) |

**Two things the audit could not settle, kept as limitations rather than as
silence.** (a) The parameter channel is now guarded by *declaration*, not by
measurement: no Vital control is bound into the route table today, and nothing in
this workspace can prove no build ever will - so the guard reads the profile instead
of trusting the assumption. (b) The refusal's `not_provable_by` list names the
things the layer compares, not an exhaustive inventory of host data; it is a
statement about what it can use, not about what REAPER exposes.

**Post-fix test run (2026-10-11, after every change above).**

| Command | Kind | Result |
| --- | --- | --- |
| `py -3.11 -m pytest tests/plugins -q -p no:cacheprovider --ignore=tests/plugins/live` | static + mock | **390 passed** (was 387; +3 tests) |
| `py -3.11 -m pytest tests/plugins/test_p12_safety_gates.py -q -p no:cacheprovider` | the two gates | **38 passed** (was 36) |
| `py -3.11 -m pytest tests/plugins/test_p8_harness.py -q -p no:cacheprovider` | the harness's own promises | **8 passed** |
| `py -3.11 -m pytest tests/plugins/live --collect-only -q -p no:cacheprovider` | live suite import/collect | **4 tests collected** (not executed) |
| `py -3.11 -m pytest tests/plugins/live/test_live_vital_read.py -q -p no:cacheprovider` | live **read** | **1 passed** (1.92 s) |
| `py -3.11 -m pytest tests/test_midi_mock.py tests/test_controller_structure.py -q -p no:cacheprovider` | repo regression | **2 passed** |

The live **write** suite (P8.3/P8.4/P10) remains **updated to the new policy and not
executed**: no live mutation was performed in this correction, per the owner's brief.

### 22.8 Git state after the audit fixes

- Branch `feature/vst-bridge`, HEAD `c060def4616cead0118136d92a0dad8924f495d2`.
  **No commit, no `git add`, no destructive git operation was issued by this
  correction or by the audit fixes**; every git command was read-only.
- `.git/index` mtime is **still `2026-10-11 05:16:43`** - unchanged since §22.6 - and
  holds **127 staged paths**. Two fingerprints, each with its recipe, because §22.6
  recorded a number whose recipe was not written down:
  `sha256(.git/index)` = `a3057a4592f88c0f785ea58b756cc19b68ac1c79e2b4841030f1e58e28893701`;
  `sha256(git diff --cached --name-only | Out-String)` =
  `73ee7a1436da3b2d43bded30b394f8bc8e294a964f752ac3ad527f231fa14f6d`.
  The §22.6 figure (`13453876…`) was taken under a different recipe over what is
  demonstrably the same, unmoved index; it is **not reconciled** here - this note
  replaces it rather than pretending to confirm it.
- `git status --porcelain -uall`: **133 entries** - `A ` 100, `AM` 17, `MM` 8,
  `M ` 2 (`README.md`, `src/reaper_reapy_mcp/server.py`; neither touched by this
  work), `??` 6. `git diff --name-only` (worktree vs index) lists **25** paths, i.e.
  newer worktree content than what the 05:16:43 staging captured.
- Untracked: `composer_v1_2_lm_probe.md` (the owner's file, not modified),
  `tests/plugins/test_p12_safety_gates.py` (new in this correction), and **four**
  per-run read-only live artifacts
  (`p7_vital_read__20261011T033255Z/033502Z/033517Z/035432Z.json`), the newest
  produced by the read re-run recorded in §22.7.

---
